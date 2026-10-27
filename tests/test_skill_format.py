"""Agent Skills format: SKILL.md frontmatter, progressive disclosure, activation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent import agents
from little_agent.agent import Agent
from little_agent.commands import CommandContext, CommandRegistry
from little_agent.config import AgentConfig, builtin_skills_dir
from little_agent.control import StopController
from little_agent.factory import build_agent
from little_agent.messages import Message
from little_agent.session import ChatSession
from little_agent.skills.activation import ActivateSkillTool
from little_agent.skills.frontmatter import FrontmatterError, parse_frontmatter
from little_agent.skills.loader import SkillLoader, name_problems, parse_skill_file
from little_agent.tools.base import ToolContext, ToolRegistry

LIBRARY = builtin_skills_dir()


def write_skill(root: Path, folder: str, text: str, files: dict[str, str] | None = None) -> Path:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    for relative, content in (files or {}).items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return directory


PDF_SKILL = """
    ---
    name: pdf-processing
    description: Extract PDF text, fill forms, merge files. Use when handling PDFs.
    license: Apache-2.0
    compatibility: Requires poppler
    metadata:
      author: example-org
      version: "1.0"
    allowed-tools: read_file list_dir
    ---
    # PDF Processing

    Read [the reference](references/REFERENCE.md) before filling forms.
