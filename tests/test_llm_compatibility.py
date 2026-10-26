"""Chat Completions contract and runtime regressions; no live API required."""

from io import BytesIO
import json
from unittest.mock import Mock, patch

import pytest

from little_agent.a2a.models import new_message
from little_agent.a2a.server import A2AService
from little_agent.agent import Agent
from little_agent.config import AgentConfig
from little_agent.llm import LLMResponseError, OpenAICompatibleChatClient
from little_agent.memory.reflection import Reflector
from little_agent.messages import Message
from little_agent.session import ChatSession
from little_agent.skills.loader import SkillLoader
from little_agent.tools.base import ToolRegistry, ToolResult


def completion(reason="stop", content="done", **message):
    return {"choices": [{"finish_reason": reason, "message": {
        "role": "assistant", "content": content, **message,
    }}], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}


def tool_call(arguments="{}", call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {
        "name": "record", "arguments": arguments,
    }}


@pytest.fixture
def client():
    return OpenAICompatibleChatClient("test-key")


@pytest.fixture
def agent(tmp_path, client):
    config = AgentConfig(
        model="test", workspace=tmp_path, require_confirmation=False,
        openai_api_key=None, openai_base_url="https://api.openai.com/v1",
    )
    return Agent(config, SkillLoader(tmp_path / "skills"), tools=ToolRegistry(),
                 llm=client, stop=Mock(triggered=False))


def test_empty_registry_omits_tools_and_choice(client):
    with patch.object(client, "_post_json", return_value=completion()) as post:
        result = client.complete("test", [Message("user", "hello")], ToolRegistry())
    assert post.call_args.args == ("/chat/completions", {
        "model": "test", "messages": [{"role": "user", "content": "hello"}],
    })
    assert result["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 5


def test_http_request_uses_chat_completions_json(client):
    with patch("urllib.request.urlopen", return_value=BytesIO(json.dumps(completion()).encode())) as send:
        client.complete("test", [Message("developer", "instructions"), Message("user", "hello")], ToolRegistry())
    request = send.call_args.args[0]
    assert request.full_url == "https://api.openai.com/v1/chat/completions"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data)["messages"][0]["role"] == "developer"


@pytest.mark.parametrize("raw", [b"[]", b"not json"])
def test_non_object_or_invalid_json_is_a_response_error(client, raw):
    with patch("urllib.request.urlopen", return_value=BytesIO(raw)):
        with pytest.raises(LLMResponseError):
            client.complete("test", [], ToolRegistry())


@pytest.mark.parametrize("reason", ["length", "content_filter", None, "unknown"])
def test_incomplete_or_unknown_finish_never_becomes_final(agent, client, reason):
    with patch.object(client, "_post_json", return_value=completion(reason, "partial")) as post:
        with pytest.raises(LLMResponseError, match="finish_reason"):
            agent.run("work")
    assert post.call_count == 1
    agent.stop.disarm.assert_called_once()


@pytest.mark.parametrize("reason,calls", [
    ("tool_calls", []), ("function_call", []), ("stop", [tool_call()]),
])
def test_inconsistent_finish_reason_is_rejected(client, reason, calls):
    with patch.object(client, "_post_json", return_value=completion(reason, tool_calls=calls)):
        with pytest.raises(LLMResponseError, match="finish_reason"):
            client.complete("test", [], ToolRegistry())


@pytest.mark.parametrize("response", [
    {}, {"choices": []}, {"choices": [None]}, {"choices": [{}]},
    {"choices": [{"finish_reason": "stop", "message": []}]},
    completion(content={"invalid": "content"}),
    completion(refusal=42),
])
def test_malformed_response_is_a_controlled_error(client, response):
    with patch.object(client, "_post_json", return_value=response):
        with pytest.raises(LLMResponseError):
            client.complete("test", [], ToolRegistry())


@pytest.mark.parametrize("call", [
    None, {}, {**tool_call(), "id": ""}, {**tool_call(), "type": "custom"},
    {**tool_call(), "function": {}}, tool_call(arguments={}),
])
def test_malformed_tool_call_is_not_dropped_or_repaired(client, call):
    with patch.object(client, "_post_json", return_value=completion("tool_calls", tool_calls=[call])):
        with pytest.raises(LLMResponseError):
            client.complete("test", [], ToolRegistry())


def test_duplicate_call_ids_are_rejected(client):
    with patch.object(client, "_post_json", return_value=completion(
        "tool_calls", tool_calls=[tool_call(), tool_call()],
    )):
        with pytest.raises(LLMResponseError, match="duplicate"):
            client.complete("test", [], ToolRegistry())


