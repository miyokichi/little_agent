"""Shell tool: run a PowerShell command in the workspace."""

from __future__ import annotations

import subprocess

from little_agent.tools import ToolContext, ToolResult, tool

_BLOCKED_TOKENS = [
    "Remove-Item",
    "rm ",
    "rmdir",
    "del ",
    "Format-Volume",
    "Stop-Computer",
    "Restart-Computer",
    "Set-ExecutionPolicy",
]


@tool(requires_confirmation=True)
def run_powershell(context: ToolContext, command: str, timeout_seconds: int = 30) -> ToolResult:
    """Run a PowerShell command in the workspace. Destructive commands are blocked by a simple guard.

    Args:
        command: PowerShell command to run.
        timeout_seconds: Timeout in seconds.
    """
    lowered = f" {command} ".lower()
    for token in _BLOCKED_TOKENS:
        if token.lower() in lowered:
            return ToolResult(False, f"Blocked potentially destructive command: {token}")

    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=context.workspace,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    body = completed.stdout.strip()
    error = completed.stderr.strip()
    if error:
        body = f"{body}\nSTDERR:\n{error}".strip()
    return ToolResult(completed.returncode == 0, body or f"Exit code: {completed.returncode}")
