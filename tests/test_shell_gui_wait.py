from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from little_agent.agent import Agent
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.skills.loader import SkillLoader
from little_agent.tools.base import ToolContext, ToolRegistry
from little_agent.tools.shell import RunPowerShellTool, _stop_and_collect


class PowerShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ToolContext(Path.cwd())
        self.process = Mock(pid=123, returncode=0)
        self.process.communicate.return_value = ('{"status":"ok","value":"日本語"}', "")
        self.process.poll.return_value = 0
        self.patcher = patch("little_agent.tools.shell.subprocess.Popen", return_value=self.process)
        self.popen = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_success_uses_workspace_utf8_and_preserves_stdout(self) -> None:
        result = RunPowerShellTool().run(self.context, command="python gui.py", timeout_seconds=0)
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.content)["value"], "日本語")
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0][:3], ["powershell", "-NoProfile", "-Command"])
        self.assertIn("[Console]::OutputEncoding", args[0][-1])
        self.assertIn("exit $LASTEXITCODE", args[0][-1])
        self.assertEqual(kwargs["cwd"], self.context.workspace)
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_default_timeout_remains_30_seconds(self) -> None:
        self.process.communicate.side_effect = subprocess.TimeoutExpired("powershell", 0.2)
        with patch("little_agent.tools.shell.time.monotonic", side_effect=[100, 100, 131]):
            with patch("little_agent.tools.shell._stop_and_collect", return_value=("partial", "")):
                result = RunPowerShellTool().run(self.context, command="python gui.py")
        self.assertFalse(result.ok)
        self.assertIn("Timed out after 30 seconds", result.content)
        self.assertIn("partial", result.content)

    def test_zero_timeout_keeps_waiting_past_30_seconds(self) -> None:
        self.process.communicate.side_effect = [
            subprocess.TimeoutExpired("powershell", 0.2),
            subprocess.TimeoutExpired("powershell", 0.2),
            ("confirmed", ""),
        ]
        with patch("little_agent.tools.shell.time.monotonic", return_value=1_000_000):
            result = RunPowerShellTool().run(self.context, command="python gui.py", timeout_seconds=0)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "confirmed")
        self.assertEqual(self.process.communicate.call_count, 3)

    def test_positive_timeout_and_partial_stderr(self) -> None:
        with patch("little_agent.tools.shell.time.monotonic", side_effect=[0, 3]):
            with patch("little_agent.tools.shell._stop_and_collect", return_value=(b"partial", "error")):
                result = RunPowerShellTool().run(self.context, command="python gui.py", timeout_seconds=2)
        self.assertFalse(result.ok)
        self.assertIn("Timed out after 2 seconds", result.content)
        self.assertIn("STDERR:\nerror", result.content)

    def test_invalid_timeout_does_not_launch(self) -> None:
        for value in (-1, None, True, 1.5, "0"):
            with self.subTest(value=value):
                result = RunPowerShellTool().run(self.context, command="python gui.py", timeout_seconds=value)
                self.assertFalse(result.ok)
        self.popen.assert_not_called()

    def test_guard_still_blocks_commands(self) -> None:
        result = RunPowerShellTool().run(self.context, command="Remove-Item file.txt", timeout_seconds=0)
        self.assertFalse(result.ok)
        self.popen.assert_not_called()

    def test_cancel_before_start_does_not_launch(self) -> None:
        context = ToolContext(Path.cwd(), is_cancelled=lambda: True)
        result = RunPowerShellTool().run(context, command="python gui.py", timeout_seconds=0)
        self.assertFalse(result.ok)
        self.assertIn("Cancelled", result.content)
        self.popen.assert_not_called()

    def test_cancel_during_wait_stops_tree_and_returns_failure(self) -> None:
        checks = iter([False, False, True])
        context = ToolContext(Path.cwd(), is_cancelled=lambda: next(checks))
        self.process.communicate.side_effect = subprocess.TimeoutExpired("powershell", 0.2)
        with patch("little_agent.tools.shell._stop_and_collect", return_value=("", "")) as stop:
            result = RunPowerShellTool().run(context, command="python gui.py", timeout_seconds=0)
        self.assertFalse(result.ok)
        self.assertIn("Cancelled by user", result.content)
        stop.assert_called_once_with(self.process)

    def test_nonzero_exit_is_an_error(self) -> None:
        self.process.returncode = 2
        self.process.communicate.return_value = ("", "GUI failed")
        result = RunPowerShellTool().run(self.context, command="python gui.py")
        self.assertFalse(result.ok)
        self.assertEqual(result.content, "STDERR:\nGUI failed")

    def test_empty_output_reports_exit_code(self) -> None:
        self.process.communicate.return_value = ("", "")
        result = RunPowerShellTool().run(self.context, command="python gui.py")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "Exit code: 0")

    def test_missing_powershell_returns_tool_failure(self) -> None:
        self.popen.side_effect = FileNotFoundError("powershell missing")
        result = RunPowerShellTool().run(self.context, command="python gui.py")
        self.assertFalse(result.ok)
        self.assertIn("Could not start PowerShell", result.content)

    def test_keyboard_interrupt_stops_tree_and_propagates(self) -> None:
        self.process.communicate.side_effect = KeyboardInterrupt
        with patch("little_agent.tools.shell._stop_and_collect", return_value=("", "")) as stop:
            with self.assertRaises(KeyboardInterrupt):
                RunPowerShellTool().run(self.context, command="python gui.py", timeout_seconds=0)
        stop.assert_called_once_with(self.process)

    def test_windows_cleanup_uses_taskkill_tree(self) -> None:
        with patch("little_agent.tools.shell.os.name", "nt"):
            with patch("little_agent.tools.shell.subprocess.run") as run:
                _stop_and_collect(self.process)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "123", "/T", "/F"])

    def test_cleanup_drain_is_bounded(self) -> None:
        self.process.communicate.side_effect = subprocess.TimeoutExpired("powershell", 5, output=b"partial")
        with patch("little_agent.tools.shell.os.name", "nt"):
            with patch("little_agent.tools.shell.subprocess.run", side_effect=FileNotFoundError):
                self.assertEqual(_stop_and_collect(self.process), (b"partial", None))


