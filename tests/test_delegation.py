from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent import agents
from little_agent.cli import build_agent
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.tools.base import ToolContext
from little_agent.tools.delegation import DelegateTaskTool

LIBRARY = Path("skills").resolve()


class FakeAgent:
    def __init__(self, reply: str = "done") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def run(self, user_text: str) -> str:
        self.prompts.append(user_text)
        return self.reply


def _config(workspace: Path, agents_dir: Path, max_depth: int = 2) -> AgentConfig:
    return AgentConfig(
        model="local",
        workspace=workspace.resolve(),
        require_confirmation=False,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
        skill_library_dir=LIBRARY,
        agents_dir=agents_dir.resolve(),
        max_delegation_depth=max_depth,
    )


class DelegateToolUnitTests(unittest.TestCase):
    def test_runs_sub_agent_and_returns_result(self) -> None:
        fake = FakeAgent("finished the research")
        tool = DelegateTaskTool(spawn=lambda name: fake)
        result = tool.run(ToolContext(Path.cwd()), task="research X", background="use Y")

        self.assertTrue(result.ok)
        self.assertIn("finished the research", result.content)
        # Context is prepended and the sub-agent receives a self-contained prompt.
        self.assertIn("research X", fake.prompts[0])
        self.assertIn("use Y", fake.prompts[0])

    def test_empty_task_is_rejected(self) -> None:
        tool = DelegateTaskTool(spawn=lambda name: FakeAgent())
        self.assertFalse(tool.run(ToolContext(Path.cwd()), task="  ").ok)

    def test_depth_limit_blocks_further_delegation(self) -> None:
        tool = DelegateTaskTool(spawn=lambda name: FakeAgent(), depth=2, max_depth=2)
        result = tool.run(ToolContext(Path.cwd()), task="anything")
        self.assertFalse(result.ok)
        self.assertIn("depth limit", result.content)

    def test_unknown_agent_lists_available(self) -> None:
        def spawn(name):
            raise FileNotFoundError(name)

        tool = DelegateTaskTool(spawn=spawn, available_agents=lambda: ["office", "coder"])
        result = tool.run(ToolContext(Path.cwd()), task="t", agent="ghost")
        self.assertFalse(result.ok)
        self.assertIn("office", result.content)
        self.assertIn("coder", result.content)

    def test_description_lists_profiles(self) -> None:
        tool = DelegateTaskTool(spawn=lambda name: FakeAgent(), available_agents=lambda: ["office"])
        self.assertIn("office", tool.description)


class StopControllerChildTests(unittest.TestCase):
    def test_child_shares_trigger_flag(self) -> None:
        parent = StopController("<ctrl>+<alt>+q")
        child = parent.child()
        # A stop during the sub-agent's run must be visible to the parent too.
        parent._on_activate()
        self.assertTrue(parent.triggered)
        self.assertTrue(child.triggered)

    def test_child_reset_is_noop(self) -> None:
        parent = StopController("<ctrl>+<alt>+q")
        child = parent.child()
        parent._on_activate()
        child.reset()  # sub-agent's Agent.run() calls reset(); it must not clear the parent's flag
        self.assertTrue(parent.triggered)

    def test_child_arm_does_not_register_listener(self) -> None:
        parent = StopController("<ctrl>+<alt>+q")
        child = parent.child()
        child.arm()  # must stay a no-op: the parent owns the single listener
        self.assertIsNone(child._listener)


class BuildAgentDelegationTests(unittest.TestCase):
    def test_delegate_tool_registered_at_depth_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            stop = StopController(config.stop_hotkey)
            agent = build_agent(config, agents.default_profile(config), lambda *_: True, stop)
            self.assertIn("delegate_task", agent.tools.names())

    def test_delegate_tool_absent_when_depth_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=0)
            stop = StopController(config.stop_hotkey)
            agent = build_agent(config, agents.default_profile(config), lambda *_: True, stop)
            self.assertNotIn("delegate_task", agent.tools.names())

    def test_spawned_sub_agent_runs_and_returns_text(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            stop = StopController(config.stop_hotkey)
            agent = build_agent(config, agents.default_profile(config), lambda *_: True, stop)
            tool = agent.tools.get("delegate_task")
            # No API key -> LocalRuleClient, so the sub-agent runs without network.
            result = tool.run(ToolContext(config.workspace), task="list the current directory")
            self.assertTrue(result.ok, result.content)
            self.assertIn("sub-agent 'default' result", result.content)


if __name__ == "__main__":
    unittest.main()