"""


def _config(workspace: Path, **overrides) -> AgentConfig:
    base = dict(
        model="local",
        workspace=workspace.resolve(),
        require_confirmation=False,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
        skill_library_dir=LIBRARY,
        agents_dir=(workspace / "agents").resolve(),
        max_delegation_depth=0,
    )
    base.update(overrides)
    return AgentConfig(**base)  # type: ignore[arg-type]


class FrontmatterTests(unittest.TestCase):
    def test_spec_fields_and_yaml_forms(self) -> None:
        fields, body, present = parse_frontmatter(
            textwrap.dedent(
                """\
                ---
                # a comment
                name: demo
                description: >
                  Folded over
                  two lines.
                literal: |
                  line one
                  line two
                quoted: "a \\"b\\" c"
                single: 'it''s'
                flow: [a, "b, c"]
                block:
                  - x
                  - y
                metadata:
                  author: me
                  version: "1.0"
                plain: continued
                  onto the next line
                number: 3
                ---
                # Body
                """
            )
        )
        self.assertTrue(present)
        self.assertEqual(fields["name"], "demo")
        self.assertEqual(fields["description"], "Folded over two lines.\n")
        self.assertEqual(fields["literal"], "line one\nline two\n")
        self.assertEqual(fields["quoted"], 'a "b" c')
        self.assertEqual(fields["single"], "it's")
        self.assertEqual(fields["flow"], ["a", "b, c"])
        self.assertEqual(fields["block"], ["x", "y"])
        self.assertEqual(fields["metadata"], {"author": "me", "version": "1.0"})
        self.assertEqual(fields["plain"], "continued onto the next line")
        self.assertEqual(fields["number"], 3)
        self.assertEqual(body, "# Body")

    def test_unquoted_colon_is_read_leniently(self) -> None:
        fields, _body, _ = parse_frontmatter("---\ndescription: Use this skill when: the user asks\n---\n")
        self.assertEqual(fields["description"], "Use this skill when: the user asks")

    def test_no_frontmatter_and_unclosed(self) -> None:
        self.assertEqual(parse_frontmatter("# Title\n"), ({}, "# Title\n", False))
        with self.assertRaises(FrontmatterError):
            parse_frontmatter("---\nname: x\n")


class ParseTests(unittest.TestCase):
    def test_full_skill(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = write_skill(Path(tmp), "pdf-processing", PDF_SKILL)
            notes: list = []
            skill = parse_skill_file(directory / "SKILL.md", notes)
        assert skill is not None
        self.assertEqual(notes, [])
        self.assertEqual(skill.name, "pdf-processing")
        self.assertEqual(skill.license, "Apache-2.0")
        self.assertEqual(skill.compatibility, "Requires poppler")
        self.assertEqual(skill.metadata, {"author": "example-org", "version": "1.0"})
        self.assertEqual(skill.allowed_tools, ("read_file", "list_dir"))
        self.assertTrue(skill.body.startswith("# PDF Processing"))
        self.assertNotIn("---", skill.body)
        self.assertFalse(skill.legacy)

    def test_name_rules(self) -> None:
        self.assertEqual(name_problems("pdf-processing", "pdf-processing"), [])
        for bad in ("PDF-Processing", "-pdf", "pdf-", "pdf--processing", "pdf_processing", "x" * 65):
            self.assertTrue(name_problems(bad, bad), bad)
        self.assertTrue(any("does not match" in p for p in name_problems("a", "b")))

    def test_lenient_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mismatched = write_skill(root, "folder", "---\nname: Other_Name\ndescription: d\nextra: 1\n---\nbody")
            no_description = write_skill(root, "nodesc", "---\nname: nodesc\n---\nbody")
            broken = write_skill(root, "broken", "---\nname: broken\ndescription: never closed\n")
            notes: list = []
            loaded = parse_skill_file(mismatched / "SKILL.md", notes)
            self.assertIsNotNone(loaded)  # warned, still loaded
            messages = " ".join(str(note) for note in notes)
            self.assertIn("does not match", messages)
            self.assertIn("lowercase", messages)
            self.assertIn("unknown frontmatter field(s): extra", messages)
            self.assertTrue(all(note.level == "warning" for note in notes))

            notes = []
            self.assertIsNone(parse_skill_file(no_description / "SKILL.md", notes))
            self.assertEqual(notes[0].level, "error")
            notes = []
            self.assertIsNone(parse_skill_file(broken / "SKILL.md", notes))
            self.assertIn("cannot be parsed", str(notes[0]))

    def test_legacy_heading_format_still_loads(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = write_skill(
                Path(tmp),
                "old_style",
                """
                # old_style

                ## Description
                Does old things.

                ## When to use
                - when it is old

                ## Allowed tools
                - read_file

                ## Instructions
                Step one.
                """,
            )
            notes: list = []
            skill = parse_skill_file(directory / "SKILL.md", notes)
        assert skill is not None
        self.assertTrue(skill.legacy)
        self.assertEqual(skill.description, "Does old things. Use when: when it is old")
        self.assertEqual(skill.allowed_tools, ("read_file",))
        self.assertEqual(skill.body, "Step one.")
        self.assertIn("legacy", str(notes[0]))


class LoaderTests(unittest.TestCase):
    def test_first_root_wins_and_shadowing_is_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            project, user = Path(tmp) / "project", Path(tmp) / "user"
            write_skill(project, "demo", "---\nname: demo\ndescription: project copy\n---\n")
            write_skill(user, "demo", "---\nname: demo\ndescription: user copy\n---\n")
            loader = SkillLoader([project, user])
            self.assertEqual([s.description for s in loader.load_all()], ["project copy"])
            self.assertIn("shadowed", str(loader.diagnostics()[0]))

    def test_profile_names_match_old_underscore_folders(self) -> None:
        loader = SkillLoader(LIBRARY, names={"excel_file", "file-manager"})
        self.assertEqual([s.name for s in loader.load_all()], ["excel-file", "file-manager"])
        self.assertIsNotNone(SkillLoader(LIBRARY).get("workspace_harness"))

    def test_catalog_is_metadata_only_and_omitted_when_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "pdf-processing", PDF_SKILL)
            write_skill(root, "xml-ish", "---\nname: xml-ish\ndescription: Handles <tags> & stuff\n---\nSECRET BODY")
            catalog = SkillLoader(root).catalog_prompt()
            self.assertIn("<available_skills>", catalog)
            self.assertIn("<name>pdf-processing</name>", catalog)
            self.assertIn(f"<location>{(root / 'pdf-processing' / 'SKILL.md').resolve()}</location>", catalog)
            self.assertIn("&lt;tags&gt; &amp; stuff", catalog)
            self.assertNotIn("SECRET BODY", catalog)
            self.assertEqual(SkillLoader(root / "missing").catalog_prompt(), "")

    def test_builtin_skills_follow_the_spec(self) -> None:
        loader = SkillLoader(LIBRARY)
        skills = loader.load_all()
        self.assertGreaterEqual(len(skills), 14)
        self.assertEqual([str(note) for note in loader.diagnostics()], [])
        for skill in skills:
            self.assertEqual(name_problems(skill.name, skill.folder), [], skill.name)
            self.assertLessEqual(len(skill.description), 1024, skill.name)
            self.assertFalse(skill.legacy, skill.name)
            self.assertTrue((LIBRARY / skill.folder / "SKILL.md").read_text(encoding="utf-8").startswith("---"))


class ActivationTests(unittest.TestCase):
    def test_activation_returns_body_directory_and_resources(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = write_skill(
                root,
                "pdf-processing",
                PDF_SKILL,
                {"references/REFERENCE.md": "ref", "scripts/extract.py": "print()"},
            )
            (directory / "__pycache__").mkdir()
            (directory / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
            tool = ActivateSkillTool(SkillLoader(root))
            self.assertEqual(tool.parameters["properties"]["name"]["enum"], ["pdf-processing"])
            result = tool.run(ToolContext(workspace=root), name="pdf-processing")
            self.assertTrue(result.ok)
            content = result.content
            self.assertTrue(content.startswith('<skill_content name="pdf-processing">'))
            self.assertIn("# PDF Processing", content)
            self.assertIn(f"Skill directory: {directory.resolve()}", content)
            self.assertIn("<file>references/REFERENCE.md</file>", content)
            self.assertIn("<file>scripts/extract.py</file>", content)
            self.assertNotIn("SKILL.md</file>", content)
            self.assertNotIn("__pycache__", content)
            self.assertIn("Compatibility: Requires poppler", content)

            again = tool.run(ToolContext(workspace=root), name="pdf-processing")
            self.assertIn("already active", again.content)
            tool.begin_run()
            self.assertIn("<skill_content", tool.run(ToolContext(workspace=root), name="pdf-processing").content)
            self.assertFalse(tool.run(ToolContext(workspace=root), name="nope").ok)


class ScriptedLLM:
    """Activates a skill, reads its reference, then answers with what it read."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.tool_names: list[list[str]] = []

    def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict:
        self.calls.append([*messages])
        self.tool_names.append(tools.names())
        results = [message for message in messages if message.role == "tool"]
        if not results:
            return {"content": "", "finish_reason": "tool_calls", "tool_calls": [{"id": "a", "name": "activate_skill", "arguments": {"name": "pdf-processing"}}]}
        if len(results) == 1:
            directory = results[0].content.split("Skill directory: ")[1].splitlines()[0]
            return {
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {"id": "r", "name": "read_file", "arguments": {"path": f"{directory}/references/REFERENCE.md"}},
                    {"id": "w", "name": "write_file", "arguments": {"path": f"{directory}/hack.md", "content": "x"}},
                ],
            }
        return {"content": "\n---\n".join(str(message.content) for message in results[1:]), "tool_calls": [], "finish_reason": "stop"}


