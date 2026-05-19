from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Path
    require_confirmation: bool = True


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    content: str


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    requires_confirmation: bool

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def descriptions(self) -> str:
        lines = []
        for tool in self._tools.values():
            lines.append(f"- {tool.name}: {tool.description}")
        return "\n".join(lines)

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]


def resolve_workspace_path(workspace: Path, requested_path: str) -> Path:
    path = Path(requested_path)
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve()
    if workspace not in [resolved, *resolved.parents]:
        raise ValueError(f"Path is outside workspace: {requested_path}")
    return resolved

