"""Tools: what the agent can call.

Core tools are plain Python functions marked with ``@tool``, kept in tool
directories (see :mod:`little_agent.tools.loader`). The ones shipped with the
package live in ``little_agent/builtin_tools/``; add your own by dropping a
``.py`` file into ``<workspace>/tools/`` or ``~/.little_agent/tools/``.

Tools that need the runtime itself — delegation, memory, MCP and skill script
tools — are supplied by the runtime when it builds an agent, not by files.
"""

from __future__ import annotations

from pathlib import Path

from little_agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from little_agent.tools.delegation import DelegateTasksTool, DelegateTaskTool
from little_agent.tools.function import FunctionTool, ToolError, tool
from little_agent.tools.loader import builtin_tools_dir, load_function_tools


def default_tools(
    enabled: set[str] | None = None,
    directories: tuple[Path, ...] | list[Path] | None = None,
    disabled: set[str] | frozenset[str] | tuple[str, ...] = (),
    workspace: Path | None = None,
) -> ToolRegistry:
    """Build the core tool registry from the tool directories.

    ``directories`` defaults to the built-in tools only. ``enabled`` is an
    optional allowlist (a profile's ``core_tools``); ``disabled`` names tools to
    leave out everywhere. Files that failed to load are listed on the
    registry's ``load_errors``.
    """

    loaded = load_function_tools(
        list(directories) if directories is not None else [builtin_tools_dir()], workspace
    )
    registry = ToolRegistry()
    registry.load_errors = loaded.errors
    for name, found in loaded.tools.items():
        if name in disabled:
            continue
        if enabled is None or name in enabled:
            registry.register(found)
    return registry


__all__ = [
    "DelegateTaskTool",
    "DelegateTasksTool",
    "FunctionTool",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "builtin_tools_dir",
    "default_tools",
    "load_function_tools",
    "tool",
]