def test_refusal_with_null_content_is_visible(agent, client):
    with patch.object(client, "_post_json", return_value=completion(content=None, refusal="Cannot comply.")):
        assert agent.run("work").text == "Cannot comply."


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_partial_tool_call_is_never_executed(agent, client, reason):
    with patch.object(client, "_post_json", return_value=completion(
        reason, content=None, tool_calls=[tool_call(arguments='{"unfinished":')],
    )), patch.object(agent, "_run_tool") as execute:
        with pytest.raises(LLMResponseError, match=reason):
            agent.run("work")
    execute.assert_not_called()


def test_tool_results_follow_all_calls_and_preserve_raw_arguments(agent, client):
    recorded = []

    class Record:
        name = "record"
        description = "Record a value"
        requires_confirmation = False
        parameters = {"type": "object", "properties": {"value": {"type": "integer"}},
                      "required": ["value"], "additionalProperties": False}

        def run(self, context, **arguments):
            recorded.append(arguments["value"])
            return ToolResult(True, "recorded")

    agent.tools.register(Record())
    raw = '{ "value" : 1 }'
    with patch.object(client, "_post_json", side_effect=[
        completion("tool_calls", content=None, tool_calls=[
            tool_call(raw, "a"), tool_call('{"value":2}', "b"),
        ]), completion(),
    ]) as post:
        assert agent.run("work").text == "done"
    assert recorded == [1, 2]
    body = post.call_args_list[1].args[1]
    assert body["tool_choice"] == "auto"
    assert body["tools"][0]["type"] == "function"
    assistant, first, second = body["messages"][-3:]
    assert assistant["tool_calls"][0]["function"]["arguments"] == raw
    assert [first["tool_call_id"], second["tool_call_id"]] == ["a", "b"]
    assert first["role"] == second["role"] == "tool"


@pytest.mark.parametrize("arguments", [
    "", "{broken", '{"value":1', "null", "[]", "0", "{}", '{"value":NaN}',
    '{"value":Infinity}',
    '{"value":"wrong"}', '{"value":1,"extra":2}',
])
def test_invalid_arguments_return_tool_error_without_execution(agent, client, arguments):
    tool = Mock(name="record", requires_confirmation=True)
    tool.name = "record"
    tool.description = "Record a value"
    tool.parameters = {"type": "object", "properties": {"value": {"type": "integer"}},
                       "required": ["value"], "additionalProperties": False}
    agent.tools.register(tool)
    with patch.object(client, "_post_json", side_effect=[
        completion("tool_calls", tool_calls=[tool_call(arguments)]), completion(),
    ]) as post:
        assert agent.run("work").text == "done"
    tool.run.assert_not_called()
    messages = post.call_args_list[1].args[1]["messages"]
    assert messages[-2]["tool_calls"][0]["function"]["arguments"] == arguments
    assert "ERROR:" in messages[-1]["content"]


def test_legacy_function_call_is_adapted(client):
    with patch.object(client, "_post_json", return_value=completion(
        "function_call", content=None, function_call={"name": "record", "arguments": "{}"},
    )):
        result = client.complete("test", [], ToolRegistry())
    assert result["finish_reason"] == "function_call"
    assert result["tool_calls"][0]["name"] == "record"
    assert result["tool_calls"][0]["id"]


def test_custom_client_requires_explicit_finish_reason(agent):
    agent.llm = Mock()
    agent.llm.complete.return_value = {"content": "done", "tool_calls": []}
    with pytest.raises(LLMResponseError, match="finish_reason"):
        agent.run("work")


def test_chat_reports_failure_without_recording_success(agent, client):
    session = ChatSession(agent)
    with patch.object(client, "_post_json", return_value=completion("length")):
        assert "finish_reason=length" in session.send("work")
    assert session.history == []


def test_a2a_marks_incomplete_completion_as_failed(agent, client):
    service = A2AService({}, lambda depth, stop, grant: agent, grace_seconds=2)
    with patch.object(client, "_post_json", return_value=completion("length")):
        task = service.message_send({"message": new_message("user", "work")})
    assert task["status"]["state"] == "failed"
    assert not task.get("artifacts")


def test_reflection_does_not_learn_from_incomplete_json():
    llm = Mock()
    llm.complete.return_value = {
        "content": '{"global_profile":"partial"}', "finish_reason": "length",
    }
    reflector = Reflector(llm, "test")
    assert reflector.reflect([Message("user", "hello")], "", "") is None
