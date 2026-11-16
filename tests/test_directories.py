"""Directory grants cross the real Tool Loop and stay scoped to one chat."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from little_agent.agent import Agent
from little_agent.cli import build_chat_context
from little_agent.commands import CommandContext, CommandRegistry
from little_agent.config import AgentConfig
from little_agent.memory.store import NullMemoryStore
from little_agent.session import ChatSession
from little_agent.skills.loader import SkillLoader
from little_agent.tools import default_tools
from little_agent.tools.base import ToolContext
from little_agent.tools.directories import AddDirectoryTool
from little_agent.tools.delegation import DelegateTaskTool
from little_agent.a2a.models import TASK_COMPLETED


class Replies:
    def __init__(self, *responses):
        self.responses = iter(responses)
    def complete(self, model, messages, tools):
        return next(self.responses)


def tool(name, **arguments):
    return {"name": name, "arguments": arguments}


def batch(*calls):
    return {"content": "", "tool_calls": list(calls)}


@pytest.fixture
def chat(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AgentConfig(model="mock", workspace=workspace, require_confirmation=False,
                         openai_api_key=None, openai_base_url="unused", enable_logging=False,
                         max_delegation_depth=0)
    agent = Agent(config, SkillLoader([]))
    agent.stop.arm = lambda: None
    agent.stop.disarm = lambda: None
    session = ChatSession(agent)
    return session, config


def context(agent):
    cfg = agent.config
    return ToolContext(cfg.workspace, cfg.readable_paths, cfg.writable_paths)


def test_directory_addition_unlocks_following_tool_in_same_run(chat, tmp_path):
    session, original = chat
    target = tmp_path / "作業 フォルダー"
    target.mkdir()
    confirmations = []
    session.agent.confirm = lambda name, args: confirmations.append((name, args)) or True
    session.agent.llm = Replies(batch(tool("add_directory", path=str(target)),
                                     tool("write_file", path=str(target / "result.txt"), content="done")),
                                {"content": "完了", "tool_calls": []})
    assert session.send("このフォルダーに結果を作成") == "完了"
    assert (target / "result.txt").read_text() == "done"
    assert confirmations[0][0] == "add_directory"
    assert original.writable_paths == ()  # caller's frozen config never changes
    assert session.agent.config.writable_paths == (target.resolve(),)
    assert str(target) in session.agent._system_prompt([]).content


def test_directory_confirmation_is_separate_from_other_tool_approval(chat, tmp_path):
    session, _ = chat
    target, rejected = tmp_path / "allowed", tmp_path / "rejected"
    target.mkdir()
    rejected.mkdir()
    session.agent.config = replace(session.agent.config, require_confirmation=True)
    approvals = iter([True, True, False])
    calls = []
    def confirm(name, args):
        calls.append(name)
        return next(approvals)
    session.agent.confirm = confirm
    session.agent.llm = Replies(batch(tool("write_file", path="inside.txt", content="inside"),
        tool("add_directory", path=str(target)), tool("add_directory", path=str(rejected)),
        tool("write_file", path=str(rejected / "blocked.txt"), content="blocked")),
        {"content": "終了", "tool_calls": []})
    session.send("作業")
    assert calls == ["write_file", "add_directory", "add_directory"]
    assert target in session.agent.config.writable_paths
    assert rejected not in session.agent.config.writable_paths
    assert not (rejected / "blocked.txt").exists()


def test_read_only_and_explicit_upgrade(chat, tmp_path):
    session, _ = chat
    directory = tmp_path / "refs"
    directory.mkdir()
    source = directory / "source.txt"
    source.write_text("reference")
    session.add_directory(str(directory), "read")
    assert session.agent.tools.get("read_file").run(context(session.agent), path=str(source)).ok
    with pytest.raises(ValueError, match="outside allowed write"):
        session.agent.tools.get("write_file").run(context(session.agent), path=str(source), content="changed")
    session.add_directory(str(directory), "write")
    assert session.agent.tools.get("write_file").run(context(session.agent), path=str(source), content="changed").ok


def test_session_switch_and_clear_preserve_additions_without_leaking_to_new_chats(chat, tmp_path):
    session, config = chat
    directory = tmp_path / "work"
    directory.mkdir()
    session.add_directory(str(directory))
    old = session.agent
    new = Agent(config, SkillLoader([]))
    session.switch_agent(new)
    session.clear()
    assert old.directory_adder is None
    assert new.config.writable_paths == (directory,)
    assert new.directory_adder == session.add_directory
    other = ChatSession(Agent(config, SkillLoader([])))
    assert directory not in other.agent.config.writable_paths


def test_canonical_duplicate_grants_and_relative_paths(chat, tmp_path):
    session, config = chat
    directory = tmp_path / "external"
    directory.mkdir()
    assert session.add_directory("../external") == directory
    assert session.add_directory(str(directory / ".")) == directory
    assert session.add_directory(str(directory), "read") == directory
    assert session.agent.config.writable_paths == (directory,)
    assert session.agent.config.readable_paths == ()
    # A symlink grants its resolved target, not a path that can later be retargeted.
    alias = config.workspace / "linked"
    try:
        alias.symlink_to(directory, target_is_directory=True)
    except OSError:
        return  # Windows without symlink privileges
    session.add_directory(str(alias))
    assert session.agent.config.writable_paths == (directory,)


@pytest.mark.parametrize("path,access", [("", "write"), ("missing", "write"), ("file.txt", "read"), (".", "admin")])
def test_invalid_directory_never_changes_access(chat, path, access):
    session, config = chat
    (config.workspace / "file.txt").write_text("file")
    with pytest.raises((OSError, ValueError)):
        session.add_directory(path, access)
    assert session.agent.config == config


def test_slash_command_preserves_spaces_quotes_and_windows_backslashes(chat):
    session, config = chat
    registry = CommandRegistry(config.workspace / "commands", config.workspace / "global")
    ctx = CommandContext(session, registry)
    original_adder = session.add_directory
    session.add_directory = Mock(return_value=Path("dummy"))
    text = r'/add_directory --read-only "C:\Users\Me\My Docs"'
    result = registry.dispatch(ctx, text)
    session.add_directory.assert_called_once_with(r"C:\Users\Me\My Docs", "read")
    assert "読み取り専用" in result.output
    session.add_directory = original_adder
    assert "使い方" in registry.dispatch(ctx, "/add_directory").output
    assert "追加できません" in registry.dispatch(ctx, "/add_directory no-such-directory").output


def test_remote_runtime_cannot_expand_its_grant(chat, tmp_path):
    session, config = chat
    directory = tmp_path / "outside"
    directory.mkdir()
    result = AddDirectoryTool().run(ToolContext(config.workspace), path=str(directory))
    assert not result.ok
    assert config.writable_paths == ()
    assert "add_directory" not in default_tools({"read_file"}).names()


def test_delegation_uses_updated_caller_paths_but_keeps_peer_authorization(chat, tmp_path):
    session, config = chat
    directory = tmp_path / "extra"
    directory.mkdir()
    peer = Mock()
    peer.name = "peer"
    peer.run_task.return_value = {"kind": "task", "status": {"state": TASK_COMPLETED}, "artifacts": []}
    pool = Mock()
    pool.available.return_value = []
    pool.connect.return_value = peer
    delegation = DelegateTaskTool(replace(config, max_delegation_depth=2), pool=pool)
    # The Tool was constructed before the new directory was added.
    session.add_directory(str(directory))
    result = delegation.run(context(session.agent), task="work", workspace=str(directory))
    assert result.ok
    assert peer.run_task.call_args.kwargs["grant"].workspace == directory


def test_shared_chat_harness_keeps_memory_and_paths_across_profile_switches(tmp_path):
    from little_agent import agents
    from little_agent.control import StopController
    from little_agent.cli import build_parser
    config = AgentConfig(model="mock", workspace=tmp_path, require_confirmation=False,
                         openai_api_key=None, openai_base_url="unused", max_delegation_depth=0)
    memory = NullMemoryStore()
    ctx = build_chat_context(config, agents.default_profile(config), lambda *_: True, StopController("x"), memory)
    external = tmp_path / "extra"
    external.mkdir()
    ctx.session.add_directory(str(external))
    assert ctx.activate("default")
    assert ctx.session.memory is memory
    assert build_parser().parse_args(["gui", "--no-memory"]).no_memory
