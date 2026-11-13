"""Exercise the real harness and HTTP adapter without an LLM/server dependency."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from little_agent.cli import build_memory_store
from little_agent.memory.hindsight import HindsightClient, HindsightError, HindsightMemoryStore
from little_agent.session import ChatSession
from test_memory import _agent, _config, RecordingClient


class FakeMemory:
    bank_id = "one-bank"

    def __init__(self):
        self.queries = []
        self.saved = []
        self.fail = False

    def recall(self, query):
        self.queries.append(query)
        if self.fail:
            raise HindsightError("offline")
        return [{"text": "Use measured diameter", "id": "fact-1", "metadata": {"kind": "teaching"}}]

    def retain(self, content, context, kind):
        if self.fail:
            raise HindsightError("offline")
        self.saved.append((content, context, kind))
        return "doc-1"


class ScriptedClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, model, messages, tools):
        self.calls.append(list(messages))
        return next(self.replies)


class HarnessTests(unittest.TestCase):
    def test_four_hooks_and_no_duplicate_exit_reflection(self):
        with TemporaryDirectory() as tmp:
            backend = FakeMemory()
            store = HindsightMemoryStore(backend, tmp)
            llm = ScriptedClient([
                {"content": "", "tool_calls": [
                    {"id": "1", "name": "hindsight_recall", "arguments": {"query": "table T D column"}},
                    {"id": "2", "name": "hindsight_retain", "arguments": {
                        "content": "Table T D is measured diameter, not design", "source_quote": "D is measured", "kind": "correction"}}]},
                {"content": "Corrected the interpretation.", "tool_calls": []},
                {"content": json.dumps({"memory": "User corrected table T column D to measured diameter."})},
            ])
            session = ChatSession(_agent(Path(tmp), llm, memory=store))
            self.assertEqual(session.send("Table T: D is measured"), "Corrected the interpretation.")
            self.assertEqual(len(backend.queries), 2)
            self.assertIn("Use measured diameter", str(llm.calls[0]))
            self.assertEqual([x[2] for x in backend.saved], ["correction", "experience"])
            self.assertIn("Table T", backend.saved[0][1])
            self.assertFalse(session.learn())
            self.assertEqual(len(llm.calls), 3)

    def test_recall_failure_is_visible_but_does_not_block_answer(self):
        with TemporaryDirectory() as tmp:
            backend = FakeMemory(); backend.fail = True
            llm = RecordingClient(["Need the criterion"])
            store = HindsightMemoryStore(backend, tmp, auto_learning=False)
            answer = _agent(Path(tmp), llm, memory=store).run("judge this")
            self.assertEqual(answer.text, "Need the criterion")
            self.assertIn("lookup unavailable", str(llm.calls[0]))

    def test_source_quote_must_be_current_user_evidence(self):
        backend = FakeMemory(); store = HindsightMemoryStore(backend, "/tmp")
        store.prepare_run("What is D?", [])
        result = store.tools()[1].run(None, content="D is design", source_quote="D is design", kind="teaching")
        self.assertFalse(result.ok)
        self.assertEqual(backend.saved, [])

    def test_pending_question_and_no_learning_do_not_retain(self):
        with TemporaryDirectory() as tmp:
            backend = FakeMemory(); store = HindsightMemoryStore(backend, tmp)
            llm = RecordingClient(["Which table?", '{"memory": ""}'])
            _agent(Path(tmp), llm, memory=store).run("Judge D")
            self.assertFalse(backend.saved)

    def test_failed_reflection_can_retry(self):
        from little_agent.messages import Message
        backend = FakeMemory(); backend.fail = True
        store = HindsightMemoryStore(backend, "/tmp")
        llm = RecordingClient(['{"memory":"Lesson"}', '{"memory":"Lesson"}'])
        messages = [Message(role="user", content="do it"), Message(role="assistant", content="done")]
        with self.assertRaises(HindsightError):
            store.learn(messages, llm, "local")
        backend.fail = False
        self.assertTrue(store.learn(messages, llm, "local"))
        self.assertFalse(store.learn(messages, llm, "local"))

    def test_step_limit_does_not_reflect(self):
        with TemporaryDirectory() as tmp:
            backend = FakeMemory(); store = HindsightMemoryStore(backend, tmp)
            llm = ScriptedClient([{"content": "", "tool_calls": [{"name":"hindsight_recall", "arguments":{"query":"D"}}]}])
            result = _agent(Path(tmp), llm, memory=store, max_tool_steps=1).run("do work")
            self.assertIn("Stopped after", result.text)
            self.assertFalse(backend.saved)
            self.assertEqual(len(llm.calls), 1)

    def test_config_and_no_memory_do_not_open_network(self):
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp), memory_backend="hindsight")
            with patch("little_agent.memory.hindsight.urlopen", side_effect=AssertionError("network")):
                self.assertIsInstance(build_memory_store(config, True), HindsightMemoryStore)
                store = build_memory_store(config, False)
                llm = RecordingClient(["ok"])
                _agent(Path(tmp), llm, memory=store).run("hello")
                self.assertEqual(store.tools(), [])
            self.assertFalse(store.enabled)


class HttpTests(unittest.TestCase):
    def test_wire_format_unicode_auth_idempotency_and_error(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, payload, self.headers.get("Authorization")))
                self.send_response(503 if payload.get("query") == "fail" else 200)
                self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps({"results": [], "success": True}).encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = HindsightClient(f"http://127.0.0.1:{server.server_port}", "bank /日本語", "test-key")
            self.assertEqual(client.recall("実測径"), [])
            a = client.retain("実測径", "表T", "teaching")
            b = client.retain("実測径", "表T", "teaching")
            self.assertEqual(a, b)
            self.assertTrue(requests[0][0].endswith("/memories/recall"))
            self.assertIn("bank%20%2F", requests[0][0])
            self.assertEqual(requests[0][2], "Bearer test-key")
            self.assertEqual(requests[1][1]["items"][0]["content"], "実測径")
            self.assertFalse(requests[1][1]["async"])
            with self.assertRaisesRegex(HindsightError, "HTTP 503"):
                client.recall("fail")
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_invalid_settings(self):
        for url in ["file:///tmp", "http://user:secret@host", "http://host?secret=1"]:
            with self.assertRaises(ValueError):
                HindsightClient(url, "bank")
        with self.assertRaises(ValueError):
            HindsightClient("http://localhost", "")


if __name__ == "__main__":
    unittest.main()