class AgentTests(unittest.TestCase):
    def test_progressive_disclosure_in_the_agent_loop(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            library = Path(tmp) / "library"
            write_skill(library, "pdf-processing", PDF_SKILL, {"references/REFERENCE.md": "FORM RULES"})
            config = _config(workspace, skill_library_dir=library)
            llm = ScriptedLLM()
            agent = Agent(config, SkillLoader(library), llm=llm)
            result = agent.run("fill in this pdf form")

            system = llm.calls[0][0].content
            self.assertIn("<available_skills>", system)
            self.assertIn("pdf-processing", system)
            self.assertNotIn("Read [the reference]", system)  # body is not loaded up front
            self.assertIn("activate_skill", llm.tool_names[0])
            self.assertIn("FORM RULES", result.text)  # references are readable
            self.assertIn("outside allowed write paths", result.text)  # but not writable
            self.assertFalse((library / "pdf-processing" / "hack.md").exists())

    def test_no_skills_means_no_catalog_and_no_tool(self) -> None:
        with TemporaryDirectory() as tmp:
            llm = ScriptedLLM()
            agent = Agent(_config(Path(tmp)), SkillLoader(Path(tmp) / "none"), llm=llm)
            self.assertNotIn("activate_skill", agent.tools.names())
            llm.complete = lambda model, messages, tools: (llm.calls.append(messages) or {"content": "ok", "tool_calls": [], "finish_reason": "stop"})  # type: ignore[method-assign]
            agent.run("hello")
            self.assertNotIn("available_skills", llm.calls[0][0].content)

    def test_slash_skill_and_skills_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            library = workspace / "library"
            write_skill(library, "pdf-processing", PDF_SKILL)
            write_skill(library, "Bad_Name", "---\nname: Bad_Name\ndescription: warned\n---\n")
            config = _config(workspace, skill_library_dir=library)
            agent = build_agent(config, agents.default_profile(config), None, StopController("x"))
            registry = CommandRegistry(workspace / "commands", workspace / "global")
            ctx = CommandContext(session=ChatSession(agent), registry=registry)

            prompt = registry.dispatch(ctx, "/pdf-processing merge a.pdf and b.pdf").agent_prompt
            self.assertTrue(prompt.startswith('<skill_content name="pdf-processing">'))
            self.assertTrue(prompt.endswith("merge a.pdf and b.pdf"))
            self.assertIn("Apache-2.0", registry.dispatch(ctx, "/skills pdf-processing").output)
            listing = registry.dispatch(ctx, "/skills").output
            self.assertIn("pdf-processing", listing)
            self.assertIn("/skills check", listing)
            self.assertIn("lowercase", registry.dispatch(ctx, "/skills check").output)
            self.assertIn("/<skill-name>", registry.dispatch(ctx, "/help").output)
            self.assertIn("Unknown command", registry.dispatch(ctx, "/no-such-skill").output)


class DiscoveryTests(unittest.TestCase):
    def test_agents_skills_directories_are_layered(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project, user = root / "ws" / ".agents" / "skills", root / "home" / ".agents" / "skills"
            write_skill(project, "datetime", "---\nname: datetime\ndescription: project override\n---\n")
            write_skill(user, "datetime", "---\nname: datetime\ndescription: user copy\n---\n")
            write_skill(user, "user-only", "---\nname: user-only\ndescription: from the user\n---\n")
            config = _config(root / "ws", shared_skill_dirs=(project, user))
            profile = agents.default_profile(config)
            self.assertEqual(profile.skill_roots(), [project, user, LIBRARY])
            loader = SkillLoader(profile.skill_roots())
            self.assertEqual(loader.get("datetime").description, "project override")
            self.assertIsNotNone(loader.get("user-only"))
            self.assertIsNotNone(loader.get("git"))  # built-ins are still there

            workspace_library = _config(root / "ws", skill_library_dir=root / "lib", shared_skill_dirs=(project, user))
            self.assertEqual(agents.default_profile(workspace_library).skill_roots(), [project, root / "lib", user])

    def test_from_env_shared_dirs(self) -> None:
        keys = ("LITTLE_AGENT_WORKSPACE", "LITTLE_AGENT_SHARED_SKILLS")
        saved = {key: os.environ.get(key) for key in keys}
        try:
            with TemporaryDirectory() as tmp:
                os.environ["LITTLE_AGENT_WORKSPACE"] = tmp
                os.environ.pop("LITTLE_AGENT_SHARED_SKILLS", None)
                config = AgentConfig.from_env()
                self.assertEqual(
                    config.shared_skill_dirs,
                    ((Path(tmp) / ".agents" / "skills").resolve(), (Path.home() / ".agents" / "skills").resolve()),
                )
                os.environ["LITTLE_AGENT_SHARED_SKILLS"] = "false"
                self.assertEqual(AgentConfig.from_env().shared_skill_dirs, ())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class SkillCreatorTests(unittest.TestCase):
    SCRIPT = LIBRARY / "skill-creator" / "scripts" / "skill_creator.py"

    def _call(self, workspace: Path, tool: str, arguments: dict) -> dict:
        payload = {"tool": tool, "workspace": str(workspace), "arguments": arguments}
        done = subprocess.run(
            [sys.executable, str(self.SCRIPT), tool], input=json.dumps(payload), capture_output=True, text=True, check=True
        )
        return json.loads(done.stdout)

    def test_created_skill_is_spec_valid(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            created = self._call(workspace, "create_skill", {"name": "My Cool_Skill", "description": "Does cool things. Use when cool things are needed."})
            self.assertTrue(created["ok"], created)
            self.assertTrue(self._call(workspace, "validate_skill", {"name": "my-cool-skill"})["ok"])
            loader = SkillLoader(workspace / "skills")
            self.assertEqual([s.name for s in loader.load_all()], ["my-cool-skill"])
            self.assertEqual(loader.diagnostics(), [])

            (workspace / "skills" / "my-cool-skill" / "SKILL.md").write_text(
                "---\nname: Wrong\ndescription: x\n---\n", encoding="utf-8"
            )
            report = self._call(workspace, "validate_skill", {"name": "my-cool-skill"})
            self.assertFalse(report["ok"])
            self.assertIn("must match the directory", report["content"])


if __name__ == "__main__":
    unittest.main()
