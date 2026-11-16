"""Explicit, session-scoped directory access for interactive chat hosts."""
from __future__ import annotations

from typing import Any

from little_agent.tools.base import ToolContext, ToolResult


class AddDirectoryTool:
    name = "add_directory"
    description = (
        "Ask the user to add an existing directory to this chat's accessible paths. "
        "Use access='read' for reference material, 'write' for read/write work. "
        "Every request needs its own human confirmation, including when other tools "
        "are already approved. Does not create a directory or persist configuration."
    )
    requires_confirmation = True
    always_confirm = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "description": "Existing directory; relative to workspace or absolute."},
            "access": {"type": "string", "enum": ["read", "write"], "default": "write"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        if context.add_directory is None:
            return ToolResult(False, "Directory access can only be extended in an interactive chat session.")
        try:
            path = context.add_directory(kwargs.get("path"), kwargs.get("access", "write"))
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            return ToolResult(False, str(exc))
        mode = "read-only" if kwargs.get("access") == "read" else "read/write"
        return ToolResult(True, f"Directory access granted for this chat: {path} ({mode}). Subsequent tools may use it.")
