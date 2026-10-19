"""File tools: list, read, write, append, move, delete, search.

Every path goes through ``context.path_policy``, so these tools can only reach
the workspace and the explicitly allowed paths.
"""

from __future__ import annotations

from little_agent.tools import ToolContext, ToolError, tool


@tool
def list_dir(context: ToolContext, path: str) -> str:
    """List files and directories under a workspace path.

    Args:
        path: Workspace-relative directory path.
    """
    policy = context.path_policy
    target = policy.resolve(path, access="read")
    if not target.exists():
        raise ToolError(f"Directory does not exist: {target}")
    if not target.is_dir():
        raise ToolError(f"Path is not a directory: {target}")
    rows = []
    for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        kind = "dir " if child.is_dir() else "file"
        rows.append(f"{kind} {policy.display(child)}")
    return "\n".join(rows) if rows else "(empty)"


@tool
def read_file(context: ToolContext, path: str) -> str:
    """Read a UTF-8 text file inside the workspace.

    Args:
        path: Workspace-relative file path.
    """
    target = context.path_policy.resolve(path, access="read")
    if not target.exists():
        raise ToolError(f"File does not exist: {target}")
    if not target.is_file():
        raise ToolError(f"Path is not a file: {target}")
    return target.read_text(encoding="utf-8")


@tool(requires_confirmation=True)
def write_file(context: ToolContext, path: str, content: str) -> str:
    """Write a UTF-8 text file inside the workspace. Creates parent directories.

    Args:
        path: Workspace-relative file path.
        content: Complete file content.
    """
    policy = context.path_policy
    target = policy.resolve(path, access="write")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {policy.display(target)}"


@tool(name="append_to_file", requires_confirmation=True)
def append_file(context: ToolContext, path: str, content: str) -> str:
    """Append UTF-8 text to a file inside the workspace. Creates the file if missing.

    Args:
        path: Workspace-relative file path.
        content: Text to append to the end of the file.
    """
    policy = context.path_policy
    target = policy.resolve(path, access="write")
    if target.exists() and not target.is_file():
        raise ToolError(f"Path is not a file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(content)
    return f"Appended to {policy.display(target)}"


@tool(requires_confirmation=True)
def delete_file(context: ToolContext, path: str) -> str:
    """Delete a file inside the workspace.

    Args:
        path: Workspace-relative file path.
    """
    policy = context.path_policy
    target = policy.resolve(path, access="write")
    if not target.exists():
        raise ToolError(f"File does not exist: {target}")
    if not target.is_file():
        raise ToolError(f"Path is not a file: {target}")
    target.unlink()
    return f"Deleted {policy.display(target)}"


@tool(requires_confirmation=True)
def move_file(context: ToolContext, source: str, destination: str) -> str:
    """Move or rename a file inside the workspace. Creates destination parent directories.

    Args:
        source: Workspace-relative source file path.
        destination: Workspace-relative destination file path.
    """
    policy = context.path_policy
    origin = policy.resolve(source, access="write")
    target = policy.resolve(destination, access="write")
    if not origin.exists():
        raise ToolError(f"Source does not exist: {origin}")
    if not origin.is_file():
        raise ToolError(f"Source is not a file: {origin}")
    if target.exists():
        raise ToolError(f"Destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    origin.replace(target)
    return f"Moved {policy.display(origin)} -> {policy.display(target)}"


@tool
def search_files(context: ToolContext, path: str, query: str) -> str:
    """Search for text in UTF-8 files under a workspace path.

    Args:
        path: Workspace-relative directory path.
        query: Text to search for.
    """
    policy = context.path_policy
    root = policy.resolve(path, access="read")
    if not root.exists():
        raise ToolError(f"Path does not exist: {root}")
    files = [root] if root.is_file() else [item for item in root.rglob("*") if item.is_file()]
    matches: list[str] = []
    for file_path in files:
        try:
            file_path = policy.resolve(str(file_path), access="read")
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, ValueError):
            continue
        for index, line in enumerate(lines, start=1):
            if query in line:
                matches.append(f"{policy.display(file_path)}:{index}: {line}")
    return "\n".join(matches[:200]) if matches else "(no matches)"
