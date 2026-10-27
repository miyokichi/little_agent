from __future__ import annotations

import os
import signal
import subprocess
import time

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
    description = (
        "Run a PowerShell command in the workspace and wait for it to finish. "
        "For a GUI that needs human input, use timeout_seconds=0 to wait without a time limit. "
        "Launch Python directly with & 'path/to/python.exe' 'script.py' so its GUI stays attached; "
        "do not use a detached launcher. The GUI must output its result and exit after confirmation "
        "or cancellation. Destructive commands are blocked by a simple guard."
    )
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "PowerShell command to run."},
            "timeout_seconds": {
                "type": "integer",
                "description": "Timeout in seconds. 0 waits until completion or user stop (for GUI input).",
                "minimum": 0,
                "default": 30,
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        command = str(kwargs["command"])
        timeout = kwargs.get("timeout_seconds", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0:
            return ToolResult(False, "timeout_seconds must be an integer >= 0 (0 means no time limit).")
        lowered = f" {command} ".lower()
        for token in _BLOCKED_TOKENS:
            if token.lower() in lowered:
                return ToolResult(False, f"Blocked potentially destructive command: {token}")

        if context.is_cancelled and context.is_cancelled():
            return ToolResult(False, "Cancelled by user before starting PowerShell.")

        # Both sides of the pipes use UTF-8, including Python children running
        # under Windows PowerShell 5.1 (whose defaults otherwise depend on locale).
        env = os.environ.copy()
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        script = (
            _UTF8_POWERSHELL + "\n" + command
            + "\n$littleAgentSucceeded = $?"
            + "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
            + "\nif (-not $littleAgentSucceeded) { exit 1 }"
        )
        try:
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", script],
                cwd=context.workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                # Keep descendants in a group that can be stopped on POSIX.
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            return ToolResult(False, f"Could not start PowerShell: {exc}")

        deadline = time.monotonic() + timeout if timeout else None
        reason = ""
        try:
            while True:
                if context.is_cancelled and context.is_cancelled():
                    reason = "Cancelled by user."
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    reason = f"Timed out after {timeout} seconds."
                    break
                try:
                    # communicate drains BOTH pipes while waiting. Repeated
                    # calls after TimeoutExpired retain all captured output.
                    output, error = process.communicate(
                        timeout=0.2 if remaining is None else min(0.2, remaining)
                    )
                    return ToolResult(
                        process.returncode == 0,
                        _output_text(output, error) or f"Exit code: {process.returncode}",
                    )
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            # Ctrl+C must not leave the GUI behind either.
            _stop_and_collect(process)
            raise

        output, error = _stop_and_collect(process)
        body = _output_text(output, error)
        return ToolResult(False, reason + (f"\n{body}" if body else ""))


def _output_text(output: str | bytes | None, error: str | bytes | None) -> str:
    # TimeoutExpired may contain bytes even for a text-mode process.
    def decode(value: str | bytes | None) -> str:
        return (value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or "").strip()

    body, stderr = decode(output), decode(error)
    return f"{body}\nSTDERR:\n{stderr}".strip() if stderr else body


def _stop_and_collect(process: subprocess.Popen[str]) -> tuple[str | bytes | None, str | bytes | None]:
    """Stop the attached process tree and bound the final pipe drain."""
    try:
        if os.name == "nt":
            # Killing only PowerShell can leave Python and its GUI running.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        process.kill()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired as exc:
        # A deliberately detached child may keep inherited pipes open. Do not
        # turn a canceled command into another unbounded wait.
        return exc.stdout, exc.stderr
