"""Function tools: @tool, schema derivation, and loading tools from .py files."""

from __future__ import annotations

import json
import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, Optional

from little_agent import agents
from little_agent.agent import Agent
from little_agent.commands import CommandContext, CommandRegistry
from little_agent.config import AgentConfig, builtin_skills_dir, builtin_tools_dir
from little_agent.control import StopController
from little_agent.factory import build_agent
from little_agent.messages import Message
from little_agent.session import ChatSession
from little_agent.skills.loader import SkillLoader
from little_agent.tools import (
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    default_tools,
    load_function_tools,
    tool,
)
from little_agent.tools.function import parse_docstring

LIBRARY = builtin_skills_dir()


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
    )
    base.update(overrides)
    return AgentConfig(**base)  # type: ignore[arg-type]


def _write_tool_file(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


GREET = """
    from little_agent.tools import tool

    @tool
    def greet(name: str, excited: bool = False) -> str:
        \"\"\"Greet someone by name.

        Args:
            name: Who to greet.
            excited: Add an exclamation mark.
        \"\"\"
        return f"Hello, {name}{'!' if excited else '.'}"
"""


class SchemaTests(unittest.TestCase):
    def test_schema_comes_from_signature_and_docstring(self) -> None:
        @tool
        def convert(
            context: ToolContext,
            amount: float,
            unit: Literal["jpy", "usd"],
            tags: list[str],
            note: Optional[str] = None,
            precision: int = 2,
            label: Annotated[str, "Shown to the user."] = "",
        ) -> str:
            """Convert an amount between currencies.

            More detail that is not part of the summary.

            Args:
                amount: How much.
                unit: Target currency.
                tags: Free-form tags,
                    continued on a second line.
                note: Optional note.

            Returns:
                The converted amount.
            """
            return ""

        self.assertEqual(convert.name, "convert")
        self.assertEqual(convert.description, "Convert an amount between currencies.")
        self.assertFalse(convert.requires_confirmation)
        props = convert.parameters["properties"]
        self.assertNotIn("context", props)
        self.assertEqual(props["amount"], {"type": "number", "description": "How much."})
        self.assertEqual(props["unit"]["enum"], ["jpy", "usd"])
        self.assertEqual(props["unit"]["type"], "string")
        self.assertEqual(props["tags"]["items"], {"type": "string"})
        self.assertEqual(props["tags"]["description"], "Free-form tags, continued on a second line.")
        self.assertEqual(props["note"]["type"], "string")
        self.assertNotIn("default", props["note"])
        self.assertEqual(props["precision"]["default"], 2)
        self.assertEqual(props["label"]["description"], "Shown to the user.")
        self.assertEqual(convert.parameters["required"], ["amount", "unit", "tags"])
        self.assertFalse(convert.parameters["additionalProperties"])

    def test_options_override_name_and_description(self) -> None:
        @tool(name="custom_name", description="Overridden.", requires_confirmation=True)
        def anything() -> str:
            """Ignored."""
            return "x"

        self.assertEqual(anything.name, "custom_name")
        self.assertEqual(anything.description, "Overridden.")
        self.assertTrue(anything.requires_confirmation)
        self.assertEqual(anything(), "x")  # still callable as a function

    def test_kwargs_allow_extra_arguments(self) -> None:
        @tool
        def loose(**options) -> dict:
            """Accept anything."""
            return options

        self.assertNotIn("additionalProperties", loose.parameters)
        result = loose.run(ToolContext(workspace=Path.cwd()), a=1)
        self.assertEqual(json.loads(result.content), {"a": 1})

    def test_invalid_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            tool(name="has space")(lambda: "")

    def test_rest_style_params(self) -> None:
        summary, params = parse_docstring("Do it.\n\n:param path: Where.\n:param int n: How many.")
        self.assertEqual(summary, "Do it.")
        self.assertEqual(params, {"path": "Where.", "n": "How many."})


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ToolContext(workspace=Path.cwd())

    def test_return_values_become_results(self) -> None:
        @tool
        def as_dict() -> dict:
            """d"""
            return {"日本": 1}

        @tool
        def nothing() -> None:
            """n"""

        @tool
        def explicit() -> ToolResult:
            """e"""
            return ToolResult(False, "custom", images=("data:image/png;base64,AA",))

        self.assertEqual(json.loads(as_dict.run(self.context).content), {"日本": 1})
        self.assertIn("日本", as_dict.run(self.context).content)
        self.assertTrue(nothing.run(self.context).ok)
        result = explicit.run(self.context)
        self.assertFalse(result.ok)
        self.assertEqual(result.images, ("data:image/png;base64,AA",))

    def test_tool_error_fails_with_message(self) -> None:
        @tool
        def refuse() -> str:
            """r"""
            raise ToolError("not today")

        result = refuse.run(self.context)
        self.assertFalse(result.ok)
        self.assertEqual(result.content, "not today")

    def test_other_exceptions_propagate_to_the_runtime(self) -> None:
        @tool
        def crash() -> str:
            """c"""
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            crash.run(self.context)

    def test_arguments_are_validated_and_lightly_coerced(self) -> None:
        @tool
        def repeat(context: ToolContext, text: str, times: int = 1, loud: bool = False) -> str:
            """r"""
            assert isinstance(context, ToolContext)
            out = text * times
            return out.upper() if loud else out

        self.assertEqual(repeat.run(self.context, text="ab", times="2").content, "abab")
        self.assertEqual(repeat.run(self.context, text="a", loud="true").content, "A")
        self.assertEqual(repeat.run(self.context, text="a", times=None).content, "a")
        missing = repeat.run(self.context)
        self.assertFalse(missing.ok)
        self.assertIn("Invalid arguments", missing.content)
        self.assertFalse(repeat.run(self.context, text="a", times="many").ok)
        self.assertFalse(repeat.run(self.context, text="a", bogus=1).ok)


class LoaderTests(unittest.TestCase):
    def test_builtin_tools_are_function_files(self) -> None:
        loaded = load_function_tools([builtin_tools_dir()])
        self.assertEqual(loaded.errors, [])
        self.assertEqual(
            set(loaded.tools),
            {"list_dir", "read_file", "write_file", "append_to_file", "delete_file",
             "move_file", "search_files", "run_powershell", "fetch_url"},
        )
        self.assertTrue(all(found.origin == "built-in" for found in loaded.tools.values()))
        self.assertTrue(loaded.tools["write_file"].requires_confirmation)
        self.assertFalse(loaded.tools["read_file"].requires_confirmation)

    def test_loads_tools_from_a_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "tools"
            path = _write_tool_file(directory, "greet", GREET)
            _write_tool_file(directory, "_helpers", "from little_agent.tools import tool\n@tool\ndef hidden() -> str:\n    return ''\n")
            loaded = load_function_tools([directory])
        self.assertEqual(list(loaded.tools), ["greet"])
        greet = loaded.tools["greet"]
        self.assertEqual(greet.source, path.resolve())
        self.assertEqual(greet.run(ToolContext(workspace=Path(tmp)), name="Ann", excited=True).content, "Hello, Ann!")

    def test_broken_file_is_reported_not_fatal(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "tools"
            _write_tool_file(directory, "broken", "def oops(:\n")
            _write_tool_file(directory, "greet", GREET)
            loaded = load_function_tools([directory])
        self.assertIn("greet", loaded.tools)
        self.assertEqual(len(loaded.errors), 1)
        self.assertIn("broken.py", loaded.errors[0])
        self.assertIn("SyntaxError", loaded.errors[0])

    def test_later_directory_overrides_and_disabled_removes(self) -> None:
        with TemporaryDirectory() as tmp:
            override = _write_tool_file(
                Path(tmp) / "tools",
                "read_file",
                """
                from little_agent.tools import tool

                @tool
                def read_file(path: str) -> str:
                    \"\"\"Read, the project's way.\"\"\"
                    return "overridden"
                """,
            )
            registry = default_tools(
                directories=[builtin_tools_dir(), override.parent],
                disabled={"run_powershell"},
            )
        self.assertEqual(registry.get("read_file").description, "Read, the project's way.")
        self.assertNotIn("run_powershell", registry.names())
        self.assertIn("write_file", registry.names())

    def test_edits_and_deletions_are_picked_up(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "tools"
            path = _write_tool_file(directory, "greet", GREET)
            first = load_function_tools([directory]).tools["greet"]
            self.assertIs(load_function_tools([directory]).tools["greet"], first)  # cached
            # Same size, written straight away: must still be seen as changed.
            path.write_text(textwrap.dedent(GREET.replace("Hello", "Hiya!")), encoding="utf-8")
            second = load_function_tools([directory]).tools["greet"]
            self.assertIsNot(second, first)
            self.assertEqual(second.run(ToolContext(workspace=Path(tmp)), name="Bo").content, "Hiya!, Bo.")
            path.unlink()
            self.assertEqual(load_function_tools([directory]).tools, {})


class ConfigTests(unittest.TestCase):
    KEYS = ("LITTLE_AGENT_WORKSPACE", "LITTLE_AGENT_TOOL_DIRS", "LITTLE_AGENT_BUILTIN_TOOLS", "LITTLE_AGENT_DISABLED_TOOLS")

    def _from_env(self, **env: str) -> AgentConfig:
        saved = {key: os.environ.get(key) for key in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)
        os.environ.update(env)
        try:
            return AgentConfig.from_env()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_tool_directory_order(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = self._from_env(LITTLE_AGENT_WORKSPACE=tmp, LITTLE_AGENT_TOOL_DIRS="extra")
        self.assertEqual(
            config.tool_dirs,
            (
                builtin_tools_dir(),
                (Path.home() / ".little_agent" / "tools").resolve(),
                workspace / "tools",
                workspace / "extra",
            ),
        )
        self.assertEqual(config.disabled_tools, frozenset())

    def test_builtin_can_be_left_out_and_tools_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            config = self._from_env(
                LITTLE_AGENT_WORKSPACE=tmp,
                LITTLE_AGENT_BUILTIN_TOOLS="false",
                LITTLE_AGENT_DISABLED_TOOLS="run_powershell, fetch_url",
            )
        self.assertNotIn(builtin_tools_dir(), config.tool_dirs)
        self.assertEqual(config.disabled_tools, frozenset({"run_powershell", "fetch_url"}))


class CallsTool:
    def __init__(self, name: str, arguments: dict) -> None:
        self.name, self.arguments = name, arguments

    def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict:
        results = [message for message in messages if message.role == "tool"]
        if results:
            return {"content": results[-1].content, "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "c1", "name": self.name, "arguments": self.arguments}]}


class AgentTests(unittest.TestCase):
    def test_agent_uses_a_workspace_tool(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            _write_tool_file(workspace / "tools", "greet", GREET)
            config = _config(workspace, tool_dirs=(builtin_tools_dir(), workspace / "tools"))
            agent = Agent(config, SkillLoader([]), llm=CallsTool("greet", {"name": "Ann"}))
            self.assertIn("greet", agent.tools.names())
            self.assertIn("read_file", agent.tools.names())
            self.assertIn("Hello, Ann.", agent.run("greet Ann").text)

    def test_confirmation_flag_is_honoured(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            _write_tool_file(
                workspace / "tools",
                "danger",
                """
                from little_agent.tools import tool

                @tool(requires_confirmation=True)
                def danger() -> str:
                    \"\"\"Do something risky.\"\"\"
                    return "did it"
                """,
            )
            asked: list[str] = []
            config = _config(workspace, require_confirmation=True, tool_dirs=(workspace / "tools",))
            agent = Agent(
                config,
                SkillLoader([]),
                llm=CallsTool("danger", {}),
                confirm=lambda name, _args: asked.append(name) or False,
            )
            result = agent.run("go")
        self.assertEqual(asked, ["danger"])
        self.assertIn("Cancelled", result.text)

    def test_profile_core_tools_filters_function_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            _write_tool_file(workspace / "tools", "greet", GREET)
            (workspace / "agents" / "narrow").mkdir(parents=True)
            (workspace / "agents" / "narrow" / "agent.json").write_text(
                json.dumps({"name": "narrow", "skills": [], "core_tools": ["greet"]}), encoding="utf-8"
            )
            config = _config(workspace, tool_dirs=(builtin_tools_dir(), workspace / "tools"), max_delegation_depth=0)
            profile = agents.load_profile(config.agents_dir, "narrow", LIBRARY)
            agent = build_agent(config, profile, None, StopController("x"))
        self.assertEqual(agent.tools.names(), ["greet"])

    def test_reload_picks_up_new_and_removed_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            tools_dir = workspace / "tools"
            config = _config(workspace, tool_dirs=(builtin_tools_dir(), tools_dir), max_delegation_depth=0)
            profile = agents.default_profile(config)
            session = ChatSession(build_agent(config, profile, None, StopController("x")))
            registry = CommandRegistry(workspace / "commands", workspace / "global")
            ctx = CommandContext(session=session, registry=registry, active_agent=profile.name)

            def activate(name: str) -> str:
                session.switch_agent(build_agent(config, agents.resolve_active(config, name), None, StopController("x")))
                return "ok"

            ctx.activate = activate
            path = _write_tool_file(tools_dir, "greet", GREET)
            output = registry.dispatch(ctx, "/reload").output
            self.assertIn("added  : greet", output)
            self.assertIn("greet", session.agent.tools.names())
            listing = registry.dispatch(ctx, "/tools").output
            self.assertIn("[workspace]", listing)
            self.assertIn("[built-in]", listing)
            detail = registry.dispatch(ctx, "/tools greet").output
            self.assertIn(str(path), detail)
            self.assertIn("Who to greet.", detail)
            path.unlink()
            self.assertIn("removed: greet", registry.dispatch(ctx, "/reload").output)
            self.assertNotIn("greet", session.agent.tools.names())


if __name__ == "__main__":
    unittest.main()
