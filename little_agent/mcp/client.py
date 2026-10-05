"""An MCP client session: one connection to one MCP server.

Covers the parts of the protocol a tool-using agent needs:

* lifecycle — ``initialize`` / ``notifications/initialized`` / shutdown
* tools     — ``tools/list`` (paginated) and ``tools/call``
* resources — ``resources/list``, ``resources/templates/list``, ``resources/read``
* prompts   — ``prompts/list`` and ``prompts/get``
* roots     — answers the server's ``roots/list`` with the agent's workspace and
  allowed paths, so a filesystem-aware server knows where the agent works
* utilities — answers ``ping``; sends ``notifications/cancelled`` for a request
  given up on; notes ``notifications/tools/list_changed``

Sampling and elicitation are not offered (the capabilities are not declared),
so a compliant server never asks for them.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import little_agent
from little_agent.mcp.transport import Transport, TransportError

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class McpError(RuntimeError):
    """A request failed: a JSON-RPC error, a timeout, or a lost connection."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None


class McpClient:
    def __init__(
        self,
        transport: Transport,
        name: str = "mcp",
        timeout: float = 60.0,
        roots: Callable[[], list[Path]] | None = None,
    ) -> None:
        self.transport = transport
        self.name = name
        self.timeout = timeout
        self._roots = roots or (lambda: [])
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._closed_reason: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str = ""
        self.protocol_version: str = PROTOCOL_VERSION
        # Set when the server says its tool list changed since we last read it.
        self.tools_changed = False

    # --- lifecycle -----------------------------------------------------------

    def connect(self) -> "McpClient":
        try:
            self.transport.start(self._on_message, self._on_close)
        except TransportError as exc:
            self._on_close(str(exc))
            raise McpError(f"MCP server '{self.name}': {exc}") from exc
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": {"name": "little-agent", "version": little_agent.__version__},
                },
            )
        except Exception:
            self.close()
            raise
        version = str(result.get("protocolVersion") or "")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            self.close()
            raise McpError(
                f"MCP server '{self.name}' wants protocol version {version!r}; "
                f"supported: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}"
            )
        self.protocol_version = version
        self.transport.set_protocol_version(version)
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")
        self.notify("notifications/initialized")
        return self

    def close(self) -> None:
        try:
            self.transport.close()
        finally:
            self._on_close("client closed the connection")

    @property
    def connected(self) -> bool:
        return self._closed_reason is None

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # --- JSON-RPC ------------------------------------------------------------

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if self._closed_reason is not None:
            raise McpError(f"MCP server '{self.name}' is disconnected: {self._closed_reason}")
        request_id = next(self._ids)
        pending = _Pending()
        with self._lock:
            self._pending[request_id] = pending
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self.transport.send(message)
            if not pending.event.wait(timeout or self.timeout):
                self._cancel(request_id, "timed out")
                raise McpError(
                    f"MCP server '{self.name}' did not answer {method} within "
                    f"{timeout or self.timeout:g}s."
                )
        except TransportError as exc:
            raise McpError(f"MCP server '{self.name}': {exc}") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

        response = pending.response or {}
        if "error" in response:
            error = response["error"] if isinstance(response["error"], dict) else {}
            raise McpError(
                f"MCP server '{self.name}' {method} failed: {error.get('message', error)}",
                code=error.get("code"),
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            self.transport.send(message)
        except TransportError as exc:
            raise McpError(f"MCP server '{self.name}': {exc}") from exc

    def _cancel(self, request_id: int, reason: str) -> None:
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        except McpError:
            pass

    def _on_message(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:
                self._answer_server_request(message)
            elif message["method"] == "notifications/tools/list_changed":
                self.tools_changed = True
            return
        request_id = message.get("id")
        with self._lock:
            pending = self._pending.get(request_id) if isinstance(request_id, int) else None
        if pending is not None:
            pending.response = message
            pending.event.set()

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
        if method == "ping":
            reply["result"] = {}
        elif method == "roots/list":
            reply["result"] = {
                "roots": [
                    {"uri": path.resolve().as_uri(), "name": path.name or str(path)}
                    for path in self._roots()
                ]
            }
        else:
            reply["error"] = {"code": -32601, "message": f"Method not supported: {method}"}
        try:
            self.transport.send(reply)
        except TransportError:
            pass

    def _on_close(self, reason: str) -> None:
        with self._lock:
            if self._closed_reason is None:
                self._closed_reason = reason
            waiting = list(self._pending.values())
        for pending in waiting:
            if pending.response is None:
                pending.response = {"error": {"code": -32000, "message": reason}}
            pending.event.set()

    # --- features ------------------------------------------------------------

    def _paginate(self, method: str, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = self.request(method, {"cursor": cursor} if cursor else {})
            items.extend(item for item in result.get(key) or [] if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return items

    def list_tools(self) -> list[dict[str, Any]]:
        if not self.supports("tools"):
            return []
        self.tools_changed = False
        return self._paginate("tools/list", "tools")

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    def list_resources(self) -> list[dict[str, Any]]:
        if not self.supports("resources"):
            return []
        return self._paginate("resources/list", "resources")

    def list_resource_templates(self) -> list[dict[str, Any]]:
        if not self.supports("resources"):
            return []
        try:
            return self._paginate("resources/templates/list", "resourceTemplates")
        except McpError as exc:
            if exc.code == -32601:  # optional method
                return []
            raise

    def read_resource(self, uri: str) -> dict[str, Any]:
        return self.request("resources/read", {"uri": uri})

    def list_prompts(self) -> list[dict[str, Any]]:
        if not self.supports("prompts"):
            return []
        return self._paginate("prompts/list", "prompts")

    def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        return self.request("prompts/get", {"name": name, "arguments": arguments or {}})
