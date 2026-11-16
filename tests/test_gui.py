"""Worker concurrency checks plus real Tkinter smoke tests when DISPLAY exists."""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from little_agent import agents
from little_agent.cli import build_chat_context, build_parser
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.gui import ChatWindow, ChatWorker
from little_agent.memory.store import NullMemoryStore
from test_directories import Replies, batch, tool


def next_event(worker, kind, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event, value = worker.events.get(timeout=max(0.01, deadline - time.monotonic()))
        if event == "error":
            raise AssertionError(value)
        if event == kind:
            return value
    raise AssertionError(f"missing event: {kind}")


def harness(tmp_path, confirm, stop):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = AgentConfig(model="mock", workspace=workspace, require_confirmation=False,
                         openai_api_key=None, openai_base_url="unused", enable_logging=False,
                         max_delegation_depth=0, skill_library_dir=tmp_path / "skills",
                         agents_dir=tmp_path / "agents", commands_dir=tmp_path / "commands",
                         global_commands_dir=tmp_path / "global-commands")
    ctx = build_chat_context(config, agents.default_profile(config), confirm, stop, NullMemoryStore())
    stop.arm = lambda: None
    stop.disarm = lambda: None
    return ctx


def test_worker_shares_chat_history_and_handles_slash_commands_without_tk(tmp_path):
    stop = StopController("x")
    cleanup = Mock()
    worker = ChatWorker(lambda confirm: harness(tmp_path, confirm, stop), stop, cleanup)
    worker.start()
    ctx = next_event(worker, "ready")
    ctx.agent.llm = Replies({"content": "最初の回答", "tool_calls": []}, {"content": "続きの回答", "tool_calls": []})
    try:
        worker.submit("最初の質問")
        assert next_event(worker, "reply") == "最初の回答"
        worker.submit("続きの質問")
        assert next_event(worker, "reply") == "続きの回答"
        assert len(ctx.session.history) == 4
        worker.submit("/config")
        assert "workspace" in next_event(worker, "reply")
        worker.submit("/clear")
        assert "cleared" in next_event(worker, "reply")
        assert ctx.session.history == []
    finally:
        worker.close()
        worker.thread.join(3)
    assert not worker.thread.is_alive()
    cleanup.assert_called_once()


def test_worker_confirmation_roundtrip_and_close_cancel_pending_request(tmp_path):
    stop = StopController("x")
    worker = ChatWorker(lambda confirm: harness(tmp_path, confirm, stop), stop, lambda: None)
    worker.start()
    ctx = next_event(worker, "ready")
    directory = tmp_path / "extra"
    directory.mkdir()
    ctx.agent.llm = Replies(batch(tool("add_directory", path=str(directory))), {"content": "追加した", "tool_calls": []})
    worker.submit("外部フォルダーを追加")
    pending = next_event(worker, "confirm")
    assert pending.name == "add_directory" and pending.arguments["path"] == str(directory)
    assert directory not in ctx.agent.config.writable_paths  # no grant before human approval
    pending.answer(True)
    assert next_event(worker, "reply") == "追加した"
    assert directory in ctx.agent.config.writable_paths
    ctx.agent.llm = Replies(batch(tool("add_directory", path=str(tmp_path))))
    worker.submit("別のフォルダーを追加")
    pending = next_event(worker, "confirm")
    worker.close()
    worker.thread.join(3)
    assert pending.answered.is_set() and not pending.approved
    assert not worker.thread.is_alive()


def test_worker_reports_failure_and_accepts_next_turn(tmp_path):
    stop = StopController("x")
    worker = ChatWorker(lambda confirm: harness(tmp_path, confirm, stop), stop, lambda: None)
    worker.start()
    ctx = next_event(worker, "ready")
    ctx.session.send = Mock(side_effect=[ValueError("失敗しました"), "次は成功"])
    try:
        worker.submit("失敗を再現")
        kind, value = worker.events.get(timeout=3)
        assert kind == "error" and "失敗しました" in value
        next_event(worker, "idle")
        worker.submit("もう一度")
        assert next_event(worker, "reply") == "次は成功"
    finally:
        worker.close()
        worker.thread.join(3)


def test_gui_cli_options_and_lazy_tk_import():
    args = build_parser().parse_args(["gui", "--agent", "office", "--no-memory", "--writable-path", "extra"])
    assert args.command == "gui" and args.agent == "office"
    assert args.no_memory and args.writable_path == ["extra"]


@pytest.fixture
def window(tmp_path):
    if os.name != "nt" and sys.platform != "darwin" and not os.environ.get("DISPLAY"):
        pytest.skip("Tk smoke tests need a display (run under Xvfb)")
    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    stop = StopController("x")
    worker = ChatWorker(lambda confirm: harness(tmp_path, confirm, stop), stop, lambda: None)
    app = ChatWindow(root, worker, ["default"])
    pump_until(root, lambda: app.context is not None)
    yield app
    worker.close()
    worker.thread.join(3)
    if not app.destroyed:
        root.destroy()


def pump_until(root, condition, timeout=4):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.005)
    assert condition(), "GUI operation timed out"


