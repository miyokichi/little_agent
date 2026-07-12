from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent import agents
from little_agent.agent import Agent
from little_agent.config import AgentConfig
from little_agent.skills.loader import SkillLoader
from little_agent.tools import default_tools

LIBRARY = Path("skills").resolve()


def _config(workspace: Path, agents_dir: Path) -> AgentConfig:
    return AgentConfig(
        model="local",
        workspace=workspace.resolve(),
        require_confirmation=False,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
        skill_library_dir=LIBRARY,
        agents_dir=agents_dir.resolve(),
    )


class DefaultToolsFilterTests(unittest.TestCase):
    def test_none_registers_all_core_tools(self) -> None:
        self.assertIn("run_powershell", default_tools().names())

    def test_allowlist_registers_only_named_tools(self) -> None:
        registry = default_tools({"read_file", "list_dir"})
        self.assertEqual(set(registry.names()), {"read_file", "list_dir"})


class ProfileFilesystemTests(unittest.TestCase):
    def test_create_agent_copies_only_requested_skills_and_leaves_library(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            profile = agents.create_agent(
                agents_dir, LIBRARY, "Office Bot", description="office", skills=["project_manager"]
            )

            self.assertEqual(profile.name, "office-bot")
            self.assertTrue((profile.skills_dir / "project_manager" / "SKILL.md").exists())
            self.assertFalse((profile.skills_dir / "datetime").exists())
            self.assertEqual(profile.enabled_skills(), ["project_manager"])
            # Library is untouched.
            self.assertTrue((LIBRARY / "project_manager").exists())
            self.assertTrue((LIBRARY / "file_manager").exists())

    def test_load_profile_round_trips(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            agents.create_agent(
                agents_dir, LIBRARY, "reader", description="d", core_tools=["read_file"]
            )
            loaded = agents.load_profile(agents_dir, "reader")
            self.assertEqual(loaded.description, "d")
            self.assertEqual(loaded.core_tools, ["read_file"])
            self.assertEqual(loaded.core_tools_set(), {"read_file"})

    def test_add_remove_and_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            agents.create_agent(agents_dir, LIBRARY, "a", skills=["project_manager"])
            agents.add_skill(agents_dir, LIBRARY, "a", "datetime")
            self.assertEqual(set(agents.load_profile(agents_dir, "a").enabled_skills()), {"project_manager", "datetime"})

            agents.remove_skill(agents_dir, "a", "datetime")
            self.assertEqual(agents.load_profile(agents_dir, "a").enabled_skills(), ["project_manager"])

            agents.delete_agent(agents_dir, "a")
            self.assertFalse(agents.profile_exists(agents_dir, "a"))

    def test_create_rejects_unknown_library_skill(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            with self.assertRaisesRegex(ValueError, "Skill not found in library"):
                agents.create_agent(agents_dir, LIBRARY, "bad", skills=["does_not_exist"])

    def test_skill_name_traversal_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            with self.assertRaises(ValueError):
                agents.create_agent(agents_dir, LIBRARY, "x", skills=["../secret"])

    def test_resolve_active(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            config = _config(root, agents_dir)
            # No request / reserved names resolve to the built-in default agent.
            self.assertEqual(agents.resolve_active(config, None).name, agents.DEFAULT_AGENT_NAME)
            self.assertTrue(agents.resolve_active(config, "default").builtin)
            self.assertTrue(agents.resolve_active(config, "library").builtin)
            with self.assertRaises(FileNotFoundError):
                agents.resolve_active(config, "missing")
            agents.create_agent(agents_dir, LIBRARY, "here", skills=["datetime"])
            self.assertEqual(agents.resolve_active(config, "here").name, "here")

    def test_default_profile_uses_library(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            default = agents.default_profile(_config(root, root / "agents"))
            self.assertTrue(default.builtin)
            self.assertEqual(default.skills_dir, LIBRARY)
            self.assertIn("datetime", default.enabled_skills())
            self.assertIsNone(default.core_tools_set())

    def test_create_rejects_reserved_name(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "reserved"):
                agents.create_agent(Path(tmp) / "agents", LIBRARY, "default", skills=["datetime"])


class AgentToolFilteringTests(unittest.TestCase):
    def test_agent_applies_core_allowlist_and_keeps_skill_and_memory_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            profile = agents.create_agent(
                agents_dir, LIBRARY, "tooled", skills=["project_manager"], core_tools=["read_file"]
            )
            config = _config(root, agents_dir)
            loader = SkillLoader(profile.skills_dir)
            agent = Agent(config, loader, core_tools=profile.core_tools_set())
            names = set(agent.tools.names())

            self.assertIn("read_file", names)          # allowed core tool
            self.assertNotIn("run_powershell", names)  # filtered-out core tool
            self.assertIn("add_task", names)           # from the copied skill
            self.assertIn("update_workspace_memory", names)  # memory tools always on

    def test_no_agent_fallback_uses_full_library_and_all_core_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            config = _config(root, agents_dir)
            # With no agent chosen, resolve_active yields the built-in default
            # (the whole library), which build_agent turns into a full-library agent.
            profile = agents.resolve_active(config, config.active_agent)
            self.assertEqual(profile.name, agents.DEFAULT_AGENT_NAME)
            self.assertTrue(profile.builtin)
            agent = Agent(config, SkillLoader(profile.skills_dir), core_tools=profile.core_tools_set())
            names = set(agent.tools.names())
            self.assertIn("run_powershell", names)
            self.assertIn("add_task", names)  # full library skills loaded


class AgentManagerScriptTests(unittest.TestCase):
    SCRIPT = Path("skills/agent_manager/scripts/agent_manager.py").resolve()

    def _run(self, tool: str, workspace: Path, arguments: dict) -> dict:
        payload = {"tool": tool, "workspace": str(workspace), "arguments": arguments}
        completed = subprocess.run(
            [sys.executable, str(self.SCRIPT), tool],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip())

    def _seed_library(self, workspace: Path) -> None:
        mini = workspace / "skills" / "mini"
        mini.mkdir(parents=True)
        (mini / "SKILL.md").write_text(
            "# mini\n\n## Description\nmini\n\n## When to use\n- t\n\n## Allowed tools\n- none\n\n## Instructions\n- t\n",
            encoding="utf-8",
        )

    def test_create_list_show_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed_library(workspace)

            created = self._run("create_agent", workspace, {"name": "Office", "skills": ["mini"]})
            self.assertTrue(created["ok"], created)
            self.assertTrue((workspace / "agents" / "office" / "skills" / "mini" / "SKILL.md").exists())

            listed = self._run("list_agents", workspace, {})
            self.assertIn("office", listed["content"])

            shown = self._run("show_agent", workspace, {"name": "office"})
            self.assertIn("mini", shown["content"])

            deleted = self._run("delete_agent", workspace, {"name": "office"})
            self.assertTrue(deleted["ok"])
            self.assertFalse((workspace / "agents" / "office").exists())

    def test_create_rejects_unknown_skill(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed_library(workspace)
            result = self._run("create_agent", workspace, {"name": "x", "skills": ["nope"]})
            self.assertFalse(result["ok"])
            self.assertIn("Skill not found", result["content"])


if __name__ == "__main__":
    unittest.main()
