from little_agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from little_agent.tools.datetime_tool import GetDateTimeTool
from little_agent.tools.filesystem import ListDirTool, ReadFileTool, SearchFilesTool, WriteFileTool
from little_agent.tools.shell import RunPowerShellTool


def default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in [
        GetDateTimeTool(),
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
    "default_tools",
]
