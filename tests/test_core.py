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
from little_agent.tools.base import ToolContext, ToolRegistry, ToolResult, resolve_workspace_path


class CoreTests(unittest.TestCase):
    def test_skill_loader_reads_sample_skills(self) -> None:
        skills = SkillLoader(Path("skills").resolve()).load_all()

        self.assertGreaterEqual(
            {skill.name for skill in skills},
            {
                "file_manager",
                "python_coder",
                "windows_operator",
                "project_manager",
                "skill_creator",
                "excel_file",
                "ppt_file",
            },
        )

    def test_workspace_path_rejects_parent_escape(self) -> None:
        workspace = (Path.cwd() / ".test-workspace").resolve()

        with self.assertRaisesRegex(ValueError, "outside workspace"):
            resolve_workspace_path(workspace, "..")

    def test_filesystem_append_delete_move_tools(self) -> None:
        workspace = (Path.cwd() / ".test-fs-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = default_tools()

        try:
            tools.get("write_file").run(context, path="a.txt", content="hello")
            appended = tools.get("append_to_file").run(context, path="a.txt", content=" world")
            content_after_append = (workspace / "a.txt").read_text(encoding="utf-8")

            moved = tools.get("move_file").run(context, source="a.txt", destination="sub/b.txt")
            source_gone = not (workspace / "a.txt").exists()
            dest_exists = (workspace / "sub" / "b.txt").exists()

            deleted = tools.get("delete_file").run(context, path="sub/b.txt")
            dest_gone = not (workspace / "sub" / "b.txt").exists()
        finally:
            for path in [workspace / "a.txt", workspace / "sub" / "b.txt"]:
                path.unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "sub").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(appended.ok)
        self.assertEqual(content_after_append, "hello world")
        self.assertTrue(moved.ok)
        self.assertTrue(source_gone)
        self.assertTrue(dest_exists)
        self.assertTrue(deleted.ok)
        self.assertTrue(dest_gone)

    def test_delete_and_move_reject_missing_or_directory(self) -> None:
        workspace = (Path.cwd() / ".test-fs-guard-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = default_tools()

        try:
            (workspace / "data").mkdir(parents=True, exist_ok=True)
            missing = tools.get("delete_file").run(context, path="nope.txt")
            on_dir = tools.get("delete_file").run(context, path="data")
            move_missing = tools.get("move_file").run(context, source="nope.txt", destination="x.txt")
        finally:
            with suppress(OSError):
                (workspace / "data").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertFalse(missing.ok)
        self.assertFalse(on_dir.ok)
        self.assertFalse(move_missing.ok)

    def test_fetch_url_rejects_non_http_scheme(self) -> None:
        from little_agent.tools.web import FetchUrlTool

        result = FetchUrlTool().run(ToolContext(workspace=Path.cwd()), url="ftp://example.com")

        self.assertFalse(result.ok)
        self.assertIn("http(s)", result.content)

    def test_message_payload_passes_through_multimodal_content(self) -> None:
        client = OpenAICompatibleChatClient("test-key", "http://localhost:1234/v1")
        blocks = [
            {"type": "text", "text": "look:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        message = Message(role="user", content=blocks)

        payload = client._message_payload(message)

        self.assertEqual(payload["content"], blocks)

    def test_agent_forwards_tool_image_output_to_model(self) -> None:
        class ImageTool:
            name = "fake_capture"
            description = "Return an image."
            requires_confirmation = False
            parameters = {"type": "object", "properties": {}, "additionalProperties": False}

            def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
                return ToolResult(True, "captured", images=("data:image/png;base64,AAAA",))

        class TwoStepLLM:
            def __init__(self) -> None:
                self.calls: list[list[Message]] = []

            def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict[str, object]:
                self.calls.append([*messages])
                if len(self.calls) == 1:
                    return {"content": "", "tool_calls": [{"id": "call_1", "name": "fake_capture", "arguments": {}}]}
                return {"content": "I can see the image.", "tool_calls": []}

        registry = ToolRegistry()
        registry.register(ImageTool())
        config = AgentConfig(
            model="local",
            workspace=Path.cwd().resolve(),
            require_confirmation=False,
            openai_api_key=None,
            openai_base_url="https://api.openai.com/v1",
        )
        llm = TwoStepLLM()
        agent = Agent(config, SkillLoader(Path("skills").resolve()), tools=registry, llm=llm)  # type: ignore[arg-type]

        answer = agent.run("capture the screen")

        self.assertEqual(answer, "I can see the image.")
        image_messages = [
            message
            for message in llm.calls[1]
            if message.role == "user"
            and isinstance(message.content, list)
            and any(block.get("type") == "image_url" for block in message.content)
        ]
        self.assertEqual(len(image_messages), 1)
        image_block = next(b for b in image_messages[0].content if b.get("type") == "image_url")
        self.assertEqual(image_block["image_url"]["url"], "data:image/png;base64,AAAA")

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
            conversation_events = [
                json.loads(line)["event"] for line in conversation_log.read_text(encoding="utf-8").splitlines()
            ]
            tool_events = [json.loads(line)["event"] for line in tool_log.read_text(encoding="utf-8").splitlines()]
            usage_records = [json.loads(line) for line in usage_log.read_text(encoding="utf-8").splitlines()]
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

    def test_skill_loader_reads_project_manager_script_tools(self) -> None:
        tools = SkillLoader(Path("skills").resolve()).load_tools()
        names = {tool.name for tool in tools}

        self.assertIn("create_project", names)
        self.assertIn("add_task", names)
        self.assertIn("update_task", names)
        self.assertIn("update_task_status", names)
        self.assertIn("add_task_comment", names)
        self.assertIn("show_project", names)
        self.assertIn("list_projects", names)
        self.assertIn("list_tasks", names)
        self.assertIn("delete_task", names)
        self.assertIn("delete_project", names)
        self.assertIn("open_project_viewer", names)
        self.assertIn("create_skill", names)
        self.assertIn("validate_skill", names)
        self.assertIn("read_excel", names)
        self.assertIn("write_excel", names)
        self.assertIn("read_ppt", names)
        self.assertIn("write_ppt", names)
        self.assertIn("git_status", names)
        self.assertIn("git_diff", names)
        self.assertIn("git_log", names)
        self.assertIn("git_add", names)
        self.assertIn("git_commit", names)
        self.assertIn("take_screenshot", names)

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
        context, tools = self._project_tools(workspace)

        try:
            added = tools.get("add_task").run(context, title="Write task manager tests", priority="high")
            listed = tools.get("list_tasks").run(context, status="open")
            stored = json.loads((workspace / "data" / "projects.json").read_text(encoding="utf-8"))
        finally:
            self._cleanup_project_workspace(workspace)

        self.assertTrue(added.ok)
        self.assertTrue(listed.ok)
        self.assertIn("Write task manager tests", listed.content)
        self.assertIn("Inbox", listed.content)
        # Standalone TODOs land in the auto-created inbox project.
        self.assertTrue(stored["projects"][0]["inbox"])

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

    @staticmethod
    def _project_tools(workspace: Path) -> tuple[ToolContext, ToolRegistry]:
        context = ToolContext(workspace=workspace)
        tools = ToolRegistry()
        for tool in SkillLoader(Path("skills").resolve()).load_tools():
            tools.register(tool)
        return context, tools

    @staticmethod
    def _cleanup_project_workspace(workspace: Path) -> None:
        for name in (
            "projects.json",
            "projects.json.lock",
            "projects.json.tmp",
            "workflows.json",
            "workflows.json.bak",
            "tasks.json",
            "tasks.json.bak",
        ):
            (workspace / "data" / name).unlink(missing_ok=True)
        with suppress(OSError):
            (workspace / "data").rmdir()
        with suppress(OSError):
            workspace.rmdir()

    @staticmethod
    def _read_projects(workspace: Path) -> dict:
        return json.loads((workspace / "data" / "projects.json").read_text(encoding="utf-8"))

    def test_project_tools_create_update_and_show(self) -> None:
        workspace = (Path.cwd() / ".test-project-workspace").resolve()
        context, tools = self._project_tools(workspace)

        try:
            created = tools.get("create_project").run(
                context,
                title="Release prep",
                goal="Ship v1",
                tasks=[
                    {"key": "t1", "title": "Draft the report", "assignee": "ai"},
                    {"key": "t2", "title": "Review the report", "assignee": "human", "depends_on": ["t1"]},
                ],
            )
            stored = self._read_projects(workspace)
            project = stored["projects"][0]
            task_ids = [task["id"] for task in project["tasks"]]
            updated = tools.get("update_task_status").run(
                context, task_id=task_ids[0], status="done", result="Saved draft.md"
            )
            shown = tools.get("show_project").run(context, project_id=project["id"])
        finally:
            self._cleanup_project_workspace(workspace)

        self.assertTrue(created.ok)
        self.assertEqual([len(task_id) for task_id in task_ids], [8, 8])
        self.assertEqual(project["tasks"][1]["depends_on"], [task_ids[0]])
        self.assertEqual(project["tasks"][1]["assignee"], "human")
        self.assertTrue(updated.ok)
        self.assertIn(f"READY: {task_ids[1]}(human)", updated.content)
        self.assertTrue(shown.ok)
        self.assertIn("<- READY", shown.content)

    def test_project_create_rejects_invalid_input(self) -> None:
        workspace = (Path.cwd() / ".test-project-invalid-workspace").resolve()
        context, tools = self._project_tools(workspace)

        try:
            cyclic = tools.get("create_project").run(
                context,
                title="Cyclic",
                tasks=[
                    {"key": "a", "title": "A", "assignee": "ai", "depends_on": ["b"]},
                    {"key": "b", "title": "B", "assignee": "ai", "depends_on": ["a"]},
                ],
            )
            unknown_dep = tools.get("create_project").run(
                context,
                title="Unknown dep",
                tasks=[{"key": "a", "title": "A", "assignee": "ai", "depends_on": ["missing"]}],
            )
            bad_assignee = tools.get("create_project").run(
                context,
                title="Bad assignee",
                tasks=[{"key": "a", "title": "A", "assignee": "robot"}],
            )
            file_created = (workspace / "data" / "projects.json").exists()
        finally:
            self._cleanup_project_workspace(workspace)

        self.assertFalse(cyclic.ok)
        self.assertIn("cycle", cyclic.content.lower())
        self.assertFalse(unknown_dep.ok)
        self.assertIn("unknown key", unknown_dep.content)
        self.assertFalse(bad_assignee.ok)
        self.assertIn("assignee", bad_assignee.content)
        self.assertFalse(file_created)

    def test_update_task_edits_fields_and_rejects_cycles(self) -> None:
        workspace = (Path.cwd() / ".test-project-edit-workspace").resolve()
        context, tools = self._project_tools(workspace)

        try:
            tools.get("create_project").run(
                context,
                title="Edit me",
                tasks=[
                    {"key": "a", "title": "A", "assignee": "ai"},
                    {"key": "b", "title": "B", "assignee": "ai", "depends_on": ["a"]},
                ],
            )
            stored = self._read_projects(workspace)
            ids = [task["id"] for task in stored["projects"][0]["tasks"]]

            edited = tools.get("update_task").run(
                context, task_id=ids[0], title="A2", assignee="human", due="tomorrow", priority="high"
            )
            cyclic = tools.get("update_task").run(context, task_id=ids[0], depends_on=[ids[1]])
            self_dep = tools.get("update_task").run(context, task_id=ids[0], depends_on=[ids[0]])
            stored_after = self._read_projects(workspace)
            task_a = stored_after["projects"][0]["tasks"][0]
        finally:
            self._cleanup_project_workspace(workspace)

        self.assertTrue(edited.ok)
        self.assertEqual(task_a["title"], "A2")
        self.assertEqual(task_a["assignee"], "human")
        self.assertEqual(task_a["due"], "tomorrow")
        self.assertEqual(task_a["priority"], "high")
        self.assertFalse(cyclic.ok)
        self.assertIn("cycle", cyclic.content.lower())
        self.assertFalse(self_dep.ok)
        self.assertEqual(task_a["depends_on"], [])

    def test_assignee_name_for_human_tasks(self) -> None:
        workspace = (Path.cwd() / ".test-project-name-workspace").resolve()
        context, tools = self._project_tools(workspace)

        try:
            tools.get("create_project").run(
                context,
                title="Named",
                tasks=[
                    {"key": "t1", "title": "Draft", "assignee": "ai"},
                    {"key": "t2", "title": "Review", "assignee": "human", "assignee_name": "山田さん"},
                ],
            )
            stored = self._read_projects(workspace)
            ids = [task["id"] for task in stored["projects"][0]["tasks"]]
            shown = tools.get("show_project").run(context)

            # An ai task must not carry a name.
            ai_named = tools.get("add_task").run(
                context, title="Solo", assignee="ai", assignee_name="無視される"
            )
            # Renaming and then switching to ai clears the name.
            renamed = tools.get("update_task").run(context, task_id=ids[1], assignee_name="田中さん")
            to_ai = tools.get("update_task").run(context, task_id=ids[1], assignee="ai")
            stored_after = self._read_projects(workspace)
            tasks_after = {t["id"]: t for t in stored_after["projects"][0]["tasks"]}
        finally:
            self._cleanup_project_workspace(workspace)

        review = tasks_after[ids[1]]
        self.assertEqual(stored["projects"][0]["tasks"][1]["assignee_name"], "山田さん")
        self.assertIn("[山田さん/pending]", shown.content)
        self.assertTrue(ai_named.ok)
        self.assertTrue(renamed.ok)
        self.assertTrue(to_ai.ok)
        self.assertEqual(review["assignee"], "ai")
        self.assertEqual(review["assignee_name"], "")

    def test_task_comments_from_agent_and_viewer(self) -> None:
        from little_agent.viewer import viewer_add_comment

        workspace = (Path.cwd() / ".test-project-comment-workspace").resolve()
        context, tools = self._project_tools(workspace)

        try:
            tools.get("create_project").run(
                context,
                title="Commented",
                tasks=[{"key": "t1", "title": "Work", "assignee": "ai"}],
            )
            stored = self._read_projects(workspace)
            project_id = stored["projects"][0]["id"]
            task_id = stored["projects"][0]["tasks"][0]["id"]

            agent_added = tools.get("add_task_comment").run(context, task_id=task_id, text="下書きを開始")
            viewer_ok, _ = viewer_add_comment(
                workspace, {"project_id": project_id, "task_id": task_id, "text": "方向性OKです"}
            )
            empty_rejected = tools.get("add_task_comment").run(context, task_id=task_id, text="  ")
            shown = tools.get("show_project").run(context, project_id=project_id)
            stored_after = self._read_projects(workspace)
            comments = stored_after["projects"][0]["tasks"][0]["comments"]
        finally:
            self._cleanup_project_workspace(workspace)

        self.assertTrue(agent_added.ok)
        self.assertTrue(viewer_ok)
        self.assertFalse(empty_rejected.ok)
        self.assertEqual([c["via"] for c in comments], ["agent", "viewer"])
        self.assertEqual([c["text"] for c in comments], ["下書きを開始", "方向性OKです"])
        self.assertTrue(all(c["at"] for c in comments))
        self.assertIn("comments=2", shown.content)
        self.assertIn("方向性OKです", shown.content)

    def test_delete_task_removes_dangling_dependencies(self) -> None:
        workspace = (Path.cwd() / ".test-project-del-workspace").resolve()
        context, tools = self._project_tools(workspace)

        try:
            tools.get("create_project").run(
                context,
                title="Del",
                tasks=[
                    {"key": "a", "title": "A", "assignee": "ai"},
                    {"key": "b", "title": "B", "assignee": "ai", "depends_on": ["a"]},
                ],
            )
            stored = self._read_projects(workspace)
            ids = [task["id"] for task in stored["projects"][0]["tasks"]]
            deleted = tools.get("delete_task").run(context, task_id=ids[0])
            stored_after = self._read_projects(workspace)
            remaining = stored_after["projects"][0]["tasks"]
        finally:
            self._cleanup_project_workspace(workspace)

        self.assertTrue(deleted.ok)
        self.assertEqual([task["id"] for task in remaining], [ids[1]])
        self.assertEqual(remaining[0]["depends_on"], [])

    def test_migration_from_legacy_tasks_and_workflows(self) -> None:
        workspace = (Path.cwd() / ".test-project-migrate-workspace").resolve()
        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        legacy_workflow = {
            "version": 1,
            "workflows": [
                {
                    "id": "wf000001",
                    "title": "Old flow",
                    "goal": "",
                    "status": "active",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "tasks": [
                        {
                            "id": "aaaa0001",
                            "title": "Old task",
                            "description": "",
                            "assignee": "ai",
                            "status": "pending",
                            "depends_on": [],
                            "result": "",
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "started_at": None,
                            "completed_at": None,
                            "completed_via": None,
                        }
                    ],
                }
            ],
        }
        legacy_tasks = [
            {
                "id": "bbbb0001",
                "title": "Buy milk",
                "status": "open",
                "created_at": "2026-01-02T00:00:00+00:00",
                "due": "friday",
                "priority": "high",
                "notes": "2 bottles",
            },
            {"id": "bbbb0002", "title": "Done thing", "status": "done", "completed_at": "2026-01-03T00:00:00+00:00"},
        ]
        (data_dir / "workflows.json").write_text(json.dumps(legacy_workflow), encoding="utf-8")
        (data_dir / "tasks.json").write_text(json.dumps(legacy_tasks), encoding="utf-8")
        context, tools = self._project_tools(workspace)

        try:
            listed = tools.get("list_projects").run(context, status="all")
            stored = self._read_projects(workspace)
            titles = {project["title"] for project in stored["projects"]}
            inbox = next(project for project in stored["projects"] if project.get("inbox"))
            migrated_open = next(task for task in inbox["tasks"] if task["id"] == "bbbb0001")
            migrated_done = next(task for task in inbox["tasks"] if task["id"] == "bbbb0002")
            legacy_backed_up = (
                (data_dir / "workflows.json.bak").exists()
                and (data_dir / "tasks.json.bak").exists()
                and not (data_dir / "workflows.json").exists()
                and not (data_dir / "tasks.json").exists()
            )
        finally:
            self._cleanup_project_workspace(workspace)

        self.assertTrue(listed.ok)
        self.assertEqual(titles, {"Old flow", "Inbox"})
        self.assertEqual(migrated_open["status"], "pending")
        self.assertEqual(migrated_open["description"], "2 bottles")
        self.assertEqual(migrated_open["due"], "friday")
        self.assertEqual(migrated_done["status"], "done")
        self.assertTrue(legacy_backed_up)

    def test_project_viewer_serves_state_and_full_crud(self) -> None:
        import threading
        import urllib.error
        import urllib.request

        from little_agent.viewer import make_server

        workspace = (Path.cwd() / ".test-project-viewer-workspace").resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        def post_json(port: int, route: str, body: dict) -> tuple[int, dict]:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{route}",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read().decode("utf-8"))

        server = None
        try:
            server = make_server(workspace, 0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()

            proj_status, proj_body = post_json(port, "/api/project/create", {"title": "Viewer proj", "goal": "g"})
            project_id = proj_body["id"]
            task1_status, task1_body = post_json(
                port, "/api/task/create", {"project_id": project_id, "title": "Step 1", "assignee": "ai"}
            )
            task1_id = task1_body["id"]
            task2_status, task2_body = post_json(
                port,
                "/api/task/create",
                {
                    "project_id": project_id,
                    "title": "Step 2",
                    "assignee": "human",
                    "assignee_name": "山田さん",
                    "depends_on": [task1_id],
                },
            )
            task2_id = task2_body["id"]
            cycle_status, _cycle_body = post_json(
                port,
                "/api/task/update",
                {"project_id": project_id, "task_id": task1_id, "depends_on": [task2_id]},
            )
            edit_status, _edit_body = post_json(
                port,
                "/api/task/update",
                {"project_id": project_id, "task_id": task1_id, "status": "done", "title": "Step 1 (edited)"},
            )
            comment_status, _comment_body = post_json(
                port,
                "/api/task/comment",
                {"project_id": project_id, "task_id": task1_id, "text": "経過メモ"},
            )
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as response:
                state = json.loads(response.read().decode("utf-8"))
            delete_status, _delete_body = post_json(
                port, "/api/task/delete", {"project_id": project_id, "task_id": task2_id}
            )
            stored = self._read_projects(workspace)
            tasks_after = stored["projects"][0]["tasks"]
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            self._cleanup_project_workspace(workspace)

        self.assertEqual(state["app"], "little-agent-viewer")
        self.assertEqual((proj_status, task1_status, task2_status), (200, 200, 200))
        self.assertEqual(cycle_status, 409)
        self.assertEqual(edit_status, 200)
        self.assertEqual(comment_status, 200)
        viewer_task = next(task for task in state["projects"][0]["tasks"] if task["id"] == task1_id)
        self.assertEqual(viewer_task["status"], "done")
        self.assertEqual(viewer_task["title"], "Step 1 (edited)")
        self.assertEqual(viewer_task["completed_via"], "viewer")
        self.assertEqual(viewer_task["created_via"], "viewer")
        self.assertEqual([c["text"] for c in viewer_task["comments"]], ["経過メモ"])
        self.assertEqual(viewer_task["comments"][0]["via"], "viewer")
        human_task = next(task for task in state["projects"][0]["tasks"] if task["id"] == task2_id)
        self.assertEqual(human_task["assignee_name"], "山田さん")
        ready_flags = {task["id"]: task["ready"] for task in state["projects"][0]["tasks"]}
        self.assertEqual(ready_flags, {task1_id: False, task2_id: True})
        self.assertEqual(delete_status, 200)
        self.assertEqual([task["id"] for task in tasks_after], [task1_id])

    def test_project_update_breaks_stale_lock(self) -> None:
        import os
        import time

        workspace = (Path.cwd() / ".test-project-lock-workspace").resolve()
        context, tools = self._project_tools(workspace)

        try:
            tools.get("create_project").run(
                context,
                title="Locked",
                tasks=[{"key": "t1", "title": "Only task", "assignee": "ai"}],
            )
            stored = self._read_projects(workspace)
            task_id = stored["projects"][0]["tasks"][0]["id"]
            lock_path = workspace / "data" / "projects.json.lock"
            lock_path.write_text("", encoding="utf-8")
            stale = time.time() - 60
            os.utime(lock_path, (stale, stale))
            updated = tools.get("update_task_status").run(context, task_id=task_id, status="done")
        finally:
            self._cleanup_project_workspace(workspace)

        self.assertTrue(updated.ok)


if __name__ == "__main__":
    unittest.main()
