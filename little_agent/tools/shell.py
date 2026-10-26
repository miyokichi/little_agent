from __future__ import annotations

import os
import subprocess

from little_agent.tools.base import ToolContext, ToolResult


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


_UTF8_POWERSHELL = "\n".join([
    "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)",
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
    "$OutputEncoding = [Console]::InputEncoding",
])


class RunPowerShellTool:
    name = "run_powershell"
    description = "Run a PowerShell command in the workspace. Destructive commands are blocked by a simple guard."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "PowerShell command to run."},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds.", "default": 30},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        command = str(kwargs["command"])
        timeout = int(kwargs.get("timeout_seconds", 30))
        lowered = f" {command} ".lower()
        for token in _BLOCKED_TOKENS:
            if token.lower() in lowered:
                return ToolResult(False, f"Blocked potentially destructive command: {token}")

        # Use one encoding across our pipes, PowerShell's native-command pipes,
        # and Python children (including their own subprocess/file defaults).
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"{_UTF8_POWERSHELL}\n{command}"],
            cwd=context.workspace,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = completed.stdout.strip()
        error = completed.stderr.strip()
        body = output
        if error:
            body = f"{body}\nSTDERR:\n{error}".strip()
        return ToolResult(completed.returncode == 0, body or f"Exit code: {completed.returncode}")
