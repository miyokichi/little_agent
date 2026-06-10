from little_agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from little_agent.tools.filesystem import ListDirTool, ReadFileTool, SearchFilesTool, WriteFileTool
from little_agent.tools.memory_tool import UpdateGlobalMemoryTool, UpdateWorkspaceMemoryTool
from little_agent.tools.shell import RunPowerShellTool


def default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in [
        ListDirTool(),
        ReadFileTool(),
        WriteFileTool(),
        SearchFilesTool(),
        RunPowerShellTool(),
    ]:
        registry.register(tool)
    return registry


__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "UpdateGlobalMemoryTool",
    "UpdateWorkspaceMemoryTool",
    "default_tools",
]
