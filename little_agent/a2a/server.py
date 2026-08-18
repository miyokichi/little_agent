"""A2A server: publish a Little Agent profile as an A2A-compliant agent.

Serves the Agent Card at ``/.well-known/agent-card.json`` and JSON-RPC 2.0 at
``/`` with ``message/send``, ``tasks/get`` and ``tasks/cancel``.

A request carries its instruction as TextParts, DataParts, or both; a DataPart
may also supply ``context`` for the run and an ``output_schema`` to demand a
machine-readable result, which comes back as a DataPart artifact.

Each task runs in its own worker thread with a **freshly built agent**, and an
agent run keeps nothing after it returns, so tasks never share context.
``tasks/cancel`` trips that task's stop controller, which aborts the agent
between tool calls — the same mechanism as the interactive emergency-stop hotkey.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from little_agent.a2a.models import (
    AGENT_CARD_PATH,
    CONTENT_TYPE_NOT_SUPPORTED,
    DEPTH_METADATA_KEY,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LEGACY_AGENT_CARD_PATH,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TASK_CANCELED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_NOT_CANCELABLE,
    TASK_NOT_FOUND,
    TASK_WORKING,
    TERMINAL_STATES,
    UNSUPPORTED_OPERATION,
    A2AError,
    RequestPayload,
    new_id,
    new_message,
    new_task,
    now_iso,
    parse_request_parts,
    result_artifact,
    rpc_error,
    rpc_result,
)

MAX_BODY_BYTES = 1024 * 1024
# How long message/send waits for a fast task before handing the client a
# non-terminal Task to poll with tasks/get.
DEFAULT_GRACE_SECONDS = 2.0
DEFAULT_PORT = 8800

# Builds an agent for a task. Receives the delegation depth requested by the
# caller and a stop controller wired to this task's cancellation.
AgentFactory = Callable[[int, Any], Any]


class TaskStore:
    """In-memory A2A task store with the stop controller for each running task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._stops: dict[str, Any] = {}
        self._done: dict[str, threading.Event] = {}

    def create(self, task: dict[str, Any], stop: Any) -> threading.Event:
        done = threading.Event()
        with self._lock:
            self._tasks[task["id"]] = task
            self._stops[task["id"]] = stop
            self._done[task["id"]] = done
        return done

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return json.loads(json.dumps(task)) if task is not None else None

    def done_event(self, task_id: str) -> threading.Event | None:
        with self._lock:
            return self._done.get(task_id)

    def set_state(
        self,
        task_id: str,
        state: str,
        message_text: str | None = None,
        artifact: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            status: dict[str, Any] = {"state": state, "timestamp": now_iso()}
            if message_text:
                status["message"] = new_message(
                    "agent", message_text, task_id=task_id, context_id=task["contextId"]
                )
            task["status"] = status
            if artifact is not None:
                task["artifacts"] = [artifact]
            if state in TERMINAL_STATES:
                event = self._done.get(task_id)
                if event is not None:
                    event.set()

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise A2AError(TASK_NOT_FOUND, f"Task not found: {task_id}")
            state = str((task.get("status") or {}).get("state"))
            if state in TERMINAL_STATES:
                raise A2AError(TASK_NOT_CANCELABLE, f"Task already {state}: {task_id}")
            stop = self._stops.get(task_id)
        # Trip the agent's stop flag; the worker records the canceled state.
        if stop is not None:
            stop.request_stop()
        self.set_state(task_id, TASK_CANCELED, "Canceled by the client.")
        result = self.get(task_id)
        assert result is not None
        return result


class A2AService:
    """Transport-independent A2A method handlers."""

    def __init__(
        self,
        card: dict[str, Any],
        agent_factory: AgentFactory,
        token: str | None = None,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
    ) -> None:
        self.card = card
        self.tasks = TaskStore()
        self._agent_factory = agent_factory
        self._token = token
        self._grace = grace_seconds

    def handle(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return rpc_error(None, A2AError(INVALID_REQUEST, "Request must be a JSON object."))
        request_id = payload.get("id")
        try:
            if payload.get("jsonrpc") != "2.0":
                raise A2AError(INVALID_REQUEST, "Only JSON-RPC 2.0 is supported.")
            method = payload.get("method")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise A2AError(INVALID_PARAMS, "params must be an object.")
            if method == "message/send":
                return rpc_result(request_id, self.message_send(params))
            if method == "tasks/get":
                return rpc_result(request_id, self.tasks_get(params))
            if method == "tasks/cancel":
                return rpc_result(request_id, self.tasks_cancel(params))
            if method in {"message/stream", "tasks/resubscribe"}:
                raise A2AError(
                    UNSUPPORTED_OPERATION,
                    "This agent does not support streaming; use message/send and poll tasks/get.",
                )
            if method in {
                "tasks/pushNotificationConfig/set",
                "tasks/pushNotificationConfig/get",
            }:
                raise A2AError(UNSUPPORTED_OPERATION, "Push notifications are not supported.")
            raise A2AError(METHOD_NOT_FOUND, f"Unknown method: {method}")
        except A2AError as exc:
            return rpc_error(request_id, exc)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to a peer.
            return rpc_error(request_id, A2AError(INTERNAL_ERROR, f"Internal error: {exc}"))

    def message_send(self, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message")
        if not isinstance(message, dict):
            raise A2AError(INVALID_PARAMS, "message is required.")
        payload = parse_request_parts(message.get("parts"))
        if not payload.instruction.strip():
            raise A2AError(
                CONTENT_TYPE_NOT_SUPPORTED,
                "No instruction was provided: send a text part, or a data part with "
                "an 'instruction' field.",
            )

        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        try:
            depth = int(metadata.get(DEPTH_METADATA_KEY, 0))
        except (TypeError, ValueError):
            depth = 0

        task_id = new_id()
        context_id = str(message.get("contextId") or new_id())
        task = new_task(task_id, context_id)
        task["history"] = [message]

        stop = _CancelFlag()
        done = self.tasks.create(task, stop)
        self.tasks.set_state(task_id, TASK_WORKING)

        worker = threading.Thread(
            target=self._run_task,
            args=(task_id, payload, depth, stop),
            name=f"a2a-task-{task_id[:8]}",
            daemon=True,
        )
        worker.start()

        # Give quick tasks a chance to finish so the caller avoids a poll round trip.
        done.wait(self._grace)
        result = self.tasks.get(task_id)
        assert result is not None
        return result

    def _run_task(
        self, task_id: str, payload: RequestPayload, depth: int, stop: "_CancelFlag"
    ) -> None:
        try:
            agent = self._agent_factory(depth, stop)
            result = agent.run(
                payload.instruction,
                context=payload.context or None,
                output_schema=payload.output_schema,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the peer as a failed task.
            self.tasks.set_state(task_id, TASK_FAILED, f"Agent run failed: {exc}")
            return
        current = self.tasks.get(task_id) or {}
        if str((current.get("status") or {}).get("state")) == TASK_CANCELED:
            return  # cancellation already recorded; don't overwrite it
        self.tasks.set_state(
            task_id, TASK_COMPLETED, artifact=result_artifact(result.text, result.data)
        )

    def tasks_get(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("id") or "")
        task = self.tasks.get(task_id)
        if task is None:
            raise A2AError(TASK_NOT_FOUND, f"Task not found: {task_id}")
        return task

    def tasks_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.tasks.cancel(str(params.get("id") or ""))

    def authorized(self, header: str | None) -> bool:
        if not self._token:
            return True
        expected = f"Bearer {self._token}"
        return header is not None and header.strip() == expected


class _CancelFlag:
    """Stop controller for a served task.

    Mirrors the ``StopController`` interface the agent loop uses (``triggered`` /
    ``reset`` / ``arm`` / ``disarm``) but is tripped by ``tasks/cancel`` instead of
    a hotkey, so a remote cancel aborts the agent between tool calls.
    """

    def __init__(self) -> None:
        self.hotkey = "tasks/cancel"
        self._event = threading.Event()

    @property
    def triggered(self) -> bool:
        return self._event.is_set()

    def request_stop(self) -> None:
        self._event.set()

    def reset(self) -> None:
        # A cancel can arrive before the agent starts its loop; never clear it.
        return

    def arm(self) -> None:
        return

    def disarm(self) -> None:
        return


def make_handler(service: A2AService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "little-agent-a2a"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib hook
            return  # keep the console clean; the agent prints its own output

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            path = self.path.split("?", 1)[0]
            if path in {AGENT_CARD_PATH, LEGACY_AGENT_CARD_PATH}:
                # The card itself is public so peers can discover how to authenticate.
                self._send_json(200, service.card)
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook
            if not service.authorized(self.headers.get("Authorization")):
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(
                    400, rpc_error(None, A2AError(INVALID_REQUEST, "Missing or oversized body."))
                )
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, rpc_error(None, A2AError(PARSE_ERROR, "Invalid JSON.")))
                return
            response = service.handle(payload)
            self._send_json(200, response)

    return Handler


def serve(
    service: A2AService,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Start the A2A HTTP server (call ``serve_forever`` on the result to block)."""

    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    httpd.daemon_threads = True
    return httpd
