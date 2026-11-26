"""Standard Agent Skills loading, progressive disclosure, and legacy migration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from little_agent.agent import Agent
from little_agent.config import AgentConfig, builtin_skills_dir, resolve_skill_library
from little_agent.session import ChatSession
from little_agent.skills.frontmatter import metadata_issues, parse_frontmatter
from little_agent.skills.loader import SkillLoader
from little_agent.skills.tools import ReadSkillTool
from little_agent.tools.base import ToolContext


def make_skill(root, name="via-analysis", description="Compare via geometry and model applicability.", body="Read references/shape.md before calculating.", **fields):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "description": description, **fields}
    (folder / "SKILL.md").write_text("---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + "---\n\n" + body, encoding="utf-8")
    return folder


def make_agent(workspace, root, llm, names=None):
    workspace.mkdir(exist_ok=True)
    config = AgentConfig(model="scripted", workspace=workspace, require_confirmation=False,
        openai_api_key=None, openai_base_url="unused", enable_logging=False)
    agent = Agent(config, SkillLoader(root, names=names), llm=llm)
    agent.stop.arm = lambda: None
    agent.stop.disarm = lambda: None
    return agent


class Scripted:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []
        self.schemas = []
    def complete(self, model, messages, tools):
        self.prompts.append([m.content for m in messages])
        self.schemas.append(tools.openai_schemas())
        return next(self.responses)


def calls(*items):
    return {"content": "", "tool_calls": [{"name": n, "arguments": args} for n, args in items]}


def test_yaml_multiline_metadata_and_arbitrary_body_are_preserved(tmp_path):
    folder = make_skill(tmp_path)
    text = '''---
name: via-analysis
description: >-
  Compare cross sections: tapered
  and vertical sidewalls.
license: MIT
compatibility: Python 3.10 or later
metadata:
  owner: "専門家"
  version: "1.0"
allowed-tools: Read Bash(python:*)
---
# Any heading

A paragraph before any step.

## Examples
- Sidewall angle is 83 degrees.

## Exceptions
Do not generalize unknown material resistivity.
'''
    (folder / "SKILL.md").write_text(text)
    skill = SkillLoader(tmp_path).load_all()[0]
    assert skill.name == "via-analysis"
    assert skill.description == "Compare cross sections: tapered and vertical sidewalls."
    assert skill.metadata == {"owner": "専門家", "version": "1.0"}
    assert skill.allowed_tools == ["Read", "Bash(python:*)"]
    assert "paragraph before any step" in skill.instructions and "## Exceptions" in skill.instructions
    assert "Python 3.10" in skill.as_prompt() and "MIT" in skill.as_prompt()
    assert not skill.legacy
    assert SkillLoader(tmp_path).load_tools() == []  # tools.json is NOT required


@pytest.mark.parametrize("frontmatter", [
    "name: via-analysis\n", "description: useful\n", "name: via-analysis\ndescription: ''\n",
    "name: via-analysis\ndescription: [a, b]\n", "name: via-analysis\ndescription: 42\n",
    "name: via-analysis\ndescription: useful\nmetadata: [x]\n",
    "name: via-analysis\ndescription: useful\nmetadata: {version: 1}\n",
    "name: via-analysis\ndescription: useful\nallowed-tools: [Read]\n",
    "name: via-analysis\ndescription: useful\nname: duplicate\n",
    "name: via-analysis\ndescription: !!python/object/apply:os.system ['false']\n",
    "name: [not closed\n",
])
def test_invalid_frontmatter_is_diagnosed_and_skipped(tmp_path, frontmatter):
    folder = tmp_path / "via-analysis"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\n" + frontmatter + "---\n\nBody")
    loader = SkillLoader(tmp_path)
    assert loader.load_all() == []
    assert "Skipped skill" in loader.warning()


def test_cosmetic_name_errors_warn_without_losing_useful_skill(tmp_path):
    folder = make_skill(tmp_path, name="via-analysis")
    text = (folder / "SKILL.md").read_text().replace("name: via-analysis", "name: Different_Name")
    (folder / "SKILL.md").write_text(text)
    loader = SkillLoader(tmp_path)
    assert loader.load_all()[0].name == "Different_Name"
    assert "parent directory" in loader.warning()
    assert "lowercase" in loader.warning()


def test_legacy_format_keeps_all_content_and_warns(tmp_path):
    folder = tmp_path / "legacy_name"
    folder.mkdir()
    (folder / "SKILL.md").write_text("# legacy_name\n\n## Description\nOld skill.\n\n## Examples\nKEEP THIS EXAMPLE\n\n## Instructions\nDo it.")
    loader = SkillLoader(tmp_path)
    skill = loader.load_all()[0]
    assert skill.name == "legacy_name" and skill.legacy
    assert "KEEP THIS EXAMPLE" in skill.as_prompt()
    assert "migrate to YAML" in loader.warning()


def test_initial_prompt_is_catalog_only_and_model_can_activate_any_skill(tmp_path):
    library = tmp_path / "library"
    first = make_skill(library, body="UNIQUE FULL INSTRUCTIONS")
    (first / "references").mkdir()
    (first / "references" / "shape.md").write_text("ACTUAL REFERENCE EVIDENCE")
    for n in range(6):
        make_skill(library, name=f"other-{n}", description=f"Other domain {n}.", body=f"UNLOADED BODY {n}")
    llm = Scripted(calls(("read_skill", {"name": "via-analysis"})),
                   calls(("read_skill", {"name": "via-analysis", "path": "references/shape.md"})),
                   {"content": "Evidence checked", "tool_calls": []})
    agent = make_agent(tmp_path / "workspace", library, llm)
    result = agent.run("Judge an unfamiliar task without matching literal keywords")
    initial = llm.prompts[0][0]
    assert "via-analysis" in initial and "other-5" in initial
    assert "UNIQUE FULL INSTRUCTIONS" not in initial and "UNLOADED BODY" not in initial
    assert "UNIQUE FULL INSTRUCTIONS" in str(llm.prompts[1])
    assert "references/shape.md" in str(llm.prompts[1])
    assert "ACTUAL REFERENCE EVIDENCE" not in str(llm.prompts[1])
    assert "ACTUAL REFERENCE EVIDENCE" in str(llm.prompts[2])
    assert "UNLOADED BODY" not in str(llm.prompts[2])
    assert result.skills == ("via-analysis",)
    assert "read_skill" not in agent.tools.names()  # run state never leaks


def test_activation_stays_in_chat_but_not_independent_tasks(tmp_path):
    library = tmp_path / "library"
    make_skill(library, body="DURABLE SKILL INSTRUCTIONS")
    llm = Scripted(calls(("read_skill", {"name": "via-analysis"})),
        {"content": "first", "tool_calls": []}, {"content": "second", "tool_calls": []},
        {"content": "independent", "tool_calls": []}, {"content": "cleared", "tool_calls": []})
    agent = make_agent(tmp_path / "workspace", library, llm)
    chat = ChatSession(agent)
    chat.send("Investigate")
    chat.send("Continue")
    assert "DURABLE SKILL INSTRUCTIONS" in llm.prompts[2][0]
    agent.run("Independent A2A-style task")
    assert "DURABLE SKILL INSTRUCTIONS" not in llm.prompts[3][0]
    chat.clear()
    chat.send("New conversation")
    assert "DURABLE SKILL INSTRUCTIONS" not in llm.prompts[4][0]


def test_explicit_activation_and_profile_filtering(tmp_path):
    library = tmp_path / "library"
    make_skill(library, body="EXPLICITLY LOADED")
    make_skill(library, name="hidden", body="HIDDEN INSTRUCTIONS")
    llm = Scripted({"content": "done", "tool_calls": []})
    agent = make_agent(tmp_path / "workspace", library, llm, names={"via_analysis"})
    agent.run("$via-analysis do this")
    assert "EXPLICITLY LOADED" in llm.prompts[0][0]
    assert "HIDDEN INSTRUCTIONS" not in llm.prompts[0][0]
    schema = next(s for s in llm.schemas[0] if s["function"]["name"] == "read_skill")
    assert schema["function"]["parameters"]["properties"]["name"]["enum"] == ["via-analysis"]
    reader = ReadSkillTool(agent.skills.load_all(), lambda s: None)
    assert not reader.run(ToolContext(agent.config.workspace), name="hidden").ok


def test_no_catalog_and_no_activation_tool_for_empty_profile(tmp_path):
    make_skill(tmp_path / "library")
    llm = Scripted({"content": "done", "tool_calls": []})
    agent = make_agent(tmp_path / "workspace", tmp_path / "library", llm, names=set())
    agent.run("$via-analysis")
    assert "Available skills" not in llm.prompts[0][0]
    assert not any(s["function"]["name"] == "read_skill" for s in llm.schemas[0])


def test_resource_paths_cannot_escape_or_cross_profile_boundary(tmp_path):
    folder = make_skill(tmp_path / "library")
    outside = tmp_path / "secret.txt"
    outside.write_text("PRIVATE DATA")
    reader = ReadSkillTool(SkillLoader(tmp_path / "library").load_all(), lambda s: None)
    ctx = ToolContext(tmp_path / "unrelated-workspace")
    for path in ("../../secret.txt", str(outside)):
        result = reader.run(ctx, name="via-analysis", path=path)
        assert not result.ok and "PRIVATE DATA" not in result.content
    try:
        (folder / "linked.txt").symlink_to(outside)
    except OSError:
        return
    assert not reader.run(ctx, name="via-analysis", path="linked.txt").ok


def test_whole_folder_shadowing_does_not_inherit_lower_priority_code(tmp_path):
    high, low = tmp_path / "high", tmp_path / "low"
    make_skill(high, description="Project override")
    low_skill = make_skill(low)
    (low_skill / "tool.py").write_text("print('{}')")
    (low_skill / "tools.json").write_text(json.dumps({"tools": [{"name": "should_not_load", "description": "lower code", "script": "tool.py"}]}))
    loader = SkillLoader([high, low])
    assert loader.load_all()[0].description == "Project override"
    assert loader.load_tools() == []
    assert "Shadowed" in loader.warning()


def test_builtin_frontmatter_is_valid_and_old_profile_names_still_resolve():
    loader = SkillLoader(builtin_skills_dir())
    skills = loader.load_all()
    assert len(skills) == 14
    assert loader.warning() is None
    for skill in skills:
        data, body = parse_frontmatter(skill.path.read_text())
        assert not metadata_issues(data, skill.path.parent)
        assert body and not skill.legacy
    old = SkillLoader(builtin_skills_dir(), names={"excel_file", "workspace_harness"})
    assert {s.name for s in old.load_all()} == {"excel-file", "workspace-harness"}
    assert {t.name for t in old.load_tools()} >= {"read_excel", "harness_overview"}


def test_common_agents_directory_and_explicit_override(tmp_path):
    common = tmp_path / ".agents" / "skills"
    make_skill(common)
    assert resolve_skill_library(None, tmp_path) == common
    assert resolve_skill_library("custom", tmp_path) == tmp_path / "custom"


def test_creator_generates_standard_yaml_with_safe_description(tmp_path):
    tools = {t.name: t for t in SkillLoader(builtin_skills_dir()).load_tools()}
    ctx = ToolContext(tmp_path)
    description = 'Use for quoting: "shape"\nand multiline notes.'
    result = tools["create_skill"].run(ctx, name="My_New Skill", description=description)
    assert result.ok, result.content
    created = tmp_path / "skills" / "my-new-skill" / "SKILL.md"
    data, body = parse_frontmatter(created.read_text())
    assert data == {"name": "my-new-skill", "description": description}
    assert not metadata_issues(data, created.parent)
    assert tools["validate_skill"].run(ctx, name="my-new-skill").ok
    assert SkillLoader(tmp_path / "skills").load_all()[0].description == description


def test_script_tools_are_refreshed_without_rebuilding_the_agent(tmp_path):
    library = tmp_path / "library"
    folder = make_skill(library)
    llm = Scripted({"content": "before", "tool_calls": []}, {"content": "after", "tool_calls": []}, {"content": "removed", "tool_calls": []})
    agent = make_agent(tmp_path / "workspace", library, llm)
    agent.run("first")
    assert not any(s["function"]["name"] == "new_extension" for s in llm.schemas[0])
    (folder / "tool.py").write_text("print('{}')")
    (folder / "tools.json").write_text(json.dumps({"tools": [{"name": "new_extension", "description": "new", "script": "tool.py"}]}))
    agent.run("next")
    assert any(s["function"]["name"] == "new_extension" for s in llm.schemas[1])
    (folder / "tools.json").unlink()
    agent.run("third")
    assert not any(s["function"]["name"] == "new_extension" for s in llm.schemas[2])


def test_skill_command_loads_full_body_before_model_selection(tmp_path):
    from little_agent.commands import CommandContext, CommandRegistry
    library = tmp_path / "library"
    make_skill(library, body="USER CHOSE THESE INSTRUCTIONS")
    llm = Scripted({"content": "done", "tool_calls": []})
    agent = make_agent(tmp_path / "workspace", library, llm)
    session = ChatSession(agent)
    registry = CommandRegistry(tmp_path / "commands", tmp_path / "global")
    ctx = CommandContext(session, registry)
    result = registry.dispatch(ctx, "/skill via-analysis compare conditions")
    assert result.agent_prompt
    session.send(result.agent_prompt)
    assert "USER CHOSE THESE INSTRUCTIONS" in llm.prompts[0][0]
    assert "unavailable" in registry.dispatch(ctx, "/skill hidden").output


def test_name_collision_is_deterministic_and_reported(tmp_path):
    first = make_skill(tmp_path, name="a", description="first")
    second = make_skill(tmp_path, name="b", description="second")
    for folder in (first, second):
        text = (folder / "SKILL.md").read_text().replace(f"name: {folder.name}", "name: shared-name")
        (folder / "SKILL.md").write_text(text)
    loader = SkillLoader(tmp_path)
    assert [s.description for s in loader.load_all()] == ["first"]
    assert "Shadowed skill name" in loader.warning()


def test_manifest_refresh_does_not_replace_a_memory_tool_with_the_same_name(tmp_path):
    from little_agent.memory.store import NullMemoryStore
    from little_agent.tools.base import ToolResult
    library = tmp_path / "library"
    folder = make_skill(library)
    (folder / "tool.py").write_text("print('{}')")
    (folder / "tools.json").write_text(json.dumps({"tools": [{"name": "memory_probe", "description": "extension", "script": "tool.py"}]}))
    class MemoryTool:
        name = "memory_probe"
        description = "real memory tool"
        parameters = {"type": "object", "properties": {}}
        requires_confirmation = False
        def run(self, context, **kwargs):
            return ToolResult(True, "REAL MEMORY")
    protected = MemoryTool()
    class Memory(NullMemoryStore):
        def tools(self):
            return [protected]
    llm = Scripted(calls(("memory_probe", {})), {"content": "done", "tool_calls": []})
    config = AgentConfig(model="scripted", workspace=tmp_path, require_confirmation=False,
                         openai_api_key=None, openai_base_url="unused")
    agent = Agent(config, SkillLoader(library), llm=llm, memory=Memory())
    agent.stop.arm = lambda: None
    agent.stop.disarm = lambda: None
    agent.run("inspect")
    assert agent.tools.get("memory_probe") is protected
    assert "REAL MEMORY" in str(llm.prompts[1])
