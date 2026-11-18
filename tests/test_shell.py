"""Exercise Unicode across real Windows PowerShell and Python pipes."""
import os
import shutil
import sys

import pytest

from little_agent.tools.base import ToolContext
from little_agent.tools.shell import RunPowerShellTool


pytestmark = pytest.mark.skipif(
    os.name != "nt" or not shutil.which("powershell"),
    reason="Requires Windows PowerShell",
)


def test_powershell_preserves_unicode_stdout_and_stderr(tmp_path):
    result = RunPowerShellTool().run(
        ToolContext(tmp_path),
        command="[Console]::Out.WriteLine('日本語の結果 😀'); "
                "[Console]::Error.WriteLine('警告メッセージ 🚀')",
    )
    assert result.ok
    assert "日本語の結果 😀" in result.content
    assert "警告メッセージ 🚀" in result.content


def test_powershell_python_child_uses_utf8_even_with_legacy_parent_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "cp932")
    monkeypatch.setenv("PYTHONUTF8", "0")
    script = tmp_path / "日本語 スクリプト.py"
    script.write_text(
        "import sys\n"
        "print('入力結果 😀')\n"
        "print('診断情報 🚀', file=sys.stderr)\n",
        encoding="utf-8",
    )
    python = sys.executable.replace("'", "''")
    script_path = str(script).replace("'", "''")
    result = RunPowerShellTool().run(
        ToolContext(tmp_path), command=f"& '{python}' '{script_path}'",
    )
    assert result.ok, result.content
    assert "入力結果 😀" in result.content
    assert "診断情報 🚀" in result.content
