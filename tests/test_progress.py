"""Intermediate reports reach the user before tools finish, without ending a turn."""
import queue
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from little_agent.cli import _repl
from little_agent.control import StopController
from little_agent.gui import ChatWindow, ChatWorker
from little_agent.tools.base import ToolResult
from test_directories import Replies, batch, tool
from test_gui import harness, next_event
from test_responses import answer, function, wire
from little_agent.llm import OpenAICompatibleResponsesClient


def context(tmp_path):
    return harness(tmp_path, lambda *_: True, StopController("x"))


def test_explicit_report_continues_into_real_work_and_final_answer(tmp_path):
    ctx = context(tmp_path)
    ctx.agent.llm = Replies(
        {"content": "調査を始めます", "tool_calls": [tool("report_progress", message="調査を始めます")]},
        {"content": "ファイルを保存します", "tool_calls": [tool("write_file", path="done.txt", content="結果")]},
        {"content": "保存しました", "tool_calls": []},
    )
    updates = []

    def progress(text):
        assert not (ctx.agent.config.workspace / "done.txt").exists()
        updates.append(text)

    assert ctx.session.send("作業して", on_progress=progress) == "保存しました"
    assert updates == ["調査を始めます", "ファイルを保存します", "ツール実行: write_file"]
    assert (ctx.agent.config.workspace / "done.txt").read_text(encoding="utf-8") == "結果"
    assert [m.content for m in ctx.session.history] == ["作業して", "保存しました"]
    assert "report_progress" not in ctx.agent.tools.names()


@pytest.mark.parametrize("prompt", ["作業して", "/custom"])
def test_cli_flushes_reports_before_tool_execution_and_displays_final_once(tmp_path, monkeypatch, capsys, prompt):
    ctx = context(tmp_path)
    if prompt.startswith("/"):
        ctx.agent.config.commands_dir.mkdir()
        (ctx.agent.config.commands_dir / "custom.md").write_text("作業して", encoding="utf-8")
        ctx.registry.reload_custom()
    seen = []
    original = ctx.agent.tools.get("list_dir").run

    def run(*args, **kwargs):
        seen.append(capsys.readouterr().out)
        return original(*args, **kwargs)

    monkeypatch.setattr(ctx.agent.tools.get("list_dir"), "run", run)
    ctx.agent.llm = Replies(
        {"content": "一覧を確認します", "tool_calls": [tool("list_dir", path=".")]},
        {"content": "確認が終わりました", "tool_calls": []},
    )
    inputs = iter([prompt, "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    printed = Mock(wraps=print)
    monkeypatch.setattr("builtins.print", printed)
    _repl(ctx)
    assert "[進捗] 一覧を確認します" in seen[0]
    assert capsys.readouterr().out.count("確認が終わりました") == 1
    assert any(call.kwargs.get("flush") for call in printed.call_args_list)


def test_gui_progress_arrives_while_tool_is_blocked(tmp_path):
    stop = StopController("x")
    worker = ChatWorker(lambda confirm: harness(tmp_path, confirm, stop), stop, lambda: None)
    release = threading.Event()
    started = threading.Event()

    class WaitingTool:
        name = "wait_test"
        description = "Wait for a test event"
        requires_confirmation = False
        parameters = {"type": "object", "properties": {}}

        def run(self, context, **kwargs):
            started.set()
            assert release.wait(3)
            return ToolResult(True, "完了")

    worker.start()
    ctx = next_event(worker, "ready")
    ctx.agent.tools.register(WaitingTool())
    ctx.agent.llm = Replies(
        {"content": "調査しています", "tool_calls": [tool("wait_test")]},
        {"content": "調査完了", "tool_calls": []},
    )
    try:
        worker.submit("調査して")
        assert next_event(worker, "progress") == "調査しています"
        assert next_event(worker, "progress") == "ツール実行: wait_test"
        assert started.wait(1)
        assert worker.events.empty()  # neither reply nor idle before the tool returns
        release.set()
        assert next_event(worker, "reply") == "調査完了"
        next_event(worker, "idle")
    finally:
        release.set()
        worker.close()
        worker.thread.join(3)
    assert not worker.thread.is_alive()


def test_gui_poll_appends_progress_without_clearing_busy_state():
    app = object.__new__(ChatWindow)
    app.root = Mock()
    app.worker = Mock()
    app.worker.events = queue.Queue()
    app.worker.events.put(("progress", "作業中です"))
    app.busy = True
    app.append = Mock()
    app._set_busy = Mock()
    app._poll()
    app.append.assert_called_once_with("assistant", "作業中です")
    app._set_busy.assert_not_called()
    assert app.busy


def test_progress_callback_can_stop_before_tool_runs(tmp_path):
    ctx = context(tmp_path)
    ctx.agent.llm = Replies(batch(tool("write_file", path="blocked.txt", content="blocked")))
    result = ctx.session.send("停止テスト", on_progress=lambda _: ctx.agent.stop.request_stop())
    assert "Stopped by user" in result
    assert not (ctx.agent.config.workspace / "blocked.txt").exists()


def test_normal_answer_does_not_force_continuation_or_emit_duplicate_final(tmp_path):
    ctx = context(tmp_path)
    ctx.agent.llm = Replies({"content": "こんにちは", "tool_calls": []})
    ctx.agent.llm.complete = Mock(wraps=ctx.agent.llm.complete)
    progress = Mock()
    assert ctx.session.send("こんにちは", on_progress=progress) == "こんにちは"
    progress.assert_not_called()
    ctx.agent.llm.complete.assert_called_once()
    assert "report_progress" in ctx.agent.llm.complete.call_args.args[2].names()
    ctx.agent.llm = Replies({"content": "通常の呼び出し", "tool_calls": []})
    ctx.agent.llm.complete = Mock(wraps=ctx.agent.llm.complete)
    ctx.session.send("進捗不要")
    assert "report_progress" not in ctx.agent.llm.complete.call_args.args[2].names()


def test_responses_report_then_work_then_final(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    requests = wire(monkeypatch,
        {"output": [function("report_progress", '{"message":"確認します"}', "p1")]},
        {"output": [*answer("一覧を取得します")["output"], function()]},
        answer("確認できました"))
    ctx.agent.llm = OpenAICompatibleResponsesClient("dummy")
    updates = []
    assert ctx.session.send("確認して", on_progress=updates.append) == "確認できました"
    assert updates == ["確認します", "一覧を取得します", "ツール実行: list_dir"]
    assert any(t["name"] == "report_progress" for t in requests[0][1]["tools"])
    assert any(i.get("type") == "function_call_output" and i["call_id"] == "p1"
               for i in requests[1][1]["input"])


def test_reports_respect_tool_step_limit(tmp_path):
    ctx = context(tmp_path)
    ctx.agent.config = replace(ctx.agent.config, max_tool_steps=2)
    ctx.agent.llm = Mock()
    ctx.agent.llm.complete.return_value = batch(tool("report_progress", message="処理中"))
    updates = []
    assert "Stopped after 2" in ctx.session.send("作業", on_progress=updates.append)
    assert updates == ["処理中", "処理中"]
    assert ctx.agent.llm.complete.call_count == 2
