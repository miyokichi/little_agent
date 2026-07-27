"""End-to-end tests for A2A delegation.

These drive a **real** A2A server over HTTP with the real client, so the Agent
Card, JSON-RPC envelope, and task lifecycle are all exercised for real.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent import agents
from little_agent.a2a import models
from little_agent.a2a.client import A2AClient, A2AClientError, fetch_agent_card
from little_agent.a2a.peers import PeerPool, parse_peers
from little_agent.a2a.server import A2AService, serve
from little_agent.config import AgentConfig
from little_agent.factory import build_agent
from little_agent.control import StopController
from little_agent.tools.base import ToolContext
from little_agent.tools.delegation import DelegateTasksTool, DelegateTaskTool

LIBRARY = Path("skills").resolve()


class ScriptedAgent:
    """Stands in for a real Agent inside the A2A server."""

    def __init__(self, reply: str = "done", block: threading.Event | None = None) -> None:
        self.reply = reply
        self.block = block
        self.prompts: list[str] = []
        self.depth: int | None = None

    def run(self, user_text: str) -> str:
        self.prompts.append(user_text)
        if self.block is not None:
            self.block.wait(5)
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


def _card(port: int, name: str = "test-agent", requires_auth: bool = False) -> dict:
    return models.agent_card(
        name=name,
        description="test",
        url=f"http://127.0.0.1:{port}/",
        version="0.1.0",
        skills=[models.agent_skill("general", "general", "general help")],
        requires_auth=requires_auth,
    )


class ServedAgent:
    """Context manager running an A2AService on a real loopback port."""

    def __init__(self, agent, token: str | None = None, grace: float = 2.0) -> None:
        self._agent = agent
        self._token = token
        self._grace = grace
        self.depths: list[int] = []

    def __enter__(self) -> "ServedAgent":
        def factory(depth, stop):
            self.depths.append(depth)
            self._agent.depth = depth
            self._agent.stop = stop
            return self._agent

        self.service = A2AService(
            _card(0, requires_auth=bool(self._token)),
            factory,
            token=self._token,
            grace_seconds=self._grace,
        )
        # Bind port 0, then publish the real port in the card the client reads.
        self.httpd = serve(self.service, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/"
        self.service.card["url"] = self.base_url
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class AgentCardTests(unittest.TestCase):
    def test_card_served_at_canonical_and_legacy_paths(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            for path in (models.AGENT_CARD_PATH, models.LEGACY_AGENT_CARD_PATH):
                with urllib.request.urlopen(served.base_url.rstrip("/") + path, timeout=5) as resp:
                    card = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(card["protocolVersion"], models.PROTOCOL_VERSION)
                self.assertEqual(card["preferredTransport"], "JSONRPC")
                self.assertIn("skills", card)
                # Streaming is genuinely unimplemented, so it must not be advertised.
                self.assertFalse(card["capabilities"]["streaming"])

    def test_client_discovers_card(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            card = fetch_agent_card(served.base_url)
            self.assertEqual(card["name"], "test-agent")


class MessageSendTests(unittest.TestCase):
    def test_task_completes_and_returns_artifact(self) -> None:
        agent = ScriptedAgent("the answer is 42")
        with ServedAgent(agent) as served:
            client = A2AClient.connect(served.base_url)
            task = client.run_task("what is the answer?")

            self.assertEqual(task["kind"], "task")
            self.assertEqual(task["status"]["state"], models.TASK_COMPLETED)
            self.assertEqual(models.task_result_text(task), "the answer is 42")
            self.assertEqual(agent.prompts, ["what is the answer?"])

    def test_depth_travels_in_message_metadata(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            client = A2AClient.connect(served.base_url)
            client.run_task("go", depth=2)
            self.assertEqual(served.depths, [2])

    def test_tasks_get_returns_the_same_task(self) -> None:
        with ServedAgent(ScriptedAgent("ok")) as served:
            client = A2AClient.connect(served.base_url)
            task = client.run_task("go")
            fetched = client.get_task(task["id"])
            self.assertEqual(fetched["id"], task["id"])
            self.assertEqual(fetched["status"]["state"], models.TASK_COMPLETED)

    def test_agent_failure_becomes_failed_task(self) -> None:
        class Boom:
            def run(self, _text):
                raise RuntimeError("kaboom")

        with ServedAgent(Boom()) as served:
            client = A2AClient.connect(served.base_url)
            task = client.run_task("go")
            self.assertEqual(task["status"]["state"], models.TASK_FAILED)
            self.assertIn("kaboom", models.task_result_text(task))

    def test_long_task_returns_non_terminal_then_polls_to_completion(self) -> None:
        gate = threading.Event()
        agent = ScriptedAgent("slow result", block=gate)
        # Grace of 0 forces message/send to hand back a working task to poll.
        with ServedAgent(agent, grace=0.0) as served:
            client = A2AClient.connect(served.base_url)
            first = client.send_message("go")
            self.assertIn(first["status"]["state"], {models.TASK_SUBMITTED, models.TASK_WORKING})
            gate.set()
            task = client.run_task("go2")
            self.assertEqual(task["status"]["state"], models.TASK_COMPLETED)


class CancelTests(unittest.TestCase):
    def test_cancel_marks_task_canceled_and_trips_stop(self) -> None:
        gate = threading.Event()
        agent = ScriptedAgent("never used", block=gate)
        with ServedAgent(agent, grace=0.0) as served:
            client = A2AClient.connect(served.base_url)
            task = client.send_message("long job")
            canceled = client.cancel_task(task["id"])

            self.assertEqual(canceled["status"]["state"], models.TASK_CANCELED)
            # The served agent's stop controller is tripped, which is what aborts
            # a real Agent between tool calls.
            self.assertTrue(agent.stop.triggered)
            gate.set()

    def test_cancel_unknown_task_returns_task_not_found(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            client = A2AClient.connect(served.base_url)
            with self.assertRaises(A2AClientError) as caught:
                client.cancel_task("does-not-exist")
            self.assertIn(str(models.TASK_NOT_FOUND), str(caught.exception))

    def test_cancel_completed_task_is_rejected(self) -> None:
        with ServedAgent(ScriptedAgent("fast")) as served:
            client = A2AClient.connect(served.base_url)
            task = client.run_task("go")
            with self.assertRaises(A2AClientError) as caught:
                client.cancel_task(task["id"])
            self.assertIn(str(models.TASK_NOT_CANCELABLE), str(caught.exception))


class ProtocolErrorTests(unittest.TestCase):
    def _post(self, base_url: str, payload: dict) -> dict:
        request = urllib.request.Request(
            base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_unknown_method(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            body = self._post(
                served.base_url, {"jsonrpc": "2.0", "id": 1, "method": "nope", "params": {}}
            )
            self.assertEqual(body["error"]["code"], models.METHOD_NOT_FOUND)
            self.assertEqual(body["id"], 1)

    def test_streaming_reports_unsupported_operation(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            body = self._post(
                served.base_url,
                {"jsonrpc": "2.0", "id": 2, "method": "message/stream", "params": {}},
            )
            self.assertEqual(body["error"]["code"], models.UNSUPPORTED_OPERATION)

    def test_wrong_jsonrpc_version(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            body = self._post(served.base_url, {"jsonrpc": "1.0", "id": 3, "method": "tasks/get"})
            self.assertEqual(body["error"]["code"], models.INVALID_REQUEST)

    def test_message_without_text_part_is_rejected(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            body = self._post(
                served.base_url,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "message/send",
                    "params": {"message": {"kind": "message", "role": "user", "parts": []}},
                },
            )
            self.assertEqual(body["error"]["code"], models.CONTENT_TYPE_NOT_SUPPORTED)


class AuthTests(unittest.TestCase):
    def test_token_required_when_configured(self) -> None:
        with ServedAgent(ScriptedAgent(), token="s3cret") as served:
            # The card stays public so peers can learn how to authenticate.
            card = fetch_agent_card(served.base_url)
            self.assertIn("securitySchemes", card)

            with self.assertRaises(A2AClientError):
                A2AClient(card).run_task("go")

            authorized = A2AClient(card, token="s3cret")
            self.assertEqual(authorized.run_task("go")["status"]["state"], models.TASK_COMPLETED)


class DelegateToolTests(unittest.TestCase):
    def test_delegates_over_a2a_to_a_url(self) -> None:
        agent = ScriptedAgent("peer finished the research")
        with TemporaryDirectory() as tmp, ServedAgent(agent) as served:
            root = Path(tmp)
            config = _config(root, root / "agents")
            tool = DelegateTaskTool(config=config, depth=0, pool=PeerPool(config))
            result = tool.run(
                ToolContext(root), task="research X", background="use Y", agent_url=served.base_url
            )

            self.assertTrue(result.ok, result.content)
            self.assertIn("peer finished the research", result.content)
            self.assertIn("research X", agent.prompts[0])
            self.assertIn("use Y", agent.prompts[0])
            # The parent's depth+1 is what the peer is told to run at.
            self.assertEqual(served.depths, [1])

    def test_empty_task_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            tool = DelegateTaskTool(config=config, pool=PeerPool(config))
            self.assertFalse(tool.run(ToolContext(root), task="  ").ok)

    def test_depth_limit_blocks_further_delegation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=2)
            tool = DelegateTaskTool(config=config, depth=2, pool=PeerPool(config))
            result = tool.run(ToolContext(root), task="anything")
            self.assertFalse(result.ok)
            self.assertIn("depth limit", result.content)

    def test_unreachable_peer_reports_cleanly(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            tool = DelegateTaskTool(config=config, pool=PeerPool(config))
            result = tool.run(
                ToolContext(root), task="t", agent_url="http://127.0.0.1:9/"
            )
            self.assertFalse(result.ok)
            self.assertIn("peer", result.content.lower())

    def test_failed_peer_task_surfaces_as_tool_error(self) -> None:
        class Boom:
            def run(self, _text):
                raise RuntimeError("peer exploded")

        with TemporaryDirectory() as tmp, ServedAgent(Boom()) as served:
            root = Path(tmp)
            config = _config(root, root / "agents")
            tool = DelegateTaskTool(config=config, pool=PeerPool(config))
            result = tool.run(ToolContext(root), task="t", agent_url=served.base_url)
            self.assertFalse(result.ok)
            self.assertIn("peer exploded", result.content)


class SlowAgent:
    """Sleeps for a fixed time so concurrency is observable in wall-clock terms."""

    def __init__(self, delay: float, reply: str = "ok") -> None:
        self.delay = delay
        self.reply = reply
        self.concurrent = 0
        self.peak = 0
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def run(self, user_text: str) -> str:
        with self._lock:
            self.prompts.append(user_text)
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            time.sleep(self.delay)
            return f"{self.reply}:{user_text}"
        finally:
            with self._lock:
                self.concurrent -= 1


class ParallelDelegationTests(unittest.TestCase):
    def _tool(self, root: Path, max_parallel: int = 4, stop=None) -> DelegateTasksTool:
        config = _config(root, root / "agents")
        config = replace(config, max_parallel_delegations=max_parallel)
        return DelegateTasksTool(config=config, pool=PeerPool(config), stop=stop)

    def test_subtasks_run_concurrently(self) -> None:
        agent = SlowAgent(delay=0.6)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp))
            started = time.monotonic()
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[
                    {"task": "alpha", "agent_url": served.base_url},
                    {"task": "bravo", "agent_url": served.base_url},
                    {"task": "charlie", "agent_url": served.base_url},
                ],
            )
            elapsed = time.monotonic() - started

            self.assertTrue(result.ok, result.content)
            # Three 0.6s tasks would take ~1.8s sequentially; in parallel they overlap.
            self.assertLess(elapsed, 1.5, f"took {elapsed:.2f}s — subtasks did not overlap")
            self.assertGreater(agent.peak, 1, "no two subtasks were ever in flight together")
            for name in ("alpha", "bravo", "charlie"):
                self.assertIn(name, result.content)

    def test_results_keep_request_order(self) -> None:
        # Later subtasks finish first, but output order must match the request.
        agent = SlowAgent(delay=0.0)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp))
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[
                    {"task": "first", "agent_url": served.base_url},
                    {"task": "second", "agent_url": served.base_url},
                ],
            )
            self.assertLess(result.content.index("first"), result.content.index("second"))
            self.assertIn("[1/2]", result.content)
            self.assertIn("[2/2]", result.content)

    def test_concurrency_is_capped(self) -> None:
        agent = SlowAgent(delay=0.4)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp), max_parallel=2)
            tool.run(
                ToolContext(Path(tmp)),
                tasks=[{"task": f"t{i}", "agent_url": served.base_url} for i in range(5)],
            )
            self.assertLessEqual(agent.peak, 2, f"peak concurrency was {agent.peak}, cap was 2")

    def test_partial_failure_keeps_good_results(self) -> None:
        agent = SlowAgent(delay=0.0)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp))
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[
                    {"task": "good", "agent_url": served.base_url},
                    {"task": "bad", "agent_url": "http://127.0.0.1:9/"},
                ],
            )
            # One peer is unreachable, but the successful result must survive.
            self.assertTrue(result.ok, result.content)
            self.assertIn("1 completed, 1 failed", result.content)
            self.assertIn("good", result.content)
            self.assertIn("FAILED", result.content)

    def test_all_failed_is_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            tool = self._tool(Path(tmp))
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[
                    {"task": "a", "agent_url": "http://127.0.0.1:9/"},
                    {"task": "b", "agent_url": "http://127.0.0.1:9/"},
                ],
            )
            self.assertFalse(result.ok)
            self.assertIn("0 completed, 2 failed", result.content)

    def test_empty_and_malformed_input_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            tool = self._tool(Path(tmp))
            self.assertFalse(tool.run(ToolContext(Path(tmp)), tasks=[]).ok)
            self.assertFalse(tool.run(ToolContext(Path(tmp)), tasks=["not an object"]).ok)

    def test_depth_limit_blocks_parallel_delegation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=2)
            tool = DelegateTasksTool(config=config, depth=2, pool=PeerPool(config))
            result = tool.run(ToolContext(root), tasks=[{"task": "x"}])
            self.assertFalse(result.ok)
            self.assertIn("depth limit", result.content)

    def test_stop_abandons_delegation_and_cancels_peer(self) -> None:
        stop = StopController("<ctrl>+<alt>+q")
        agent = SlowAgent(delay=3.0)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp), stop=stop)

            # Trip the emergency stop shortly after the delegation starts.
            threading.Timer(0.4, stop._on_activate).start()
            started = time.monotonic()
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[{"task": "long", "agent_url": served.base_url}],
            )
            elapsed = time.monotonic() - started

            self.assertFalse(result.ok)
            self.assertIn("Stopped", result.content)
            # Returned well before the peer's 3s task would have finished.
            self.assertLess(elapsed, 2.5, f"took {elapsed:.2f}s — stop was not honored")
            # The abandoned peer task was cancelled rather than left running.
            self.assertTrue(agent.stop.triggered)


class PeerRegistryTests(unittest.TestCase):
    def test_parse_named_and_bare_urls(self) -> None:
        peers = parse_peers("office=http://127.0.0.1:8801/, http://example.com:9000/")
        self.assertEqual(peers["office"], "http://127.0.0.1:8801/")
        self.assertIn("example.com-9000", peers)

    def test_empty_input(self) -> None:
        self.assertEqual(parse_peers(None), {})
        self.assertEqual(parse_peers("  "), {})

    def test_available_includes_local_profiles(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            agents.create_agent(agents_dir, LIBRARY, "office", skills=["datetime"])
            pool = PeerPool(_config(root, agents_dir))
            self.assertIn("office", pool.available())
            self.assertIn(agents.DEFAULT_AGENT_NAME, pool.available())

    def test_unknown_local_profile_raises_before_spawning(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = PeerPool(_config(root, root / "agents"))
            with self.assertRaises(FileNotFoundError):
                pool.connect(name="does-not-exist")


class LocalSpawnTests(unittest.TestCase):
    """Spawning a real local A2A server subprocess for a profile.

    Only the spawn/discovery path is exercised (no task is sent), so the test
    never reaches an LLM even when the environment has API credentials.
    """

    def test_local_profile_is_served_and_discoverable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            agents.create_agent(agents_dir, LIBRARY, "helper", skills=["datetime"])
            pool = PeerPool(_config(root, agents_dir))
            try:
                client = pool.connect(name="helper")
                self.assertEqual(client.name, "little-agent/helper")
                # The profile's skills are advertised on the card.
                self.assertIn("datetime", [s["id"] for s in client.card["skills"]])
                # A second connect reuses the already-running server.
                self.assertEqual(pool.connect(name="helper").endpoint, client.endpoint)
            finally:
                pool.shutdown()

    def test_parallel_connects_start_exactly_one_server(self) -> None:
        """Concurrent delegations to one local profile must not race the spawn.

        Without per-name serialization a second caller could pick up the URL of a
        server that has not started listening yet, or start a duplicate.
        """

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            agents.create_agent(agents_dir, LIBRARY, "helper", skills=["datetime"])
            pool = PeerPool(_config(root, agents_dir))
            try:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    clients = list(
                        executor.map(lambda _: pool.connect(name="helper"), range(4))
                    )
                endpoints = {client.endpoint for client in clients}
                self.assertEqual(len(endpoints), 1, f"spawned more than one server: {endpoints}")
            finally:
                pool.shutdown()


class BuildAgentDelegationTests(unittest.TestCase):
    def test_delegate_tool_registered_at_depth_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            agent = build_agent(
                config, agents.default_profile(config), lambda *_: True, StopController("x")
            )
            self.assertIn("delegate_task", agent.tools.names())

    def test_delegate_tool_absent_at_depth_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=2)
            agent = build_agent(
                config, agents.default_profile(config), lambda *_: True, StopController("x"), depth=2
            )
            self.assertNotIn("delegate_task", agent.tools.names())

    def test_delegation_can_be_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=0)
            agent = build_agent(
                config, agents.default_profile(config), lambda *_: True, StopController("x")
            )
            self.assertNotIn("delegate_task", agent.tools.names())


if __name__ == "__main__":
    unittest.main()
