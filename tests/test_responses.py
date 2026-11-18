"""Responses wire format and the real agent tool loop, without an API server."""
import io
import json
from dataclasses import replace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from little_agent.agent import Agent
from little_agent.config import AgentConfig
from little_agent.llm import LocalRuleClient, OpenAICompatibleChatClient, OpenAICompatibleResponsesClient
from little_agent.messages import Message
from little_agent.tools import default_tools
from little_agent.tools.base import ToolRegistry
from test_gui import ChatWorker, harness, next_event
from little_agent.control import StopController


def answer(text="完了"):
    return {"status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}


def function(name="list_dir", arguments='{"path":"."}', call_id="call_1"):
    return {"type": "function_call", "id": "fc_1", "call_id": call_id,
            "name": name, "arguments": arguments, "status": "completed"}


def wire(monkeypatch, *responses):
    requests = []
    pending = iter(responses)

    def urlopen(request, timeout):
        requests.append((request, json.loads(request.data)))
        return io.BytesIO(json.dumps(next(pending), ensure_ascii=False).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return requests


def test_responses_text_usage_and_request(monkeypatch):
    response = answer("日本語 😀")
    response["output"].insert(0, {"type": "reasoning", "summary": [],
                                  "content": [{"type": "reasoning_text", "text": "private"}]})
    requests = wire(monkeypatch, response)
    client = OpenAICompatibleResponsesClient("dummy", "http://localhost:8080/v1/", timeout=12)
    result = client.complete("qwen", [Message("system", "指示"), Message("user", "質問")], default_tools())
    request, body = requests[0]
    assert request.full_url == "http://localhost:8080/v1/responses"
    assert request.get_header("Authorization") == "Bearer dummy"
    assert body["input"] == [{"role": "system", "content": "指示"}, {"role": "user", "content": "質問"}]
    assert body["store"] is False
    assert body["tools"][0]["type"] == "function"
    assert "function" not in body["tools"][0]
    assert body["tools"][0]["strict"] is False
    assert result["content"] == "日本語 😀"
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}


def test_responses_without_tools_or_shared_history(monkeypatch):
    requests = wire(monkeypatch, answer(), answer())
    client = OpenAICompatibleResponsesClient("dummy")
    client.complete("qwen", [Message("user", "first conversation")], ToolRegistry())
    client.complete("qwen", [Message("user", "second conversation")], ToolRegistry())
    assert "tools" not in requests[0][1]
    assert "first conversation" not in json.dumps(requests[1][1])
    assert "previous_response_id" not in requests[1][1]


def test_responses_image_input_and_manual_tool_history(monkeypatch):
    requests = wire(monkeypatch, answer())
    client = OpenAICompatibleResponsesClient("dummy")
    client.complete("qwen", [
        Message("assistant", "確認します", tool_calls=[{"id": "c1", "name": "capture", "arguments": {}}]),
        Message("tool", "OK", tool_call_id="c1"),
        Message("user", [{"type": "text", "text": "画像"}, {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,abc", "detail": "low"}}]),
    ], ToolRegistry())
    items = requests[0][1]["input"]
    assert items[1] == {"type": "function_call", "call_id": "c1", "name": "capture", "arguments": "{}"}
    assert items[2] == {"type": "function_call_output", "call_id": "c1", "output": "OK"}
    assert items[3]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,abc", "detail": "low"}


def test_responses_worker_executes_tools_replays_reasoning_and_reports(monkeypatch, tmp_path):
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    first = {"status": "completed", "output": [reasoning, *answer("ファイルを作成して確認します")["output"],
        function("write_file", json.dumps({"path": "result.txt", "content": "日本語 😀"}), "write_1"),
        function("read_file", '{"path":"result.txt"}', "read_2")]}
    requests = wire(monkeypatch, first, answer("保存と確認が完了しました"))
    stop = StopController("x")
    worker = ChatWorker(lambda confirm: harness(tmp_path, confirm, stop), stop, lambda: None)
    worker.start()
    ctx = next_event(worker, "ready")
    ctx.agent.llm = OpenAICompatibleResponsesClient("dummy")
    try:
        worker.submit("ファイルを作成して確認")
        assert next_event(worker, "reply") == "保存と確認が完了しました"
        assert (ctx.agent.config.workspace / "result.txt").read_text(encoding="utf-8") == "日本語 😀"
        items = requests[1][1]["input"]
        assert items[2:6] == first["output"]
        outputs = [item for item in items if item.get("type") == "function_call_output"]
        assert [item["call_id"] for item in outputs] == ["write_1", "read_2"]
        assert "日本語 😀" in outputs[1]["output"]
        assert "opaque" not in str(ctx.session.history)
    finally:
        worker.close()
        worker.thread.join(3)
    assert not worker.thread.is_alive()


@pytest.mark.parametrize("response", [
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [function()]},
    {"status": "failed", "error": {"message": "failed"}},
    {"status": "in_progress", "output": []},
    {"status": "completed"},
    {"status": "completed", "output": []},
    {"output": [{"type": "reasoning", "summary": []}]},
    {"output": ["invalid"]},
    {"output": [function(arguments='{"path":')]},
    {"output": [function(arguments="[]")]},
    {"output": [function(call_id="")]},
    {"output": [{**function(), "status": "incomplete"}]},
])
def test_responses_rejects_incomplete_or_malformed_results(monkeypatch, response):
    wire(monkeypatch, response)
    with pytest.raises(RuntimeError):
        OpenAICompatibleResponsesClient("dummy").complete("qwen", [], default_tools())


def test_responses_refusal(monkeypatch):
    wire(monkeypatch, {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "できません"}]}]})
    result = OpenAICompatibleResponsesClient("dummy").complete("qwen", [], ToolRegistry())
    assert result["content"] == "できません"


def test_responses_http_error_is_descriptive(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=HTTPError(
        "http://localhost/v1/responses", 404, "Not Found", {}, io.BytesIO(b"unsupported route"))))
    with pytest.raises(RuntimeError, match="Responses API returned HTTP 404"):
        OpenAICompatibleResponsesClient("dummy").complete("qwen", [], ToolRegistry())


def test_api_mode_configuration_and_client_selection(monkeypatch):
    monkeypatch.setenv("LITTLE_AGENT_API_MODE", " Responses ")
    monkeypatch.setattr("little_agent.config.load_dotenv", lambda: None)
    config = AgentConfig.from_env()
    assert config.api_mode == "responses"
    config = replace(config, openai_api_key="dummy")
    assert isinstance(Agent._default_llm(config), OpenAICompatibleResponsesClient)
    chat = Agent._default_llm(replace(config, api_mode="chat_completions"))
    assert type(chat) is OpenAICompatibleChatClient
    assert isinstance(Agent._default_llm(replace(config, openai_api_key=None)), LocalRuleClient)
    monkeypatch.delenv("LITTLE_AGENT_API_MODE")
    assert AgentConfig.from_env().api_mode == "chat_completions"
    monkeypatch.setenv("LITTLE_AGENT_API_MODE", "typo")
    with pytest.raises(ValueError, match="LITTLE_AGENT_API_MODE"):
        AgentConfig.from_env()
