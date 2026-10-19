"""Connections to the configured MCP servers, shared for the life of the process.

An MCP server is a long-lived peer — often a child process — so it is connected
once and shared by every agent this process builds: each chat turn, an
``/agent`` switch, every A2A task. Agents built from different profiles see
different *selections* of the same connections (:class:`McpView`).

A server is connected the first time an agent that selects it is built. One
that fails to start is reported (``/mcp``, the startup banner) and left out;
the agent still runs with everything else.
"""

from __future__ import annotations

import atexit
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from little_agent.mcp.client import McpClient, McpError
from little_agent.mcp.config import McpConfigError, McpServerConfig, load_mcp_config
from little_agent.mcp.tools import ListMcpResourcesTool, McpTool, ReadMcpResourceTool
from little_agent.mcp.transport import HttpTransport, StdioTransport, Transport

if TYPE_CHECKING:
    from little_agent.config import AgentConfig


@dataclass(slots=True)
class McpServerState:
    config: McpServerConfig
    client: McpClient | None = None
    error: str | None = None
    tool_specs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.config.disabled:
            return "disabled"
        if self.client is not None and self.client.connected:
            return "connected"
        if self.error:
            return "failed"
        return "not connected"


class McpManager:
    def __init__(
        self,
        servers: dict[str, McpServerConfig],
        workspace: Path,
        roots: tuple[Path, ...] = (),
        config_error: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.roots = tuple(dict.fromkeys((workspace, *roots)))
        self.config_error = config_error
        self.states = {name: McpServerState(server) for name, server in servers.items()}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: "AgentConfig") -> "McpManager":
        try:
            servers = load_mcp_config(config.mcp_config_paths, config.workspace)
            error = None
        except McpConfigError as exc:
            servers, error = {}, str(exc)
        return cls(
            servers,
            config.workspace,
            (*config.writable_paths, *config.readable_paths),
            config_error=error,
        )

    def _transport(self, server: McpServerConfig) -> Transport:
        if server.command:
            return StdioTransport(
                server.command, server.args, server.env, server.cwd or self.workspace
            )
        return HttpTransport(str(server.url), server.headers, timeout=server.timeout)

    def ensure(self, name: str) -> McpServerState:
        """Connect ``name`` if it is not already (a failure is recorded, not raised)."""

        with self._lock:
            state = self.states[name]
            if state.config.disabled or (state.client is not None and state.client.connected):
                return state
            if state.error is not None:
                return state  # failed once; /mcp reconnect retries
            self._connect(state)
            return state

    def reconnect(self, name: str) -> McpServerState:
        with self._lock:
            state = self.states[name]
            if state.client is not None:
                state.client.close()
            state.client, state.error, state.tool_specs = None, None, []
            if not state.config.disabled:
                self._connect(state)
            return state

    def _connect(self, state: McpServerState) -> None:
        server = state.config
        client = McpClient(
            self._transport(server), server.name, server.timeout, roots=lambda: list(self.roots)
        )
        try:
            client.connect()
            specs = client.list_tools()
        except (McpError, OSError, ValueError) as exc:
            client.close()
            state.client, state.error, state.tool_specs = None, str(exc), []
            return
        state.client, state.error = client, None
        state.tool_specs = [
            spec for spec in specs if spec.get("name") and server.tool_allowed(str(spec["name"]))
        ]

    def refresh_tools(self, state: McpServerState) -> bool:
        """Re-list a server's tools after it announced a change. True if refreshed."""

        client = state.client
        if client is None or not client.tools_changed:
            return False
        try:
            specs = client.list_tools()
        except McpError:
            return False
        state.tool_specs = [
            spec for spec in specs if spec.get("name") and state.config.tool_allowed(str(spec["name"]))
        ]
        return True

    def view(self, selected: list[str] | None) -> "McpView":
        """The servers an agent may use; ``None`` selects every configured one."""

        names = list(self.states) if selected is None else list(selected)
        missing = [name for name in names if name not in self.states]
        states = [self.ensure(name) for name in names if name in self.states]
        return McpView(self, states, missing)

    def close(self) -> None:
        with self._lock:
            for state in self.states.values():
                if state.client is not None:
                    state.client.close()
                    state.client = None


class McpView:
    """The MCP servers one agent was given, and what they contribute to it."""

    def __init__(self, manager: McpManager, states: list[McpServerState], missing: list[str]) -> None:
        self.manager = manager
        self.states = states
        self.missing = missing

    @property
    def connected(self) -> list[McpServerState]:
        return [state for state in self.states if state.status == "connected"]

    def tools(self) -> list[Any]:
        tools: list[Any] = []
        for state in self.connected:
            tools.extend(McpTool(state, spec) for spec in state.tool_specs)
        with_resources = [
            state for state in self.connected if state.client is not None and state.client.supports("resources")
        ]
        if with_resources:
            tools.append(ListMcpResourcesTool(with_resources))
            tools.append(ReadMcpResourceTool(with_resources))
        return tools

    def refresh(self) -> bool:
        """Pick up tool-list changes announced since the tools were built."""

        return any([self.manager.refresh_tools(state) for state in self.connected])

    def sections(self) -> list[tuple[str, str]]:
        """System-prompt sections: the instructions servers send at initialize."""

        sections = []
        for state in self.connected:
            assert state.client is not None
            if state.client.instructions.strip():
                sections.append(
                    (f"MCP server '{state.config.name}' instructions", state.client.instructions.strip())
                )
        return sections

    def warnings(self) -> list[str]:
        lines = []
        if self.manager.config_error:
            lines.append(f"[mcp] {self.manager.config_error}")
        for name in self.missing:
            lines.append(f"[mcp] server '{name}' is selected by the agent but not defined in mcp.json")
        for state in self.states:
            if state.status == "failed":
                lines.append(f"[mcp] server '{state.config.name}' failed to start: {state.error}")
        return lines

    def describe(self) -> str:
        if not self.states:
            return "none"
        return ", ".join(
            f"{state.config.name} ({state.status}"
            + (f", {len(state.tool_specs)} tool(s)" if state.status == "connected" else "")
            + ")"
            for state in self.states
        )


# --- process-wide sharing ----------------------------------------------------

_shared: dict[tuple[Any, ...], McpManager] = {}
_shared_lock = threading.Lock()


def shared_manager(config: "AgentConfig") -> McpManager:
    """The manager for this set of ``mcp.json`` files, created on first use.

    Keyed on the files alone: an A2A task granted another workspace reuses the
    servers the process already started rather than starting a second copy.
    Their working directory and roots are those of the first configuration.
    """

    key = tuple(config.mcp_config_paths)
    with _shared_lock:
        manager = _shared.get(key)
        if manager is None:
            manager = McpManager.from_config(config)
            _shared[key] = manager
        return manager


def shutdown_shared() -> None:
    with _shared_lock:
        managers = list(_shared.values())
        _shared.clear()
    for manager in managers:
        manager.close()


atexit.register(shutdown_shared)