def test_actual_tk_send_receive_and_folder_picker(window, tmp_path, monkeypatch):
    app = window
    app.context.agent.llm = Replies({"content": "こんにちは。作業を手伝います。", "tool_calls": []})
    app.input.insert("1.0", "こんにちは")
    app.send_button.invoke()
    assert app.busy and str(app.send_button["state"]) == "disabled"
    pump_until(app.root, lambda: not app.busy)
    assert "こんにちは。作業を手伝います。" in app.transcript.get("1.0", "end")
    assert len(app.context.session.history) == 2
    external = tmp_path / "資料 フォルダー"
    external.mkdir()
    monkeypatch.setattr("tkinter.filedialog.askdirectory", lambda **kw: str(external))
    app.read_only.set(True)
    app.add_button.invoke()
    pump_until(app.root, lambda: not app.busy)
    assert external in app.context.agent.config.readable_paths
    assert external not in app.context.agent.config.writable_paths
    assert "読み取り専用" in app.transcript.get("1.0", "end")


def test_actual_tk_confirmation_on_main_thread_and_exit(window, tmp_path, monkeypatch):
    app = window
    directory = tmp_path / "new-work"
    directory.mkdir()
    observed = []
    def approve(*args, **kwargs):
        observed.append(threading.get_ident())
        return True
    monkeypatch.setattr("tkinter.messagebox.askyesno", approve)
    app.context.agent.llm = Replies(batch(tool("add_directory", path=str(directory)),
         tool("write_file", path=str(directory / "result.txt"), content="GUI confirmed")),
         {"content": "保存できました", "tool_calls": []})
    app.submit("ディレクトリを追加して保存")
    pump_until(app.root, lambda: not app.busy)
    assert observed == [threading.get_ident()]
    assert (directory / "result.txt").read_text() == "GUI confirmed"
    app.submit("/exit")
    pump_until(app.root, lambda: app.destroyed)
    assert not app.worker.thread.is_alive()


def test_actual_tk_stop_remains_responsive_during_llm_wait(window):
    app = window
    started = threading.Event()
    class WaitingLLM:
        def complete(self, model, messages, tools):
            started.set()
            assert app.worker.stop._event.wait(3)
            return batch(tool("write_file", path="should-not-exist.txt", content="blocked"))
    app.context.agent.llm = WaitingLLM()
    app.submit("時間がかかる処理")
    assert started.wait(1)
    app.stop_button.invoke()
    pump_until(app.root, lambda: not app.busy)
    assert "Stopped by user" in app.transcript.get("1.0", "end")
    assert not (app.context.agent.config.workspace / "should-not-exist.txt").exists()


def test_worker_keeps_hindsight_recall_and_exit_reflection(tmp_path):
    from little_agent.memory.hindsight import HindsightMemoryStore
    from test_hindsight import FakeMemory, ScriptedClient
    backend = FakeMemory()
    stop = StopController("x")
    def factory(confirm):
        ctx = harness(tmp_path, confirm, stop)
        ctx.agent.memory = HindsightMemoryStore(backend, ctx.agent.config.workspace)
        ctx.agent.llm = ScriptedClient([
            {"content": "調査結果を確認しました。", "tool_calls": []},
            {"content": '{"memory":"調査時は実測値と設計値を区別する。"}', "tool_calls": []},
        ])
        return ctx
    worker = ChatWorker(factory, stop, lambda: None)
    worker.start()
    next_event(worker, "ready")
    worker.submit("実測値で調査してください")
    assert "確認しました" in next_event(worker, "reply")
    worker.close()
    worker.thread.join(3)
    assert len(backend.queries) == 1
    assert len(backend.saved) == 1  # run and exit must not learn the same turn twice


def test_stop_requested_during_memory_recall_is_not_cleared_before_tool_loop(tmp_path):
    started = threading.Event()
    stop = StopController("x")
    def factory(confirm):
        ctx = harness(tmp_path, confirm, stop)
        class WaitingMemory(NullMemoryStore):
            def prepare_run(self, instruction, history):
                started.set()
                assert stop._event.wait(3)
                return "memory lookup returned"
        ctx.agent.memory = WaitingMemory()
        ctx.agent.llm = Mock()
        return ctx
    worker = ChatWorker(factory, stop, lambda: None)
    worker.start()
    ctx = next_event(worker, "ready")
    try:
        worker.submit("遅い記憶検索")
        assert started.wait(1)
        stop.request_stop()
        assert "Stopped by user" in next_event(worker, "reply")
        ctx.agent.llm.complete.assert_not_called()
    finally:
        worker.close()
        worker.thread.join(3)
