"""PowerShell pipe and child-process encoding regressions."""

import os
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from little_agent.tools.base import ToolContext
from little_agent.tools.shell import RunPowerShellTool


@pytest.fixture
def context(tmp_path):
    return ToolContext(tmp_path)


def test_shell_preserves_parent_environment_and_cwd(context):
    with patch.dict(os.environ, {"PYTHONIOENCODING": "cp932", "PYTHONUTF8": "0"}):
        with patch("little_agent.tools.shell.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, "ok\n", "",
        )) as execute:
            result = RunPowerShellTool().run(context, command="Write-Output ok", timeout_seconds=7)
        assert os.environ["PYTHONIOENCODING"] == "cp932"
        assert os.environ["PYTHONUTF8"] == "0"
        options = execute.call_args.kwargs
        assert options["env"]["PYTHONIOENCODING"] == "utf-8"
        assert options["env"]["PYTHONUTF8"] == "1"
        assert options["cwd"] == context.workspace
        assert options["timeout"] == 7
    assert result.ok
    assert result.content == "ok"


def test_blocked_command_is_not_started(context):
    with patch("little_agent.tools.shell.subprocess.run") as execute:
        result = RunPowerShellTool().run(context, command="Remove-Item important.txt")
    assert not result.ok
    execute.assert_not_called()


requires_powershell = pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell unavailable")


@requires_powershell
def test_powershell_unicode_stdout(context):
    # Force the parent's original problematic locale; the tool must override it.
    with patch.dict(os.environ, {"PYTHONIOENCODING": "cp932", "PYTHONUTF8": "0"}):
        result = RunPowerShellTool().run(context, command="Write-Output '日本語🙂'")
    assert result.ok, result.content
    assert result.content == "日本語🙂"


@requires_powershell
def test_powershell_unicode_stderr_and_exit_code(context):
    result = RunPowerShellTool().run(
        context, command="[Console]::Error.WriteLine('日本語🙂'); exit 7",
    )
    assert not result.ok
    assert "STDERR:\n日本語🙂" in result.content


def python_command(context, source):
    # Pass a real UTF-8 source file to avoid PowerShell 5.1's native argv quoting.
    script = context.workspace / "unicode_child.py"
    script.write_text(source, encoding="utf-8")
    executable = sys.executable.replace("'", "''")
    script_arg = str(script).replace("'", "''")
    return f"& '{executable}' '{script_arg}'"


@requires_powershell
def test_python_child_unicode_stdout_and_stderr(context):
    command = python_command(context, "import sys\nprint('日本語🙂')\nprint('標準エラー🙂', file=sys.stderr)\n")
    with patch.dict(os.environ, {"PYTHONIOENCODING": "cp932", "PYTHONUTF8": "0"}):
        result = RunPowerShellTool().run(context, command=command)
    assert result.ok, result.content
    assert "日本語🙂" in result.content
    assert "標準エラー🙂" in result.content
    assert "UnicodeEncodeError" not in result.content


@requires_powershell
def test_powershell_unicode_pipe_to_python(context):
    command = python_command(context, "import sys\nprint(sys.stdin.read().strip())\n")
    result = RunPowerShellTool().run(context, command=f"'日本語🙂' | {command}")
    assert result.ok, result.content
    assert result.content == "日本語🙂"


@requires_powershell
def test_nested_python_subprocess_unicode(context):
    command = python_command(context, (
        "import subprocess, sys\n"
        "result = subprocess.run([sys.executable, '-c', \"print('日本語🙂')\"], "
        "capture_output=True, text=True, check=True)\n"
        "print(result.stdout.strip())\n"
    ))
    result = RunPowerShellTool().run(context, command=command)
    assert result.ok, result.content
    assert result.content == "日本語🙂"


@requires_powershell
def test_invalid_output_byte_does_not_crash(context):
    command = python_command(context, "import sys\nsys.stdout.buffer.write(b'bad \\xff byte')\n")
    result = RunPowerShellTool().run(context, command=command)
    assert result.ok, result.content
    assert result.content == "bad \ufffd byte"
