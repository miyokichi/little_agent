"""MCP: the client against a real server process / HTTP endpoint, and serve-mcp.

The client side runs ``tests/mcp_fake_server.py`` as a genuine stdio child
process (and in-process over Streamable HTTP), so framing, pagination, server
requests and notifications are exercised for real.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent import agents
from little_agent.agent import Agent, RunResult
from little_agent.cli import build_parser
from little_agent.commands import CommandContext, CommandRegistry
from little_agent.config import AgentConfig, builtin_skills_dir
from little_agent.control import StopController
from little_agent.factory import build_agent
from little_agent.mcp.client import McpClient, McpError
from little_agent.mcp.config import McpConfigError, load_mcp_config
from little_agent.mcp.manager import McpManager
from little_agent.mcp.server import TOOL_NAME, McpAgentServer
from little_agent.mcp.tools import tool_name
from little_agent.mcp.transport import HttpTransport, StdioTransport
from little_agent.messages import Message
from little_agent.session import ChatSession
from little_agent.skills.loader import SkillLoader
from little_agent.tools.base import ToolContext, ToolRegistry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_fake_server import McpFakeHttpServer  # noqa: E402

LIBRARY = builtin_skills_dir()
FAKE_SERVER = Path(__file__).resolve().parent / "mcp_fake_server.py"
REPO = Path(__file__).resolve().parent.parent


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


def _write_mcp_json(path: Path, servers: dict) -> Path:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def _fake_entry(**extra) -> dict:
    return {"command": sys.executable, "args": [str(FAKE_SERVER)], **extra}


def _stdio_client(roots=None) -> McpClient:
    return McpClient(
        StdioTransport(sys.executable, (str(FAKE_SERVER),)), "fake", timeout=10, roots=roots
    ).connect()


class ConfigTests(unittest.TestCase):
    def test_reads_stdio_and_http_servers_with_env_expansion(self) -> None:
        with TemporaryDirectory() as tmp:
            os.environ["LA_TEST_TOKEN"] = "s3cret"
            try:
                path = _write_mcp_json(
                    Path(tmp) / "mcp.json",
                    {
                        "local": {"command": "npx", "args": ["-y", "pkg"], "env": {"K": "${LA_TEST_TOKEN}"}},
                        "remote": {
                            "url": "https://example.test/mcp",
                            "headers": {"Authorization": "Bearer ${LA_TEST_TOKEN}"},
                            "require_confirmation": False,
                            "tools": ["search"],
                        },
                    },
                )
                servers = load_mcp_config([path], Path(tmp))
            finally:
                del os.environ["LA_TEST_TOKEN"]
        self.assertEqual(servers["local"].transport, "stdio")
        self.assertEqual(servers["local"].args, ("-y", "pkg"))
        self.assertEqual(servers["local"].env, {"K": "s3cret"})
        self.assertTrue(servers["local"].require_confirmation)
        self.assertEqual(servers["remote"].transport, "http")
        self.assertEqual(servers["remote"].headers["Authorization"], "Bearer s3cret")
        self.assertFalse(servers["remote"].require_confirmation)
        self.assertTrue(servers["remote"].tool_allowed("search"))
        self.assertFalse(servers["remote"].tool_allowed("delete"))

    def test_later_file_overrides_earlier(self) -> None:
        with TemporaryDirectory() as tmp:
            first = _write_mcp_json(Path(tmp) / "global.json", {"a": {"command": "one"}, "b": {"command": "b"}})
            second = _write_mcp_json(Path(tmp) / "project.json", {"a": {"command": "two"}})
            servers = load_mcp_config([first, second, Path(tmp) / "missing.json"], Path(tmp))
        self.assertEqual(servers["a"].command, "two")
        self.assertEqual(servers["b"].command, "b")

    def test_malformed_definitions_are_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            for servers in (
                {"x": {"command": "a", "url": "http://b"}},
                {"x": {}},
                {"bad name": {"command": "a"}},
                {"x": {"command": "a", "args": "not-a-list"}},
            ):
                path = _write_mcp_json(Path(tmp) / "mcp.json", servers)
                with self.assertRaises(McpConfigError):
                    load_mcp_config([path], Path(tmp))
            broken = Path(tmp) / "broken.json"
            broken.write_text("{nope", encoding="utf-8")
            with self.assertRaises(McpConfigError):
                load_mcp_config([broken], Path(tmp))

    def test_from_env_reads_global_then_workspace_file(self) -> None:
        with TemporaryDirectory() as tmp:
            saved = {key: os.environ.get(key) for key in ("LITTLE_AGENT_WORKSPACE", "LITTLE_AGENT_MCP_CONFIG", "LITTLE_AGENT_GLOBAL_MCP_CONFIG")}
            os.environ["LITTLE_AGENT_WORKSPACE"] = tmp
            os.environ.pop("LITTLE_AGENT_MCP_CONFIG", None)
            os.environ["LITTLE_AGENT_GLOBAL_MCP_CONFIG"] = str(Path(tmp) / "g.json")
            try:
                config = AgentConfig.from_env()
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        self.assertEqual(
            config.mcp_config_paths,
            ((Path(tmp) / "g.json").resolve(), (Path(tmp) / "mcp.json").resolve()),
        )


class ToolNameTests(unittest.TestCase):
    def test_names_are_prefixed_and_function_safe(self) -> None:
        self.assertEqual(tool_name("fs", "read_file"), "mcp__fs__read_file")
        self.assertEqual(tool_name("fs", "show.roots"), "mcp__fs__show_roots")

    def test_long_names_are_truncated_uniquely(self) -> None:
        first = tool_name("server", "x" * 80)
        second = tool_name("server", "x" * 79 + "y")
        self.assertLessEqual(len(first), 64)
        self.assertNotEqual(first, second)


class StdioClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        self.client = _stdio_client(roots=lambda: [self.workspace])

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def test_initialize_reports_server(self) -> None:
        self.assertEqual(self.client.server_info["name"], "fake")
        self.assertTrue(self.client.supports("tools"))
        self.assertIn("echo", self.client.instructions)
        self.assertEqual(self.client.protocol_version, "2025-06-18")

    def test_tools_are_paginated(self) -> None:
        names = [tool["name"] for tool in self.client.list_tools()]
        self.assertEqual(names, ["echo", "fail", "picture", "stats", "show.roots", "grow"])

    def test_call_tool(self) -> None:
        result = self.client.call_tool("echo", {"text": "こんにちは"})
        self.assertEqual(result["content"][0]["text"], "echo: こんにちは")

    def test_protocol_error_raises(self) -> None:
        with self.assertRaises(McpError) as caught:
            self.client.call_tool("nope", {})
        self.assertEqual(caught.exception.code, -32602)

    def test_server_can_ask_for_roots(self) -> None:
        result = self.client.call_tool("show.roots", {})
        self.assertEqual(result["content"][0]["text"], self.workspace.as_uri())

    def test_resources_and_prompts(self) -> None:
        self.assertEqual([r["uri"] for r in self.client.list_resources()], ["memo://readme", "memo://logo"])
        self.assertEqual(self.client.list_resource_templates()[0]["uriTemplate"], "memo://{name}")
        read = self.client.read_resource("memo://readme")
        self.assertEqual(read["contents"][0]["text"], "hello from a resource")
        self.assertEqual(self.client.list_prompts()[0]["name"], "review")
        prompt = self.client.get_prompt("review", {"path": "a.py"})
        self.assertEqual(prompt["messages"][0]["content"]["text"], "Please review a.py.")

    def test_list_changed_notification_is_noticed(self) -> None:
        self.client.list_tools()
        self.client.call_tool("grow", {})
        self.assertTrue(self.client.tools_changed)
        self.assertIn("late", [tool["name"] for tool in self.client.list_tools()])
        self.assertFalse(self.client.tools_changed)

    def test_requests_fail_once_disconnected(self) -> None:
        self.client.close()
        with self.assertRaises(McpError):
            self.client.call_tool("echo", {"text": "x"})


class HttpClientTests(unittest.TestCase):
    def _roundtrip(self, sse: bool) -> None:
        server = McpFakeHttpServer(sse=sse)
        try:
            client = McpClient(HttpTransport(server.url, {"X-Test": "1"}), "web", timeout=10).connect()
            names = [tool["name"] for tool in client.list_tools()]
            self.assertIn("echo", names)
            result = client.call_tool("echo", {"text": "hi"})
            self.assertEqual(result["content"][0]["text"], "echo: hi")
            client.call_tool("grow", {})
            if sse:  # the notification rides the same event stream as the reply
                self.assertTrue(client.tools_changed)
            client.close()
            later = {key.lower(): value for key, value in server.seen_headers[-1].items()}
            self.assertEqual(later.get("mcp-session-id"), McpFakeHttpServer.SESSION)
            self.assertEqual(later.get("mcp-protocol-version"), "2025-06-18")
            self.assertEqual(later.get("x-test"), "1")
            self.assertTrue(server.deleted)
        finally:
            server.close()

    def test_json_responses(self) -> None:
        self._roundtrip(sse=False)

    def test_sse_responses(self) -> None:
        self._roundtrip(sse=True)

    def test_unreachable_server_is_an_error(self) -> None:
        client = McpClient(HttpTransport("http://127.0.0.1:9/mcp", timeout=2), "gone", timeout=2)
        with self.assertRaises(McpError):
            client.connect()


class ScriptedLLM:
    """Calls one tool, then answers with whatever the tool said."""

    def __init__(self, tool: str, arguments: dict) -> None:
        self.tool = tool
        self.arguments = arguments
        self.calls: list[list[Message]] = []

    def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict:
        self.calls.append([*messages])
        tool_messages = [message for message in messages if message.role == "tool"]
        if tool_messages:
            return {"content": f"final: {tool_messages[-1].content}", "tool_calls": [], "finish_reason": "stop"}
        return {"content": "", "tool_calls": [{"id": "c1", "name": self.tool, "arguments": self.arguments}], "finish_reason": "tool_calls"}


class AgentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        self.mcp_json = self.workspace / "mcp.json"
        self.managers: list[McpManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.close()
        self.tmp.cleanup()

    def _agent(self, servers: dict, profile=None, **config_overrides) -> Agent:
        _write_mcp_json(self.mcp_json, servers)
        config = _config(self.workspace, mcp_config_paths=(self.mcp_json,), **config_overrides)
        manager = McpManager.from_config(config)
        self.managers.append(manager)
        profile = profile or agents.default_profile(config)
        view = manager.view(profile.mcp_selection())
        return build_agent(config, profile, None, StopController("<ctrl>+<alt>+q"), mcp=view)

    def test_default_agent_gets_every_server_tool(self) -> None:
        agent = self._agent({"fake": _fake_entry()})
        names = agent.tools.names()
        for expected in ("mcp__fake__echo", "mcp__fake__show_roots", "list_mcp_resources", "read_mcp_resource"):
            self.assertIn(expected, names)
        self.assertTrue(agent.tools.get("mcp__fake__echo").requires_confirmation)
        self.assertIn("[MCP fake]", agent.tools.get("mcp__fake__echo").description)

    def test_tool_allowlist_and_confirmation_setting(self) -> None:
        agent = self._agent({"fake": _fake_entry(tools=["echo"], require_confirmation=False)})
        mcp_tools = [name for name in agent.tools.names() if name.startswith("mcp__")]
        self.assertEqual(mcp_tools, ["mcp__fake__echo"])
        self.assertFalse(agent.tools.get("mcp__fake__echo").requires_confirmation)

    def test_named_profile_opts_in_per_server(self) -> None:
        agents_dir = self.workspace / "agents"
        for name, extra in (("plain", {}), ("wired", {"mcp_servers": ["fake", "ghost"]})):
            (agents_dir / name).mkdir(parents=True)
            (agents_dir / name / "agent.json").write_text(
                json.dumps({"name": name, "skills": [], **extra}), encoding="utf-8"
            )
        plain = self._agent({"fake": _fake_entry()}, agents.load_profile(agents_dir, "plain", LIBRARY))
        self.assertFalse([n for n in plain.tools.names() if "mcp" in n])
        wired = self._agent({"fake": _fake_entry()}, agents.load_profile(agents_dir, "wired", LIBRARY))
        self.assertIn("mcp__fake__echo", wired.tools.names())
        self.assertTrue(any("ghost" in line for line in wired.mcp.warnings()))

    def test_failed_server_does_not_stop_the_agent(self) -> None:
        agent = self._agent({"broken": {"command": "definitely-not-a-real-command-xyz"}, "fake": _fake_entry()})
        self.assertIn("mcp__fake__echo", agent.tools.names())
        states = {state.config.name: state for state in agent.mcp.states}
        self.assertEqual(states["broken"].status, "failed")
        self.assertTrue(any("broken" in line for line in agent.mcp.warnings()))

    def test_agent_runs_an_mcp_tool(self) -> None:
        agent = self._agent({"fake": _fake_entry()})
        llm = ScriptedLLM("mcp__fake__echo", {"text": "via agent"})
        agent.llm = llm
        result = agent.run("say something")
        self.assertIn("echo: via agent", result.text)
        system = llm.calls[0][0].content
        self.assertIn("MCP server 'fake' instructions", system)
        self.assertIn("prefer the echo tool", system)

    def test_tool_results_map_to_agent_results(self) -> None:
        agent = self._agent({"fake": _fake_entry()})
        context = ToolContext(workspace=self.workspace)
        failed = agent.tools.get("mcp__fake__fail").run(context)
        self.assertFalse(failed.ok)
        self.assertEqual(failed.content, "it broke")
        picture = agent.tools.get("mcp__fake__picture").run(context)
        self.assertTrue(picture.images[0].startswith("data:image/png;base64,"))
        stats = agent.tools.get("mcp__fake__stats").run(context)
        self.assertEqual(json.loads(stats.content), {"count": 3})
        listing = agent.tools.get("list_mcp_resources").run(context)
        self.assertIn("memo://readme", listing.content)
        read = agent.tools.get("read_mcp_resource").run(context, server="fake", uri="memo://readme")
        self.assertIn("hello from a resource", read.content)
        logo = agent.tools.get("read_mcp_resource").run(context, server="fake", uri="memo://logo")
        self.assertEqual(len(logo.images), 1)

    def test_tools_added_by_the_server_appear_on_the_next_run(self) -> None:
        agent = self._agent({"fake": _fake_entry()})
        agent.llm = ScriptedLLM("mcp__fake__grow", {})
        agent.run("grow")
        self.assertNotIn("mcp__fake__late", agent.tools.names())
        agent.llm = ScriptedLLM("mcp__fake__late", {})
        self.assertIn("late tool ran", agent.run("use the new tool").text)
        self.assertIn("mcp__fake__late", agent.tools.names())

    def test_mcp_command(self) -> None:
        agent = self._agent({"fake": _fake_entry()})
        registry = CommandRegistry(self.workspace / "commands", self.workspace / "global")
        ctx = CommandContext(session=ChatSession(agent), registry=registry)
        status = registry.dispatch(ctx, "/mcp").output
        self.assertIn("fake", status)
        self.assertIn("connected", status)
        self.assertIn("echo", registry.dispatch(ctx, "/mcp tools").output)
        self.assertIn("memo://readme", registry.dispatch(ctx, "/mcp resources fake").output)
        self.assertIn("review", registry.dispatch(ctx, "/mcp prompts").output)
        prompt = registry.dispatch(ctx, "/mcp prompt fake review path=a.py")
        self.assertEqual(prompt.agent_prompt, "Please review a.py.")
        self.assertIn("Unknown MCP server", registry.dispatch(ctx, "/mcp tools nope").output)
        self.assertIn("Reconnected", registry.dispatch(ctx, "/mcp reconnect fake").output)
        self.assertIn("mcp__fake__echo", agent.tools.names())

    def test_mcp_command_without_servers(self) -> None:
        config = _config(self.workspace)
        agent = build_agent(config, agents.default_profile(config), None, StopController("x"))
        registry = CommandRegistry(self.workspace / "commands", self.workspace / "global")
        ctx = CommandContext(session=ChatSession(agent), registry=registry)
        self.assertIn("No MCP servers", registry.dispatch(ctx, "/mcp").output)


class FakeAgent:
    def __init__(self, data=None) -> None:
        self.data = data
        self.calls: list[tuple] = []

    def run(self, instruction, context=None, output_schema=None):
        self.calls.append((instruction, context, output_schema))
        if output_schema is not None:
            return RunResult(json.dumps(self.data), data=self.data)
        return RunResult(f"done: {instruction}")


class ServeMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        self.config = _config(self.workspace)
        self.out = io.StringIO()
        self.agent = FakeAgent(data={"answer": 42})
        self.grants: list = []

        def factory(grant, stop):
            self.grants.append(grant)
            return self.agent

        self.server = McpAgentServer(
            self.config, agents.default_profile(self.config), self.out, agent_factory=factory
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _exchange(self, *messages: dict) -> list[dict]:
        for message in messages:
            self.server.handle_line(json.dumps(message))
        for thread in self.server._threads:
            thread.join(5)
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def test_initialize_and_list(self) -> None:
        replies = self._exchange(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "nope"},
        )
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", replies[0]["result"]["capabilities"])
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], TOOL_NAME)
        self.assertEqual(replies[2]["error"]["code"], -32601)

    def test_call_runs_a_task(self) -> None:
        replies = self._exchange(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": {"instruction": "summarise", "context": {"k": 1}}},
            }
        )
        self.assertEqual(replies[0]["id"], 7)
        self.assertFalse(replies[0]["result"]["isError"])
        self.assertEqual(replies[0]["result"]["content"][0]["text"], "done: summarise")
        self.assertEqual(self.agent.calls[0][1], {"k": 1})

    def test_output_schema_returns_structured_content(self) -> None:
        schema = {"type": "object", "properties": {"answer": {"type": "number"}}}
        replies = self._exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": {"instruction": "x", "output_schema": schema}},
            }
        )
        self.assertEqual(replies[0]["result"]["structuredContent"], {"answer": 42})

    def test_workspace_grant_is_checked(self) -> None:
        inside = self.workspace / "case"
        replies = self._exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": {"instruction": "x", "workspace": "case"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": TOOL_NAME,
                    "arguments": {"instruction": "x", "workspace": str(self.workspace.parent / "elsewhere")},
                },
            },
        )
        by_id = {reply["id"]: reply for reply in replies}
        self.assertFalse(by_id[1]["result"]["isError"])
        self.assertEqual(self.grants[0].workspace, inside)
        self.assertTrue(by_id[2]["result"]["isError"])
        self.assertIn("outside", by_id[2]["result"]["content"][0]["text"])

    def test_bad_calls_are_tool_errors(self) -> None:
        replies = self._exchange(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": TOOL_NAME, "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "other"}},
        )
        by_id = {reply["id"]: reply for reply in replies}
        self.assertTrue(by_id[1]["result"]["isError"])
        self.assertEqual(by_id[2]["error"]["code"], -32602)

    def test_parse_error(self) -> None:
        self.server.handle_line("{not json")
        self.assertEqual(json.loads(self.out.getvalue())["error"]["code"], -32700)

    def test_end_to_end_over_stdio(self) -> None:
        """Little Agent as an MCP client of Little Agent as an MCP server."""

        env = {
            "LITTLE_AGENT_WORKSPACE": str(self.workspace),
            "OPENAI_API_KEY": "",
            "LITTLE_AGENT_ENABLE_LOGGING": "false",
            "LITTLE_AGENT_GLOBAL_MCP_CONFIG": str(self.workspace / "none.json"),
            "PYTHONPATH": str(REPO),
        }
        (self.workspace / "hello.txt").write_text("hi", encoding="utf-8")
        client = McpClient(
            StdioTransport(sys.executable, ("-m", "little_agent", "serve-mcp"), env=env, cwd=self.workspace),
            "self",
            timeout=30,
        ).connect()
        try:
            self.assertEqual(client.server_info["name"], "little-agent/default")
            self.assertEqual([tool["name"] for tool in client.list_tools()], [TOOL_NAME])
            # The no-API fallback LLM answers "list ..." with list_dir.
            result = client.call_tool(TOOL_NAME, {"instruction": "list the workspace"})
            self.assertFalse(result.get("isError"))
            self.assertIn("hello.txt", result["content"][0]["text"])
        finally:
            client.close()


class LaunchTests(unittest.TestCase):
    def test_serve_mcp_parses(self) -> None:
        args = build_parser().parse_args(["serve-mcp", "--agent", "office", "--auto-approve"])
        self.assertEqual(args.command, "serve-mcp")
        self.assertEqual(args.agent, "office")
        self.assertTrue(args.auto_approve)

    def test_build_agent_without_mcp_config_has_no_mcp(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            agent = build_agent(config, agents.default_profile(config), None, StopController("x"))
        self.assertIsNone(agent.mcp)
        self.assertFalse([name for name in agent.tools.names() if "mcp" in name])

    def test_shared_manager_is_reused(self) -> None:
        from little_agent.mcp.manager import shared_manager, shutdown_shared

        with TemporaryDirectory() as tmp:
            path = _write_mcp_json(Path(tmp) / "mcp.json", {})
            config = _config(Path(tmp), mcp_config_paths=(path,))
            granted = replace(config, workspace=(Path(tmp) / "sub").resolve())
            try:
                self.assertIs(shared_manager(config), shared_manager(granted))
            finally:
                shutdown_shared()


if __name__ == "__main__":
    unittest.main()
