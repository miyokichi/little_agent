from __future__ import annotations

from little_agent.memory import MasterMemory
from little_agent.tools.base import ToolContext, ToolResult


class UpdateWorkspaceMemoryTool:
    name = "update_workspace_memory"
    description = (
        "Overwrite the workspace-level persistent memory (memory.md). "
        "Use to remember facts, preferences, or context specific to this workspace."
    )
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Full markdown content to save as the workspace memory.",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    def __init__(self, memory: MasterMemory) -> None:
        self._memory = memory

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        self._memory.save(str(kwargs["content"]))
        return ToolResult(True, f"Workspace memory saved to {self._memory.path}")


class UpdateGlobalMemoryTool:
    name = "update_global_memory"
    description = (
        "Overwrite the global persistent memory shared across all workspaces and agents. "
        "Use to remember facts, preferences, or context that apply everywhere."
    )
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Full markdown content to save as the global memory.",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    def __init__(self, memory: MasterMemory) -> None:
        self._memory = memory

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        self._memory.save(str(kwargs["content"]))
        return ToolResult(True, f"Global memory saved to {self._memory.path}")
