from little_agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from little_agent.tools.filesystem import (
    AppendFileTool,
    DeleteFileTool,
    ListDirTool,
    MoveFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from little_agent.tools.memory_tool import UpdateGlobalMemoryTool, UpdateWorkspaceMemoryTool
from little_agent.tools.shell import RunPowerShellTool
from little_agent.tools.web import FetchUrlTool


def default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in [
        ListDirTool(),
        ReadFileTool(),
        WriteFileTool(),
        AppendFileTool(),
        DeleteFileTool(),
        MoveFileTool(),
        SearchFilesTool(),
        RunPowerShellTool(),
        FetchUrlTool(),
    ]:
        registry.register(tool)
    return registry


__all__ = [
    "AppendFileTool",
    "DeleteFileTool",
    "FetchUrlTool",
    "MoveFileTool",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "UpdateGlobalMemoryTool",
    "UpdateWorkspaceMemoryTool",
    "default_tools",
]
