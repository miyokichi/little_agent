from contextlib import suppress
import json
from unittest.mock import patch
import unittest
from pathlib import Path

from little_agent.agent import Agent
from little_agent.config import AgentConfig
from little_agent.llm import OpenAICompatibleChatClient
from little_agent.messages import Message
from little_agent.skills.loader import SkillLoader
from little_agent.tools import default_tools
from little_agent.tools.base import ToolContext, ToolRegistry, resolve_workspace_path


class CoreTests(unittest.TestCase):
    def test_skill_loader_reads_sample_skills(self) -> None:
        skills = SkillLoader(Path("skills").resolve()).load_all()

        self.assertGreaterEqual(
            {skill.name for skill in skills},
            {
                "file_manager",
                "python_coder",
                "windows_operator",
                "task_manager",
                "skill_creator",
                "excel_file",
                "ppt_file",
            },
        )

    def test_workspace_path_rejects_parent_escape(self) -> None:
        workspace = (Path.cwd() / ".test-workspace").resolve()

        with self.assertRaisesRegex(ValueError, "outside workspace"):
            resolve_workspace_path(workspace, "..")

    def test_local_fallback_agent_can_use_datetime(self) -> None:
        config = AgentConfig(
            model="local",
            workspace=(Path.cwd() / ".test-workspace").resolve(),
            require_confirmation=True,
            openai_api_key=None,
            openai_base_url="https://api.openai.com/v1",
        )
        agent = Agent(config, SkillLoader(Path("skills").resolve()))

        answer = agent.run("date")

        self.assertIn("[get_datetime]", answer)
        self.assertIn("OK:", answer)

    def test_openai_compatible_client_payload_shape(self) -> None:
        client = OpenAICompatibleChatClient("test-key", "http://localhost:1234/v1")
        message = Message(role="user", content="hello")

        payload = client._message_payload(message)

        self.assertEqual(payload, {"role": "user", "content": "hello"})

    def test_openai_compatible_client_payload_shape_for_tool_loop(self) -> None:
        client = OpenAICompatibleChatClient("test-key", "http://localhost:1234/v1")
        assistant = Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "get_datetime", "arguments": {}}],
        )
        tool = Message(role="tool", content="OK: today", tool_call_id="call_1")

        assistant_payload = client._message_payload(assistant)
        tool_payload = client._message_payload(tool)

        self.assertEqual(assistant_payload["tool_calls"][0]["function"]["name"], "get_datetime")
        self.assertEqual(tool_payload["tool_call_id"], "call_1")

    def test_openai_compatible_client_parses_tool_calls(self) -> None:
        calls = OpenAICompatibleChatClient._tool_calls(
            {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "get_datetime",
                            "arguments": "{}",
                        },
                    }
                ]
            }
        )

        self.assertEqual(calls, [{"id": "call_1", "name": "get_datetime", "arguments": {}}])

    def test_openai_compatible_client_wraps_timeout(self) -> None:
        client = OpenAICompatibleChatClient("test-key", "http://localhost:1234/v1", timeout=1)

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "timed out after 1 seconds"):
                client._post_json("/chat/completions", {})

    def test_agent_returns_tool_result_to_llm_for_final_answer(self) -> None:
        class TwoStepLLM:
            def __init__(self) -> None:
                self.calls: list[list[Message]] = []

            def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict[str, object]:
                self.calls.append([*messages])
                if len(self.calls) == 1:
                    return {
                        "content": "",
                        "tool_calls": [{"id": "call_1", "name": "get_datetime", "arguments": {}}],
                    }
                tool_messages = [message for message in messages if message.role == "tool"]
                return {"content": f"Final answer based on {tool_messages[-1].content}", "tool_calls": []}

        config = AgentConfig(
            model="local",
            workspace=Path.cwd().resolve(),
            require_confirmation=True,
            openai_api_key=None,
            openai_base_url="https://api.openai.com/v1",
        )
        llm = TwoStepLLM()
        agent = Agent(config, SkillLoader(Path("skills").resolve()), llm=llm)  # type: ignore[arg-type]

        answer = agent.run("date")

        self.assertIn("Final answer based on [get_datetime]", answer)
        self.assertEqual(len(llm.calls), 2)
        self.assertTrue(any(message.role == "tool" for message in llm.calls[1]))

    def test_agent_logs_conversation_tools_and_usage(self) -> None:
        workspace = (Path.cwd() / ".test-log-workspace").resolve()
        log_dir = workspace / "logs"
        config = AgentConfig(
            model="local",
            workspace=workspace,
            require_confirmation=True,
            openai_api_key=None,
            openai_base_url="https://api.openai.com/v1",
            enable_logging=True,
            log_dir=log_dir,
        )
        agent = Agent(config, SkillLoader(Path("skills").resolve()))

        try:
            answer = agent.run("date")
            assert agent.logger is not None
            session_id = agent.logger.session_id
            conversation_log = log_dir / "conversations" / f"{session_id}.jsonl"
            tool_log = log_dir / "tools" / f"{session_id}.jsonl"
            usage_log = log_dir / "usage" / f"{session_id}.jsonl"
            conversation_events = [json.loads(line)["event"] for line in conversation_log.read_text().splitlines()]
            tool_events = [json.loads(line)["event"] for line in tool_log.read_text().splitlines()]
            usage_records = [json.loads(line) for line in usage_log.read_text().splitlines()]
        finally:
            for path in (log_dir / "conversations").glob("*.jsonl"):
                path.unlink(missing_ok=True)
            for path in (log_dir / "tools").glob("*.jsonl"):
                path.unlink(missing_ok=True)
            for path in (log_dir / "usage").glob("*.jsonl"):
                path.unlink(missing_ok=True)
            for path in [log_dir / "conversations", log_dir / "tools", log_dir / "usage", log_dir, workspace]:
                with suppress(OSError):
                    path.rmdir()

        self.assertIn("[get_datetime]", answer)
        self.assertIn("user_message", conversation_events)
        self.assertIn("final_answer", conversation_events)
        self.assertIn("tool_result", tool_events)
        self.assertGreaterEqual(usage_records[-1]["totals"]["total_tokens"], 1)

    def test_skill_loader_reads_task_manager_script_tools(self) -> None:
        tools = SkillLoader(Path("skills").resolve()).load_tools()
        names = {tool.name for tool in tools}

        self.assertIn("add_task", names)
        self.assertIn("list_tasks", names)
        self.assertIn("complete_task", names)
        self.assertIn("delete_task", names)
        self.assertIn("create_skill", names)
        self.assertIn("validate_skill", names)
        self.assertIn("read_excel", names)
        self.assertIn("write_excel", names)
        self.assertIn("read_ppt", names)
        self.assertIn("write_ppt", names)

    def test_default_tools_do_not_include_portable_task_manager_tools(self) -> None:
        names = default_tools().names()

        self.assertNotIn("add_task", names)
        self.assertNotIn("list_tasks", names)
        self.assertNotIn("complete_task", names)
        self.assertNotIn("delete_task", names)

    def test_agent_registers_skill_script_tools(self) -> None:
        config = AgentConfig(
            model="local",
            workspace=Path.cwd().resolve(),
            require_confirmation=True,
            openai_api_key=None,
            openai_base_url="https://api.openai.com/v1",
        )
        agent = Agent(config, SkillLoader(Path("skills").resolve()))

        self.assertIn("add_task", agent.tools.names())
        self.assertIn("list_tasks", agent.tools.names())
        self.assertIn("create_skill", agent.tools.names())
        self.assertIn("read_excel", agent.tools.names())
        self.assertIn("read_ppt", agent.tools.names())

    def test_task_tools_can_add_and_list_tasks(self) -> None:
        workspace = (Path.cwd() / ".test-task-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = ToolRegistry()
        for tool in SkillLoader(Path("skills").resolve()).load_tools():
            tools.register(tool)

        try:
            added = tools.get("add_task").run(context, title="Write task manager tests", priority="high")
            listed = tools.get("list_tasks").run(context, status="open")
        finally:
            tasks_file = workspace / "data" / "tasks.json"
            tasks_file.unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "data").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(added.ok)
        self.assertTrue(listed.ok)
        self.assertIn("Write task manager tests", listed.content)

    def test_skill_creator_can_create_and_validate_skill(self) -> None:
        workspace = (Path.cwd() / ".test-skill-creator-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = ToolRegistry()
        for tool in SkillLoader(Path("skills").resolve()).load_tools():
            tools.register(tool)

        try:
            created = tools.get("create_skill").run(
                context,
                name="Demo Skill",
                description="Demo skill for tests.",
                include_scripts=True,
                include_tools_manifest=True,
            )
            validated = tools.get("validate_skill").run(context, name="demo-skill")
        finally:
            for path in [
                workspace / "skills" / "demo-skill" / "scripts" / "example_tool.py",
                workspace / "skills" / "demo-skill" / "tools.json",
                workspace / "skills" / "demo-skill" / "SKILL.md",
            ]:
                path.unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "skills" / "demo-skill" / "scripts").rmdir()
            with suppress(OSError):
                (workspace / "skills" / "demo-skill").rmdir()
            with suppress(OSError):
                (workspace / "skills").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(created.ok)
        self.assertTrue(validated.ok)

    def test_excel_skill_can_write_and_read_xlsx(self) -> None:
        workspace = (Path.cwd() / ".test-excel-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = ToolRegistry()
        for tool in SkillLoader(Path("skills").resolve()).load_tools():
            tools.register(tool)

        try:
            written = tools.get("write_excel").run(
                context,
                path="reports/sample.xlsx",
                sheet_name="Data",
                rows=[["Name", "Score"], ["Ada", 10], ["Grace", 12]],
            )
            read = tools.get("read_excel").run(context, path="reports/sample.xlsx")
        finally:
            (workspace / "reports" / "sample.xlsx").unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "reports").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(written.ok)
        self.assertTrue(read.ok)
        self.assertIn("Ada", read.content)
        self.assertIn("Grace", read.content)

    def test_ppt_skill_can_write_and_read_pptx(self) -> None:
        workspace = (Path.cwd() / ".test-ppt-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = ToolRegistry()
        for tool in SkillLoader(Path("skills").resolve()).load_tools():
            tools.register(tool)

        try:
            written = tools.get("write_ppt").run(
                context,
                path="deck/sample.pptx",
                slides=[
                    {"title": "Roadmap", "bullets": ["Build skills", "Test tools"]},
                    {"title": "Next", "bullets": ["Iterate"]},
                ],
            )
            read = tools.get("read_ppt").run(context, path="deck/sample.pptx")
        finally:
            (workspace / "deck" / "sample.pptx").unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "deck").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(written.ok)
        self.assertTrue(read.ok)
        self.assertIn("Roadmap", read.content)
        self.assertIn("Build skills", read.content)


if __name__ == "__main__":
    unittest.main()
