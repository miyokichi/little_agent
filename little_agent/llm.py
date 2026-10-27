from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4

from little_agent.messages import Message, text_content
from little_agent.tools.base import ToolRegistry


class LLMResponseError(RuntimeError):
    """An incomplete or invalid completion must not become a successful task."""


class LLMClient:
    """Return content, tool_calls, finish_reason, and optional usage/refusal.

    ``finish_reason`` is required, including for local or custom clients.
    Tool arguments may be dictionaries or raw JSON strings.
    """

    def complete(
        self,
        model: str,
        messages: list[Message],
        tools: ToolRegistry,
    ) -> dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleChatClient(LLMClient):
    """Call an OpenAI-compatible Chat Completions API without the OpenAI SDK."""

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", timeout: int = 60) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(
        self,
        model: str,
        messages: list[Message],
        tools: ToolRegistry,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [self._message_payload(message) for message in messages],
        }
        schemas = tools.openai_schemas()
        if schemas:
            body.update(tools=schemas, tool_choice="auto")
        response = self._post_json("/chat/completions", body)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMResponseError("Chat completion API returned no valid choices.")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise LLMResponseError("Chat completion API returned no valid assistant message.")
        content = message.get("content")
        refusal = message.get("refusal")
        if content is not None and not isinstance(content, str):
            raise LLMResponseError("Chat completion API returned non-text assistant content.")
        if refusal is not None and not isinstance(refusal, str):
            raise LLMResponseError("Chat completion API returned an invalid refusal.")
        result = {
            "content": content or "",
            "refusal": refusal,
            "finish_reason": choice.get("finish_reason"),
            "tool_calls": self._tool_calls(message)
            if choice.get("finish_reason") in ("stop", "tool_calls", "function_call") else [],
            "usage": response.get("usage") or {},
        }
        validate_completion(result)
        return result

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Chat completion API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach chat completion API: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"Chat completion API timed out after {self.timeout} seconds.") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"Chat completion API returned invalid JSON: {raw[:500]}") from exc
        if not isinstance(parsed, dict):
            raise LLMResponseError("Chat completion API returned a non-object JSON response.")
        return parsed

    @staticmethod
    def _message_payload(message: Message) -> dict[str, Any]:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.name:
            item["name"] = message.name
        if message.role == "tool" and message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [_openai_tool_call(call) for call in message.tool_calls]
        return item

    @staticmethod
    def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise LLMResponseError("Chat completion API returned invalid tool_calls.")
        calls = []
        for call in raw_calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise LLMResponseError("Chat completion API returned an unsupported tool call type.")
            function = call.get("function")
            if (
                not isinstance(call.get("id"), str) or not call["id"]
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str) or not function["name"]
                or not isinstance(function.get("arguments"), str)
            ):
                raise LLMResponseError("Chat completion API returned a malformed function tool call.")
            calls.append({"id": call["id"], "name": function["name"], "arguments": function["arguments"]})
        if len({call["id"] for call in calls}) != len(calls):
            raise LLMResponseError("Chat completion API returned duplicate tool call IDs.")

        # Deprecated function_call responses are adapted to the modern tool loop.
        legacy_call = message.get("function_call")
        if legacy_call is not None:
            if (
                calls or not isinstance(legacy_call, dict)
                or not isinstance(legacy_call.get("name"), str) or not legacy_call["name"]
                or not isinstance(legacy_call.get("arguments"), str)
            ):
                raise LLMResponseError("Chat completion API returned a malformed legacy function_call.")
            calls.append({
                "id": f"call_{uuid4().hex[:12]}",
                "name": legacy_call["name"],
                "arguments": legacy_call["arguments"],
            })
        return calls


class LocalRuleClient(LLMClient):
    """A tiny no-API fallback so the CLI can be exercised immediately."""

    def complete(
        self,
        model: str,
        messages: list[Message],
        tools: ToolRegistry,
    ) -> dict[str, Any]:
        tool_messages = [message for message in messages if message.role == "tool"]
        text = next(
            (text_content(message.content) for message in reversed(messages) if message.role == "user"),
            "",
        )
        lowered = text.lower()
        wants_time = "time" in lowered or "date" in lowered
        last = text_content(tool_messages[-1].content) if tool_messages else ""
        # Like a real model: a skill's tools appear only after activating it.
        if wants_time and last.startswith("[activate_skill]") and "get_datetime" in tools.names():
            return {
                "content": "", "finish_reason": "tool_calls", "usage": {},
                "tool_calls": [{"name": "get_datetime", "arguments": {}}],
            }
        if tool_messages:
            return {
                "content": last, "tool_calls": [],
                "finish_reason": "stop", "usage": {},
            }

        if wants_time:
            if "get_datetime" in tools.names():
                call = {"name": "get_datetime", "arguments": {}}
            elif "activate_skill" in tools.names():
                call = {"name": "activate_skill", "arguments": {"name": "datetime"}}
            else:
                call = {"name": "get_datetime", "arguments": {}}
            return {
                "content": "", "finish_reason": "tool_calls", "usage": {},
                "tool_calls": [call],
            }
        if "list" in lowered or "ls" in lowered:
            return {
                "content": "", "finish_reason": "tool_calls", "usage": {},
                "tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}],
            }
        return {
            "content": (
                "OPENAI_API_KEY is not configured, so I am running in local fallback mode. "
                "I can still handle simple requests like date/time or listing the workspace."
            ),
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": {},
        }


def validate_completion(response: dict[str, Any]) -> None:
    """Require an explicit, consistent terminal reason for a non-streamed turn."""
    reason = response.get("finish_reason")
    if reason == "length":
        raise LLMResponseError(
            "Chat completion stopped with finish_reason=length (token limit); the response is incomplete."
        )
    if reason == "content_filter":
        raise LLMResponseError(
            "Chat completion stopped with finish_reason=content_filter; content was filtered."
        )
    if reason not in ("stop", "tool_calls", "function_call"):
        raise LLMResponseError(f"Chat completion returned missing or unsupported finish_reason: {reason!r}.")
    calls = response.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise LLMResponseError("Chat completion returned invalid tool_calls.")
    if reason == "stop" and calls:
        raise LLMResponseError("Chat completion returned finish_reason=stop with tool calls.")
    if reason in ("tool_calls", "function_call") and not calls:
        raise LLMResponseError(f"Chat completion returned finish_reason={reason} without tool calls.")
    if response.get("refusal") and calls:
        raise LLMResponseError("Chat completion returned a refusal with tool calls.")


def _openai_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    arguments = call.get("arguments", {})
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": call.get("id") or f"call_{uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": str(call.get("name") or ""),
            "arguments": arguments,
        },
    }