class SubprocessWaitTests(unittest.TestCase):
    """Exercise real pipes/waits on any OS by substituting Python for PowerShell."""

    def python_process(self, script: str, launched: threading.Event | None = None):
        real_popen = subprocess.Popen

        def launch(args, **kwargs):
            if args[0] != "powershell":
                return real_popen(args, **kwargs)
            process = real_popen([sys.executable, "-c", script], **kwargs)
            self.process = process
            if launched:
                launched.set()
            return process

        return patch("little_agent.tools.shell.subprocess.Popen", side_effect=launch)

    def test_agent_does_not_call_llm_again_until_input_is_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launched = threading.Event()
            gate = root / "confirmed"
            script = (
                "from pathlib import Path; import time\n"
                f"while not Path({str(gate)!r}).exists(): time.sleep(0.01)\n"
                'print(\'{"status":"ok","value":"日本語"}\')\n'
            )
            calls = []

            class LLM:
                def complete(self, _model, messages, _tools):
                    calls.append(list(messages))
                    if len(calls) == 1:
                        return {"content": "", "finish_reason": "tool_calls", "tool_calls": [{
                            "id": "gui", "name": "run_powershell",
                            "arguments": {"command": "python gui.py", "timeout_seconds": 0},
                        }]}
                    return {"content": "finished", "finish_reason": "stop", "tool_calls": []}

            registry = ToolRegistry()
            registry.register(RunPowerShellTool())
            config = AgentConfig(model="local", workspace=root, require_confirmation=False,
                                 openai_api_key=None, openai_base_url="http://unused", enable_logging=False)
            stop = StopController("<ctrl>+<alt>+q")
            stop.arm = lambda: None
            agent = Agent(config, SkillLoader(root / "skills"), tools=registry, llm=LLM(), stop=stop)
            answers = []
            worker = threading.Thread(target=lambda: answers.append(agent.run("collect input")), daemon=True)
            with self.python_process(script, launched):
                worker.start()
                try:
                    self.assertTrue(launched.wait(5))
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(worker.is_alive())
                    gate.touch()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                finally:
                    stop._on_activate()
                    worker.join(5)
            self.assertEqual(answers[0].text, "finished")
            self.assertEqual(len(calls), 2)
            self.assertIn('"value":"日本語"', str(calls[1][-1].content))

    def test_real_process_cancellation_returns_promptly(self) -> None:
        canceled = threading.Event()
        timer = threading.Timer(0.4, canceled.set)
        with self.python_process("import time; time.sleep(60)"):
            timer.start()
            started = time.monotonic()
            try:
                result = RunPowerShellTool().run(
                    ToolContext(Path.cwd(), is_cancelled=canceled.is_set),
                    command="python gui.py", timeout_seconds=0,
                )
            finally:
                timer.cancel()
        self.assertFalse(result.ok)
        self.assertIn("Cancelled", result.content)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(self.process.poll())

    def test_agent_cancel_on_last_step_returns_stop_without_another_llm_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stop = StopController("<ctrl>+<alt>+q")
            stop.arm = lambda: None
            llm = Mock()
            llm.complete.return_value = {"content": "", "finish_reason": "tool_calls", "tool_calls": [{
                "id": "gui", "name": "run_powershell",
                "arguments": {"command": "python gui.py", "timeout_seconds": 0},
            }]}
            registry = ToolRegistry()
            registry.register(RunPowerShellTool())
            config = AgentConfig(model="local", workspace=root, require_confirmation=False,
                                 openai_api_key=None, openai_base_url="http://unused",
                                 enable_logging=False, max_tool_steps=1)
            agent = Agent(config, SkillLoader(root / "skills"), tools=registry, llm=llm, stop=stop)
            timer = threading.Timer(0.4, stop._on_activate)
            with self.python_process("import time; time.sleep(60)"):
                timer.start()
                try:
                    result = agent.run("collect input")
                finally:
                    timer.cancel()
            self.assertIn("Stopped by user", result.text)
            llm.complete.assert_called_once()

    def test_real_process_timeout_preserves_partial_output(self) -> None:
        with self.python_process("import time; print('途中の結果', flush=True); time.sleep(60)"):
            result = RunPowerShellTool().run(
                ToolContext(Path.cwd()), command="python gui.py", timeout_seconds=1
            )
        self.assertFalse(result.ok)
        self.assertIn("Timed out after 1 seconds", result.content)
        self.assertIn("途中の結果", result.content)
        self.assertIsNotNone(self.process.poll())

    @unittest.skipUnless(sys.platform == "linux", "checks descendant state via /proc")
    def test_cancellation_also_kills_attached_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = (
                "import subprocess, sys, time\nfrom pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            with self.python_process(script):
                result = RunPowerShellTool().run(
                    ToolContext(Path(tmp), is_cancelled=pid_file.exists),
                    command="python gui.py", timeout_seconds=5,
                )
            self.assertFalse(result.ok)
            self.assertIn("Cancelled", result.content)
            state_file = Path("/proc") / pid_file.read_text() / "stat"
            # An orphaned killed child may briefly remain as a zombie.
            try:
                self.assertIn(state_file.read_text().split(") ", 1)[1].split()[0], {"Z", "X"})
            except FileNotFoundError:
                pass

    def test_real_pipes_handle_large_stdout_and_stderr(self) -> None:
        with self.python_process("import sys; print('a'*100000); print('b'*100000, file=sys.stderr)"):
            result = RunPowerShellTool().run(ToolContext(Path.cwd()), command="python gui.py")
        self.assertTrue(result.ok)
        self.assertIn("a" * 100000, result.content)
        self.assertIn("b" * 100000, result.content)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "requires Windows PowerShell")
class WindowsPowerShellIntegrationTests(unittest.TestCase):
    def test_powershell_error_is_not_masked_by_encoding_wrapper(self) -> None:
        result = RunPowerShellTool().run(
            ToolContext(Path.cwd()), command="Write-Error 'test failure'", timeout_seconds=10
        )
        self.assertFalse(result.ok)
        self.assertIn("test failure", result.content)

    def test_python_unicode_result_and_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "日本語 gui.py"
            script.write_text("print('日本語の結果')\nraise SystemExit(7)\n", encoding="utf-8")
            command = "& '{}' '{}'".format(
                sys.executable.replace("'", "''"), str(script).replace("'", "''")
            )
            result = RunPowerShellTool().run(ToolContext(root), command=command, timeout_seconds=10)
        self.assertFalse(result.ok)
        self.assertIn("日本語の結果", result.content)


if __name__ == "__main__":
    unittest.main()
