"""Serve a Little Agent profile as an MCP server over stdio.

    little-agent serve-mcp --agent office

Any MCP host (Claude Desktop, Claude Code, an IDE, another agent) can then hand
the agent a task the same way it calls any tool::

    {"mcpServers": {"office": {"command": "little-agent",
                               "args": ["serve-mcp", "--agent", "office"]}}}

This is the third entry point onto the one runtime, alongside chat and A2A, and
it behaves like A2A: each ``tools/call`` builds a fresh agent, passes no history
and gives it a :class:`~little_agent.memory.store.NullMemoryStore`, so calls
share no context and nothing is persisted on the caller's behalf. Tools that
need confirmation are refused unless ``--auto-approve`` is given, because there
is no human at this end of the pipe.

One tool is offered, ``run_task``. Its ``workspace`` / ``allowed_paths``
arguments are the same grant A2A carries in metadata, checked against this
server's own writable / readable roots before any work starts.

stdout carries the protocol and nothing else: anything else the runtime prints
is redirected to stderr, which MCP hosts show as the server's log.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from typing import Any, TextIO

import little_agent
from little_agent import agents
from little_agent.a2a.grant import GrantError, GrantPolicy, WorkGrant
from little_agent.agent import StructuredOutputError
from little_agent.agents import AgentProfile
from little_agent.config import AgentConfig
from little_agent.factory import build_agent
from little_agent.mcp.client import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from little_agent.skills.loader import SkillLoader

TOOL_NAME = "run_task"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class _CancelFlag:
    """Stop controller for one call, tripped by ``notifications/cancelled``."""

    def __init__(self) -> None:
        self.hotkey = "notifications/cancelled"
        self._event = threading.Event()

    @property
    def triggered(self) -> bool:
        return self._event.is_set()

    def request_stop(self) -> None:
        self._event.set()

    def reset(self) -> None:
        pass  # a cancel that arrived before the run started must still count

    def arm(self) -> None:
        pass

    def disarm(self) -> None:
        pass


def _tool_definition(profile: AgentProfile) -> dict[str, Any]:
    skills = SkillLoader(
        [root.resolve() for root in profile.skill_roots()], names=profile.skill_names()
    ).load_all()
    about = profile.description or f"Little Agent profile '{profile.name}'."
    skill_text = ", ".join(skill.name for skill in skills) or "core tools only"
    return {
        "name": TOOL_NAME,
        "title": f"Little Agent ({profile.name})",
        "description": (
            f"{about} Hands one self-contained task to this agent, which works with its "
            f"own tools and skills ({skill_text}) and returns the result. The agent "
            "shares no context with the caller: put everything it needs in the task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "The complete task to do."},
                "context": {
                    "type": "object",
                    "description": "Optional structured data for this task (rendered as JSON).",
                },
                "output_schema": {
                    "type": "object",
                    "description": (
                        "Optional JSON Schema. When given, the result is JSON validated "
                        "against it and returned as structuredContent."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional directory the agent works in (read+write) for this task.",
                },
                "allowed_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional files/directories outside the workspace it may read.",
                },
            },
            "required": ["instruction"],
        },
    }


class McpAgentServer:
    """The protocol side of ``serve-mcp``, independent of where bytes come from."""

    def __init__(
        self,
        config: AgentConfig,
        profile: AgentProfile,
        out: TextIO,
        auto_approve: bool = False,
        allow_any_path: bool = False,
        agent_factory: Any = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.out = out
        self.auto_approve = auto_approve
        self.grant_policy = GrantPolicy.from_config(config, allow_any=allow_any_path)
        self._agent_factory = agent_factory or self._build_agent
        self._write_lock = threading.Lock()
        self._calls: dict[Any, _CancelFlag] = {}
        self._calls_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._tool = _tool_definition(profile)

    # --- transport -----------------------------------------------------------

    def serve(self, stream: TextIO) -> None:
        for line in stream:
            if line.strip():
                self.handle_line(line)
        for thread in self._threads:
            thread.join()

    def handle_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            self._send({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": str(exc)}})
            return
        for item in message if isinstance(message, list) else [message]:
            self.handle(item)

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self.out.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.out.flush()

    def _result(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    # --- dispatch ------------------------------------------------------------

    def handle(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")
            return
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" not in message:
            if method == "notifications/cancelled":
                with self._calls_lock:
                    flag = self._calls.get(params.get("requestId"))
                if flag is not None:
                    flag.request_stop()
            return  # other notifications (initialized, ...) need no action
        request_id = message["id"]
        if method is None:
            return  # a response to a request we never send
        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            self._result(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": f"little-agent/{self.profile.name}",
                        "title": f"Little Agent ({self.profile.name})",
                        "version": little_agent.__version__,
                    },
                    "instructions": (
                        f"Use {TOOL_NAME} to hand a complete, self-contained task to this "
                        "agent. Each call is independent; nothing carries over between calls."
                    ),
                },
            )
        elif method == "ping":
            self._result(request_id, {})
        elif method == "tools/list":
            self._result(request_id, {"tools": [self._tool]})
        elif method == "tools/call":
            if params.get("name") != TOOL_NAME:
                self._error(request_id, INVALID_PARAMS, f"Unknown tool: {params.get('name')}")
                return
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            flag = _CancelFlag()
            with self._calls_lock:
                self._calls[request_id] = flag
            thread = threading.Thread(
                target=self._call, args=(request_id, arguments, flag), daemon=True
            )
            self._threads = [item for item in self._threads if item.is_alive()] + [thread]
            thread.start()
        else:
            self._error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    # --- the tool ------------------------------------------------------------

    def _build_agent(self, grant: WorkGrant, stop: _CancelFlag):
        task_config = grant.apply(self.config)
        task_config.workspace.mkdir(parents=True, exist_ok=True)

        def confirm(tool_name: str, _arguments: dict[str, Any]) -> bool:
            if self.auto_approve:
                return True
            print(f"[mcp] denied '{tool_name}' (needs confirmation; no human at this server).")
            return False

        return build_agent(task_config, self.profile, confirm, stop)

    def _call(self, request_id: Any, arguments: dict[str, Any], flag: _CancelFlag) -> None:
        try:
            result = self._run_task(arguments, flag)
        except Exception as exc:  # noqa: BLE001 - a failed task is a tool error, not a crash.
            result = _tool_error(f"Task failed: {exc}")
        finally:
            with self._calls_lock:
                self._calls.pop(request_id, None)
        if flag.triggered:
            return  # cancelled: the spec says not to answer
        self._result(request_id, result)

    def _run_task(self, arguments: dict[str, Any], flag: _CancelFlag) -> dict[str, Any]:
        instruction = str(arguments.get("instruction") or "").strip()
        if not instruction:
            return _tool_error("'instruction' is required.")
        context = arguments.get("context")
        output_schema = arguments.get("output_schema")
        if context is not None and not isinstance(context, dict):
            return _tool_error("'context' must be an object.")
        if output_schema is not None and not isinstance(output_schema, dict):
            return _tool_error("'output_schema' must be an object.")
        try:
            grant = WorkGrant.request(
                self.config.workspace, arguments.get("workspace"), arguments.get("allowed_paths")
            )
            grant = self.grant_policy.authorize(grant)
        except GrantError as exc:
            return _tool_error(str(exc))

        agent = self._agent_factory(grant, flag)
        try:
            run = agent.run(instruction, context=context, output_schema=output_schema)
        except StructuredOutputError as exc:
            return _tool_error(str(exc))
        result: dict[str, Any] = {"content": [{"type": "text", "text": run.text}], "isError": False}
        if output_schema is not None:
            data = run.data
            # structuredContent must be an object; wrap any other JSON value.
            result["structuredContent"] = data if isinstance(data, dict) else {"result": data}
        return result


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Define the serve-mcp options (shared with the ``little-agent`` CLI)."""

    parser.add_argument("--agent", help="Agent profile to serve (default: the whole library).")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Allow tools that normally require confirmation (no human is at this server).",
    )
    parser.add_argument(
        "--readable-path",
        action="append",
        default=[],
        metavar="PATH",
        help="File or directory the agent may read, and may be granted as allowed_paths. Repeatable.",
    )
    parser.add_argument(
        "--writable-path",
        action="append",
        default=[],
        metavar="PATH",
        help="File or directory outside the workspace the agent may write. Repeatable.",
    )
    parser.add_argument(
        "--allow-any-path",
        action="store_true",
        help="Accept any workspace/path a caller asks for (only for a trusted, private server).",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Serve on stdin/stdout until the host closes the pipe."""

    from little_agent.a2a.serve import with_cli_paths
    from little_agent.mcp.manager import shutdown_shared

    protocol_out = sys.stdout
    # Keep stray prints (tool confirmations, hotkey notices, skill warnings) off
    # the protocol stream.
    sys.stdout = sys.stderr
    try:
        protocol_out.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    config = AgentConfig.from_env()
    config = with_cli_paths(config, args.readable_path, args.writable_path)
    config.workspace.mkdir(parents=True, exist_ok=True)
    profile = agents.resolve_active(config, args.agent)
    auto_approve = args.auto_approve or _env_flag("LITTLE_AGENT_MCP_AUTO_APPROVE")
    allow_any_path = args.allow_any_path or _env_flag("LITTLE_AGENT_MCP_ALLOW_ANY_PATH")
    server = McpAgentServer(
        config, profile, protocol_out, auto_approve=auto_approve, allow_any_path=allow_any_path
    )
    print(
        f"[mcp] serving agent '{profile.name}' over stdio (workspace {config.workspace}; "
        f"confirmation-required tools {'auto-approved' if auto_approve else 'denied'})",
        file=sys.stderr,
    )
    try:
        server.serve(sys.stdin)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_shared()


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def main(argv: list[str] | None = None) -> None:
    run(add_arguments(argparse.ArgumentParser(prog="little-agent-mcp")).parse_args(argv))


if __name__ == "__main__":
    main()
