"""Learning integration through the real runtime and an independent stdio peer."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from little_agent.agent import Agent, RunInterrupted
from little_agent.cli import build_parser
from little_agent.commands import CommandContext, CommandRegistry
from little_agent.config import AgentConfig
from little_agent.mcp.config import McpServerConfig
from little_agent.mcp.manager import McpManager
from little_agent.memory.store import NullMemoryStore
from little_agent.session import ChatSession
from little_agent.skills.loader import SkillLoader
from little_agent.tools.base import ToolRegistry, ToolResult
from little_agent.work import LearningClient, WorkCoordinator
from little_agent.work.models import LearningError, identity

PEER = Path(__file__).with_name("learning_fake_server.py")


class NoHotkey:
    hotkey = "test"
    triggered = False
    def reset(self): pass
    def arm(self): pass
    def disarm(self): pass


class MockTool:
    requires_confirmation = True
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, name, root):
        self.name = name
        self.description = name
        self.root = root
        self.count = 0
        self.fail = False

    def run(self, context, **kwargs):
        self.count += 1
        if self.name == "inspect_via":
            return ToolResult(True, "資料: Via側壁はテーパー。既存モデルは垂直側壁を想定。")
        path = self.root / "calculation.txt"
        path.write_text("Via抵抗（架空のモック計算）: 1.8 mΩ", encoding="utf-8")
        if self.fail:
            raise RuntimeError("after side effect")
        return ToolResult(True, path.read_text(encoding="utf-8"))


def call(name, **args):
    return {"name": name, "arguments": args}


def response(*calls):
    return {"content": "", "tool_calls": list(calls)}


def progress(messages, action):
    tools = [m.content for m in messages if m.role == "tool" and "operation_id=" in str(m.content)]
    op_id = re.search(r"operation_id=([a-f0-9]{64})", tools[-1]).group(1)
    return call("work_record_progress", action=action, reason="実際のTool出力に基づく", result=tools[-1], evidence=[op_id], state="executed")


class ViaLLM:
    def __init__(self):
        self.step = 0
        self.messages = []

    def complete(self, model, messages, tools):
        self.step += 1
        self.messages.append(list(messages))
        if self.step == 1:
            return response(call("inspect_via"))
        if self.step == 2:
            # Calculation in the very same batch must not run after NEED_INPUT.
            return response(progress(messages, "断面形状を比較した"),
                            call("work_evaluate", target="model_applicability", facts={"sidewall": "taper"}),
                            call("mock_calculate"))
        if self.step == 3:
            return response(call("mock_calculate"))
        if self.step == 4:
            return response(progress(messages, "再検証した条件で計算結果を得た"))
        return {"content": "計算結果は1.8 mΩです。完了するには /work finish。", "tool_calls": []}


class OneResponse:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
    def complete(self, model, messages, tools):
        self.calls += 1
        return self.reply if self.calls == 1 else {"content": "候補結果", "tool_calls": []}


@pytest.fixture
def env(tmp_path):
    control = tmp_path / "control.json"
    control.write_text("{}")
    db = tmp_path / "engine.sqlite3"
    manager = McpManager({"learning_engine": McpServerConfig(
        name="learning_engine", command=sys.executable,
        args=(str(PEER), str(db), str(control)), timeout=2,
    )}, tmp_path)
    coordinator = WorkCoordinator(LearningClient(manager), tmp_path / "data" / "work" / "checkpoint.json")
    tools = ToolRegistry()
    inspect = MockTool("inspect_via", tmp_path)
    calculate = MockTool("mock_calculate", tmp_path)
    tools.register(inspect)
    tools.register(calculate)
    config = AgentConfig(model="mock", workspace=tmp_path, require_confirmation=False,
                         openai_api_key=None, openai_base_url="unused", enable_logging=False)
    llm = ViaLLM()
    agent = Agent(config, SkillLoader(tmp_path / "skills"), tools=tools, llm=llm,
                  stop=NoHotkey(), mcp=manager.view(None))
    session = ChatSession(agent, coordinator)
    state = {"root": tmp_path, "control": control, "db": db, "manager": manager,
             "coordinator": coordinator, "agent": agent, "session": session,
             "inspect": inspect, "calculate": calculate, "llm": llm}
    yield state
    state["coordinator"].close()
    manager.close()


def audit(env, name):
    with sqlite3.connect(env["db"]) as db:
        return [json.loads(r[0]) for r in db.execute("SELECT arguments FROM audit WHERE name=?", (name,))]


def engine_value(env, key):
    with sqlite3.connect(env["db"]) as db:
        row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None


def complete_via(env):
    session, coord = env["session"], env["coordinator"]
    text = session.send("Via抵抗を計算してください")
    assert "質問" in text
    assert coord.work.state == "NEED_INPUT"
    assert len(coord.work.questions) == 1  # peer intentionally duplicates it
    assert env["calculate"].count == 0
    assert len(audit(env, "le_record_progress")) == 1
    text = session.send("側壁形状が異なるので断面を比較してモデルを再検証してください")
    assert "回答の解釈" in text
    assert coord.work.pending_answer
    assert not audit(env, "le_confirm_answer")
    assert not engine_value(env, "knowledge")
    token = coord.answer_token
    assert "1.8 mΩ" in coord.command(env["agent"], "confirm " + token)
    assert env["calculate"].count == 1
    assert coord.work.state == "READY"  # a final answer never finishes work
    assert not audit(env, "le_finish_work")
    assert coord.work.next_action == ""
    review = coord.command(env["agent"], "finish")
    assert "判断基準" in review and "手順" in review and "未解決事項" in review
    assert coord.work.state == "FINISHED"
    assert len(audit(env, "le_reflect")) == 1
    assert not engine_value(env, "knowledge")
    return coord


def test_via_e2e_human_review_and_reuse(env):
    coord = complete_via(env)
    token = next(iter(coord.review_tokens))
    assert "approved" in coord.command(env["agent"], "review approve " + token)
    knowledge = engine_value(env, "knowledge")
    assert len(knowledge) == 1 and knowledge[0]["status"] == "approved"
    first_id = coord.work.work_id
    llm = ViaLLM()
    # On the second job all five steps can continue in a single run.
    env["agent"].llm = llm
    text = env["session"].send("別のVia抵抗も計算してください")
    assert "1.8 mΩ" in text
    assert coord.work.work_id != first_id
    assert env["calculate"].count == 2
    assert coord.knowledge == knowledge
    assert knowledge[0]["id"] in llm.messages[0][-1].content
    assert "DO NOT TRUST DRAFT" not in llm.messages[0][-1].content
    assert len(audit(env, "le_record_answer")) == 1
    assert not env["session"].learn()
    assert isinstance(env["agent"].memory, NullMemoryStore)


def test_restore_question_and_confirmed_resume(env):
    env["session"].send("Via抵抗を計算")
    old = env["coordinator"]
    work_id = old.work.work_id
    old.close()
    restored = WorkCoordinator(LearningClient(env["manager"]), old.path)
    env["coordinator"] = restored
    env["session"].coordinator = restored
    assert restored.work.work_id == work_id
    assert restored.work.questions
    assert "回答の解釈" in env["session"].send("テーパー形状なので再検証してください")
    restored.close()
    again = WorkCoordinator(LearningClient(env["manager"]), old.path)
    env["coordinator"] = again
    env["session"].coordinator = again
    assert again.answer_token is None  # fresh UI review after restart
    assert "トークンが無効" in again.command(env["agent"], "confirm forged")
    again.command(env["agent"], "answer")
    assert "1.8 mΩ" in again.command(env["agent"], "confirm " + again.answer_token)
    assert env["inspect"].count == 1 and env["calculate"].count == 1


def test_tool_replay_returns_cached_result_and_unknown_operation_blocks(env):
    coord = env["coordinator"]
    coord.start("調査")
    env["agent"].llm = OneResponse(response(call("inspect_via"), call("inspect_via")))
    env["session"].send("調査を続ける")
    assert env["inspect"].count == 1
    op_id = identity(["mock_calculate", {}])
    coord.work.operations[op_id] = {"name": "mock_calculate", "arguments": {}, "state": "in_flight", "result": ""}
    coord._save()
    coord.close()
    restored = WorkCoordinator(LearningClient(env["manager"]), coord.path)
    env["coordinator"] = restored
    assert restored.work.state == "PAUSED"
    assert "結果不明" in restored.command(env["agent"], "resume")
    assert env["calculate"].count == 0
    assert "PAUSED" in restored.command(env["agent"], "resolve " + op_id + " retry 出力が存在せず未実行と確認")
    assert op_id not in restored.work.operations


def test_forced_gate_blocks_without_llm_decision_selection(env):
    coord = env["coordinator"]
    coord.start("計算")
    env["agent"].llm = OneResponse(response(call("mock_calculate"), call("inspect_via")))
    text = env["session"].send("計算を実行")
    assert "NEED_INPUT" in text
    assert env["calculate"].count == env["inspect"].count == 0
    assert any(a["target"] == "before_tool:mock_calculate" for a in audit(env, "le_evaluate"))


@pytest.mark.parametrize("decision", [
    {"state": "REVIEW", "questions": [{"id": "expert", "text": "専門家の承認が必要"}]},
    {"state": "BLOCK"}, {"state": "READY_TO_RESUME"},
    {"state": "MYSTERY"}, {"state": "NEED_INPUT"},
    {"state": "READY", "questions": [{"id": "q", "text": "未回答"}]},
])
def test_non_ready_or_invalid_decision_never_runs_tools(env, decision):
    env["control"].write_text(json.dumps({"decision": decision}))
    env["session"].send("計算")
    assert env["calculate"].count == env["inspect"].count == 0
    assert env["coordinator"].work.state not in {"READY", "EXPLORE"}


def test_explore_only_allows_explicitly_trusted_read_tools(env):
    coord = env["coordinator"]
    env["control"].write_text(json.dumps({"decision": {"state": "EXPLORE", "allowed_tools": ["mock_calculate", "read_file"]}}))
    coord.start("調査")
    with pytest.raises(RunInterrupted, match="範囲外"):
        coord.before_tool("mock_calculate", {})
    (env["root"] / "spec.txt").write_text("Via spec")
    assert coord.before_tool("read_file", {"path": "spec.txt"}) is None
    assert env["calculate"].count == 0


@pytest.mark.parametrize("reply", [
    {"content": None}, {"content": "", "tool_calls": "invalid"},
    {"content": "", "tool_calls": [None]},
    response({"name": "mock_calculate", "arguments": "not a dict"}),
    response(call("work_record_progress", action="planned only", reason="", result="", evidence=[], state="executed")),
])
def test_abnormal_llm_output_fails_closed(env, reply):
    env["agent"].llm = OneResponse(reply)
    env["session"].send("調査")
    assert env["calculate"].count == 0
    assert env["coordinator"].work.state == "PAUSED"
    assert not audit(env, "le_record_progress")


@pytest.mark.parametrize("control", [{"fail": "le_evaluate"}, {"malformed": "le_evaluate"}, {"unapproved": True}])
def test_mcp_failures_and_unapproved_knowledge_stop_work(env, control):
    env["control"].write_text(json.dumps(control))
    text = env["session"].send("計算")
    assert "停止" in text
    assert env["calculate"].count == env["inspect"].count == 0
    assert env["coordinator"].work.state == "ERROR"
    assert env["coordinator"].knowledge == []


def test_disconnected_server_reconnect_and_retry(env):
    env["control"].write_text(json.dumps({"drop_reply": "le_start_work"}))
    assert "通信失敗" in env["session"].send("計算")
    work_id = env["coordinator"].work.work_id
    assert env["coordinator"].work.state == "ERROR"
    env["control"].write_text("{}")
    env["manager"].reconnect("learning_engine")
    # Reset scripted LLM; the failed connection had never called it.
    assert "NEED_INPUT" in env["coordinator"].command(env["agent"], "resume")
    starts = audit(env, "le_start_work")
    assert starts[0]["request_id"] == starts[1]["request_id"]
    assert all(a["work_id"] == work_id for a in starts)


def test_answer_correction_and_tokens_cannot_publish_knowledge(env):
    env["session"].send("計算")
    coord = env["coordinator"]
    env["session"].send("最初の回答")
    old_token = coord.answer_token
    env["session"].send("訂正した原文です")
    assert len(audit(env, "le_start_work")) == 1
    assert audit(env, "le_record_answer")[-1]["answer"] == "訂正した原文です"
    assert "トークンが無効" in coord.command(env["agent"], "confirm " + old_token)
    assert not audit(env, "le_confirm_answer")
    assert not audit(env, "le_review_candidate")
    assert not engine_value(env, "knowledge")


def test_privileged_tools_and_resources_are_never_exposed_even_after_refresh(env):
    agent, manager = env["agent"], env["manager"]
    env["coordinator"].bind(agent)
    names = agent.tools.names()
    assert "work_evaluate" in names and "work_record_progress" in names
    assert not any("le_confirm" in n or "le_review" in n or "mcp__learning_engine" in n for n in names)
    assert "read_mcp_resource" not in names
    assert "UNAPPROVED" not in str(agent.mcp.sections())
    manager.reconnect("learning_engine")
    manager.states["learning_engine"].client.tools_changed = True
    agent.mcp.refresh()
    agent._register_mcp_tools()
    assert not any(n.startswith("mcp__learning_engine") for n in agent.tools.names())


def test_unfinished_job_cannot_finish_and_progress_states_distinguish_execution(env):
    coord = env["coordinator"]
    coord.start("調査")
    assert "完了には" in coord.command(env["agent"], "finish")
    coord.record_progress("計算の予定", "次の作業", "未実行", [], "planned")
    coord.record_progress("仮説", "未確認", "推測", [], "hypothesis")
    assert coord.work.recorded_units == []
    with pytest.raises(LearningError, match="成功したTool"):
        coord.record_progress("偽の実行", "", "成功と主張", ["fabricated"], "executed")
    assert [a["state"] for a in audit(env, "le_record_progress")] == ["planned", "hypothesis"]
    assert not audit(env, "le_finish_work")


def test_review_edit_defer_reject_and_stale_token(env):
    coord = complete_via(env)
    token = next(iter(coord.review_tokens))
    coord.command(env["agent"], "review edit " + token + " 形状差がある場合は再検証する")
    assert not engine_value(env, "knowledge")
    assert "無効" in coord.command(env["agent"], "review approve " + token)
    tokens = list(coord.review_tokens)
    coord.command(env["agent"], "review defer " + tokens[0])
    assert not engine_value(env, "knowledge")
    token = next(iter(coord.review_tokens))
    coord.command(env["agent"], "review reject " + token)
    assert not engine_value(env, "knowledge")


def test_finish_lost_reply_does_not_reopen_or_duplicate_reflection(env):
    coord = complete_via(env)
    # A separate new completed candidate with a lost finish reply.
    env["agent"].llm = OneResponse(response(call("inspect_via")))
    env["session"].send("調査2")
    op_id = next(iter(coord.work.operations))
    coord.record_progress("調査した", "資料確認", "結果あり", [op_id], "executed")
    env["control"].write_text(json.dumps({"drop_reply": "le_finish_work"}))
    text = coord.command(env["agent"], "finish")
    assert "通信失敗" in text
    assert coord.work.completion and coord.work.state == "ERROR"
    assert engine_value(env, "work:" + coord.work.work_id)["state"] == "FINISHED"
    env["control"].write_text("{}")
    env["manager"].reconnect("learning_engine")
    assert "知識候補" in coord.command(env["agent"], "finish")
    assert coord.work.state == "FINISHED"
    finishes = audit(env, "le_finish_work")
    assert finishes[-1]["request_id"] == finishes[-2]["request_id"]
    assert len(audit(env, "le_reflect")) == 2  # exactly once for each job


def test_failed_side_effect_requires_human_resolution(env):
    coord = env["coordinator"]
    coord.start("調査")
    # A tool failure can happen after writing; never automatically retry it.
    env["inspect"].fail = False
    env["calculate"].fail = True
    coord.work.operations["fake-proof"] = {"name": "inspect_via", "arguments": {}, "state": "executed", "result": "verified"}
    # The fixture only permits calculation after explicit expert confirmation.
    env["control"].write_text(json.dumps({"decision": {"state": "READY"}}))
    env["agent"].llm = OneResponse(response(call("mock_calculate"), call("mock_calculate")))
    text = env["session"].send("実行")
    assert "失敗" in text and env["calculate"].count == 1
    assert (env["root"] / "calculation.txt").exists()
    assert "結果不明" in coord.command(env["agent"], "resume")
    op_id = identity(["mock_calculate", {}])
    assert coord.work.operations[op_id]["state"] == "uncertain"
    coord.command(env["agent"], "resolve " + op_id + " done 出力ファイルの内容を確認済み")
    assert coord.work.operations[op_id]["state"] == "executed"


def test_coordinator_lock_and_cli_routing(env):
    with pytest.raises(LearningError, match="別の学習Chat"):
        WorkCoordinator(LearningClient(env["manager"]), env["coordinator"].path)
    assert build_parser().parse_args(["chat", "--learning"]).learning
    assert not build_parser().parse_args(["chat"]).learning
    registry = CommandRegistry(env["root"] / "commands", env["root"] / "global")
    ctx = CommandContext(env["session"], registry)
    assert "WorkSession" in registry.dispatch(ctx, "/work status").output
    ordinary = ChatSession(env["agent"])
    ctx.session = ordinary
    assert "--learning" in registry.dispatch(ctx, "/work start 仕事").output


def test_cli_end_to_end_with_trusted_review_actions(env, monkeypatch, capsys):
    import little_agent.cli as cli
    import little_agent.work as work_package
    import little_agent.mcp.manager as mcp_manager
    from types import SimpleNamespace

    env["coordinator"].close()
    actual_type = WorkCoordinator
    captured = {}

    def coordinator_factory(*args, **kwargs):
        value = actual_type(*args, **kwargs)
        captured["coordinator"] = value
        return value

    monkeypatch.setattr(work_package, "WorkCoordinator", coordinator_factory)
    monkeypatch.setattr(mcp_manager, "shared_manager", lambda _config: env["manager"])
    monkeypatch.setattr(cli.AgentConfig, "from_env", lambda: env["agent"].config)
    monkeypatch.setattr(cli, "_select_profile_at_launch", lambda *_: SimpleNamespace(name="mock"))
    monkeypatch.setattr(cli, "build_agent", lambda *a, **kw: env["agent"])
    step = 0

    def human_input(_prompt):
        nonlocal step
        step += 1
        coord = captured["coordinator"]
        if step == 1:
            return "Via抵抗を計算してください"
        if step == 2:
            return "/shape/taper です。再検証してください"  # literal slash-prefixed expert answer
        if step == 3:
            return "/work confirm " + coord.answer_token
        if step == 4:
            return "/work finish"
        if step == 5:
            return "/work review approve " + next(iter(coord.review_tokens))
        return "/exit"

    monkeypatch.setattr("builtins.input", human_input)
    cli.chat(build_parser().parse_args(["chat", "--learning"]))
    output = capsys.readouterr().out
    assert "回答の解釈" in output and "1.8 mΩ" in output and "判断基準" in output
    assert "Unknown command" not in output
    assert audit(env, "le_record_answer")[0]["answer"].startswith("/shape/taper")
    assert engine_value(env, "knowledge")[0]["status"] == "approved"
    assert captured["coordinator"]._lock_file is None


def test_multiple_questions_are_confirmed_individually(env):
    coord = env["coordinator"]
    env["control"].write_text(json.dumps({"decision": {"state": "NEED_INPUT", "questions": [
        {"id": "q1", "text": "形状は？"}, {"id": "q2", "text": "温度は？"}]}}))
    env["session"].send("計算")
    env["session"].send("テーパーです")
    assert "q2" in coord.command(env["agent"], "confirm " + coord.answer_token)
    assert coord.work.questions == [{"id": "q2", "text": "温度は？"}]
    assert not audit(env, "le_resume_work")
    env["session"].send("25度です")
    env["control"].write_text("{}")
    # Resume is allowed only after the second confirmation.
    coord.command(env["agent"], "confirm " + coord.answer_token)
    assert len(audit(env, "le_confirm_answer")) == 2
    assert len(audit(env, "le_resume_work")) == 1


def test_resolving_unknown_operation_does_not_bypass_new_block(env):
    coord = env["coordinator"]
    coord.start("計算")
    op_id = identity(["mock_calculate", {}])
    coord.work.operations[op_id] = {"name": "mock_calculate", "arguments": {}, "state": "in_flight", "result": ""}
    coord.work.state = "PAUSED"
    coord._save()
    coord.command(env["agent"], "resolve " + op_id + " retry 外部操作は未実行と確認")
    env["control"].write_text(json.dumps({"decision": {"state": "BLOCK"}}))
    text = coord.command(env["agent"], "resume")
    assert "BLOCK" in text
    assert env["calculate"].count == 0


def test_optional_mandatory_checkpoint_is_enforced_for_read_tool(env):
    coord = env["coordinator"]
    coord.required_check = lambda name, args: name == "read_file"
    coord.start("調査")
    env["control"].write_text(json.dumps({"decision": {"state": "BLOCK"}}))
    with pytest.raises(RunInterrupted, match="BLOCK"):
        coord.before_tool("read_file", {"path": "spec.txt"})
    assert "read_file" not in [op["name"] for op in coord.work.operations.values()]


def test_plain_chat_without_work_and_restored_final_review(env):
    coord = env["coordinator"]
    env["agent"].llm = OneResponse({"content": "通常会話", "tool_calls": []})
    assert coord.command(env["agent"], "chat こんにちは") == "通常会話"
    assert coord.work is None
    env["agent"].llm = ViaLLM()
    complete_via(env)
    work_id = coord.work.work_id
    coord.close()
    restored = WorkCoordinator(LearningClient(env["manager"]), coord.path)
    env["coordinator"] = restored
    assert restored.review_tokens == {}
    assert "判断基準" in restored.command(env["agent"], "review")
    assert restored.work.work_id == work_id
    assert "work_sessions" in restored.command(env["agent"], "list")
    assert "判断基準" in restored.command(env["agent"], "open " + work_id)


def test_lost_evaluation_reply_recovers_pending_question_from_engine(env):
    coord = env["coordinator"]
    coord.start("計算")
    env["control"].write_text(json.dumps({"drop_reply": "le_evaluate"}))
    with pytest.raises(RunInterrupted, match="通信失敗"):
        coord.evaluate("model_applicability", {"shape": "taper"})
    assert not coord.work.questions
    assert coord.work.state == "ERROR"
    env["control"].write_text("{}")
    env["manager"].reconnect("learning_engine")
    assert "側壁形状" in coord.command(env["agent"], "resume")
    assert coord.work.state == "NEED_INPUT" and coord.work.questions
    assert "回答の解釈" in env["session"].send("形状が異なるので再検証する")
    assert env["calculate"].count == 0


def test_non_json_tool_arguments_never_execute(env):
    env["agent"].llm = OneResponse(response(call("mock_calculate", value=float("nan"))))
    text = env["session"].send("計算")
    assert "non-JSON" in text
    assert env["coordinator"].work.state == "PAUSED"
    assert env["calculate"].count == 0
