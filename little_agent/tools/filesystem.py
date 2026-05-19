from __future__ import annotations

from pathlib import Path

from little_agent.tools.base import ToolContext, ToolResult, resolve_workspace_path


class ListDirTool:
    name = "list_dir"
    description = "List files and directories under a workspace path."
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative directory path."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        path = resolve_workspace_path(context.workspace, str(kwargs["path"]))
        if not path.exists():
            return ToolResult(False, f"Directory does not exist: {path}")
        if not path.is_dir():
            return ToolResult(False, f"Path is not a directory: {path}")
        rows = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            kind = "dir " if child.is_dir() else "file"
            rows.append(f"{kind} {child.relative_to(context.workspace)}")
        return ToolResult(True, "\n".join(rows) if rows else "(empty)")


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file inside the workspace."
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        path = resolve_workspace_path(context.workspace, str(kwargs["path"]))
        if not path.exists():
            return ToolResult(False, f"File does not exist: {path}")
        if not path.is_file():
            return ToolResult(False, f"Path is not a file: {path}")
        return ToolResult(True, path.read_text(encoding="utf-8"))


class WriteFileTool:
    name = "write_file"
    description = "Write a UTF-8 text file inside the workspace. Creates parent directories."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "content": {"type": "string", "description": "Complete file content."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        path = resolve_workspace_path(context.workspace, str(kwargs["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(kwargs["content"]), encoding="utf-8")
        return ToolResult(True, f"Wrote {path.relative_to(context.workspace)}")


class SearchFilesTool:
    name = "search_files"
    description = "Search for text in UTF-8 files under a workspace path."
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative directory path."},
            "query": {"type": "string", "description": "Text to search for."},
        },
        "required": ["path", "query"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        root = resolve_workspace_path(context.workspace, str(kwargs["path"]))
        query = str(kwargs["query"])
        if not root.exists():
            return ToolResult(False, f"Path does not exist: {root}")
        files = [root] if root.is_file() else [item for item in root.rglob("*") if item.is_file()]
        matches: list[str] = []
        for file_path in files:
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines, start=1):
                if query in line:
                    rel = file_path.relative_to(context.workspace)
                    matches.append(f"{rel}:{index}: {line}")
        return ToolResult(True, "\n".join(matches[:200]) if matches else "(no matches)")

