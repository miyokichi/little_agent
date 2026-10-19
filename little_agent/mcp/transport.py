"""The two standard MCP transports, on the standard library alone.

* :class:`StdioTransport` starts the server as a child process and exchanges
  newline-delimited JSON-RPC messages over its stdin/stdout.
* :class:`HttpTransport` speaks Streamable HTTP: every message is a POST, and
  the reply arrives either as one JSON body or as a ``text/event-stream``.

A transport only moves messages. Every message that arrives — a response, a
notification, or a request from the server — is handed to the ``on_message``
callback given to :meth:`start`; matching responses to requests is the
client's job.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

MessageHandler = Callable[[dict[str, Any]], None]
CloseHandler = Callable[[str], None]


class TransportError(RuntimeError):
    """The server could not be reached, or went away."""


class Transport:
    def start(self, on_message: MessageHandler, on_close: CloseHandler) -> None:
        raise NotImplementedError

    def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def set_protocol_version(self, version: str) -> None:
        """Told after initialize; only HTTP needs it (as a header)."""


def _resolve_command(command: str) -> str:
    """Find the executable the way a shell would, so ``npx`` finds ``npx.cmd``."""

    return shutil.which(command) or command


class StdioTransport(Transport):
    STDERR_TAIL = 20

    def __init__(
        self,
        command: str,
        args: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.command = command
        self.args = args
        self.env = env or {}
        self.cwd = cwd
        self._process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        # The server's own log lines; the tail is quoted when it dies.
        self._stderr: deque[str] = deque(maxlen=self.STDERR_TAIL)

    def start(self, on_message: MessageHandler, on_close: CloseHandler) -> None:
        try:
            self._process = subprocess.Popen(
                [_resolve_command(self.command), *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd else None,
                env={**os.environ, **self.env},
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise TransportError(f"Could not start '{self.command}': {exc}") from exc
        threading.Thread(
            target=self._read_stdout, args=(on_message, on_close), daemon=True
        ).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self, on_message: MessageHandler, on_close: CloseHandler) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Not protocol traffic: a server that logs to stdout by mistake.
                self._stderr.append(line)
                continue
            for item in message if isinstance(message, list) else [message]:
                if isinstance(item, dict):
                    on_message(item)
        code = process.wait()
        on_close(f"server process exited with code {code}{self.stderr_tail()}")

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        for line in process.stderr:
            self._stderr.append(line.rstrip())

    def stderr_tail(self) -> str:
        if not self._stderr:
            return ""
        return "\n--- server stderr ---\n" + "\n".join(self._stderr)

    def send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise TransportError(f"MCP server is not running.{self.stderr_tail()}")
        data = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise TransportError(f"Could not write to MCP server: {exc}") from exc

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        # The spec's shutdown: close stdin, then escalate if it lingers.
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


class HttpTransport(Transport):
    SESSION_HEADER = "Mcp-Session-Id"

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 60.0) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._on_message: MessageHandler | None = None

    def start(self, on_message: MessageHandler, on_close: CloseHandler) -> None:
        self._on_message = on_message

    def set_protocol_version(self, version: str) -> None:
        self.protocol_version = version

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers[self.SESSION_HEADER] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def send(self, message: dict[str, Any]) -> None:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 404 and self.session_id:
                raise TransportError(f"MCP session expired (HTTP 404): {detail}") from exc
            raise TransportError(f"MCP server returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(f"Could not reach MCP server {self.url}: {reason}") from exc

        with response:
            session = response.headers.get(self.SESSION_HEADER)
            if session:
                self.session_id = session
            if response.status == 202:
                return
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in content_type:
                self._read_events(response, message.get("id"))
                return
            raw = response.read().decode("utf-8")
        if raw.strip():
            self._deliver(json.loads(raw))

    def _read_events(self, response: Any, request_id: Any) -> None:
        """Deliver SSE ``data`` payloads until the reply to ``request_id`` arrives."""

        data_lines: list[str] = []
        for raw_line in response:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
                continue
            if line or not data_lines:
                continue  # event:, id:, retry:, comments; or a blank between events
            payload = "\n".join(data_lines)
            data_lines = []
            try:
                message = json.loads(payload)
            except json.JSONDecodeError:
                continue
            self._deliver(message)
            if _answers(message, request_id):
                return
        if data_lines:
            try:
                self._deliver(json.loads("\n".join(data_lines)))
            except json.JSONDecodeError:
                pass

    def _deliver(self, message: Any) -> None:
        assert self._on_message is not None
        for item in message if isinstance(message, list) else [message]:
            if isinstance(item, dict):
                self._on_message(item)

    def close(self) -> None:
        if not self.session_id:
            return
        request = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
        try:
            urllib.request.urlopen(request, timeout=5).close()
        except (urllib.error.URLError, OSError):
            pass  # best effort: a server may not allow session termination
        self.session_id = None


def _answers(message: Any, request_id: Any) -> bool:
    items = message if isinstance(message, list) else [message]
    return any(
        isinstance(item, dict)
        and item.get("id") == request_id
        and ("result" in item or "error" in item)
        for item in items
    )
