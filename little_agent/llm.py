from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from uuid import uuid4
from typing import Any

try:
    from json_repair import repair_json as _repair_json
    _HAS_JSON_REPAIR = True
except ImportError:
    _HAS_JSON_REPAIR = False

from little_agent.messages import Message, text_content
from little_agent.tools.base import ToolRegistry


class LLMClient:
    def complete(
        self,
        model: str,
        messages: list[Message],
        tools: ToolRegistry,
    ) -> dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleChatClient(LLMClient):
    """Call an OpenAI-compatible Chat Completions API without the OpenAI SDK."""

    api_label = "Chat completion API"

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
            "tools": tools.openai_schemas(),
            "tool_choice": "auto",
        }
        response = self._post_json("/chat/completions", body)
        choice = response["choices"][0]["message"]
        return {
            "content": choice.get("content") or "",
            "tool_calls": self._tool_calls(choice),
            "usage": response.get("usage") or {},
        }

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
            raise RuntimeError(f"{self.api_label} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach {self.api_label}: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"{self.api_label} timed out after {self.timeout} seconds.") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.api_label} returned invalid JSON: {raw[:500]}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{self.api_label} returned a non-object JSON response.")
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
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append(
                {
                    "id": call.get("id") or f"call_{uuid4().hex[:12]}",
                    "name": function.get("name", ""),
                    "arguments": _json_object(function.get("arguments") or "{}"),
                }
            )

        legacy_call = message.get("function_call")
        if legacy_call:
            calls.append(
                {
                    "id": f"call_{uuid4().hex[:12]}",
                    "name": legacy_call.get("name", ""),
                    "arguments": _json_object(legacy_call.get("arguments") or "{}"),
                }
            )
        return [call for call in calls if call["name"]]


class OpenAICompatibleResponsesClient(OpenAICompatibleChatClient):
    """Stateless Responses adapter for the same local tool loop as chat.

    Replay output items with each tool result rather than keeping a server-side
    response ID. Client instances therefore hold no mutable conversation state.
    """

    api_label = "Responses API"

    def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict[str, Any]:
        inputs: list[dict[str, Any]] = []
        for message in messages:
            inputs.extend(self._input_items(message))
        body: dict[str, Any] = {
            "model": model,
            "input": inputs,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        schemas = tools.openai_schemas()
        if schemas:
            # Responses functions are flat; preserve optional arguments instead
            # of allowing the API to normalize them into required properties.
            body["tools"] = [
                {"type": "function", **schema["function"], "strict": False}
                for schema in schemas
            ]
            body["tool_choice"] = "auto"
        response = self._post_json("/responses", body)
        status = response.get("status")
        if response.get("error") or status in {"failed", "cancelled", "incomplete"}:
            detail = response.get("error") or response.get("incomplete_details") or status
            raise RuntimeError(f"Responses API did not complete: {detail}")
        if status not in {None, "completed"}:
            raise RuntimeError(f"Unexpected Responses API status: {status}")
        output = response.get("output")
        if not isinstance(output, list):
            raise RuntimeError("Responses API returned no output array.")
        texts = []
        calls = []
        for item in output:
            if not isinstance(item, dict):
                raise RuntimeError("Responses API returned an invalid output item.")
            if item.get("type") == "message":
                for block in item.get("content") or []:
                    if block.get("type") == "output_text":
                        texts.append(block.get("text") or "")
                    elif block.get("type") == "refusal":
                        texts.append(block.get("refusal") or "")
            elif item.get("type") == "function_call":
                if item.get("status") not in {None, "completed"}:
                    raise RuntimeError("Responses API returned an unfinished function call.")
                if not item.get("call_id") or not item.get("name"):
                    raise RuntimeError("Responses API returned a function call without call_id or name.")
                arguments = item.get("arguments") or "{}"
                # Never repair truncated tool arguments into an executable call.
                try:
                    arguments = json.loads(arguments)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Responses API returned invalid function arguments.") from exc
                if not isinstance(arguments, dict):
                    raise RuntimeError("Responses API function arguments must be a JSON object.")
                calls.append({"id": item["call_id"], "name": item["name"], "arguments": arguments})
        if not any(texts) and not calls:
            raise RuntimeError("Responses API returned neither an answer nor a function call.")
        usage = response.get("usage") or {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        return {
            "content": "\n".join(texts),
            "tool_calls": calls,
            "responses_output": output,
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": usage.get("total_tokens", input_tokens + output_tokens),
            } if usage else {},
        }

    @staticmethod
    def _input_items(message: Message) -> list[dict[str, Any]]:
        if message.role == "assistant" and message.responses_output is not None:
            return message.responses_output
        if message.role == "tool":
            if not message.tool_call_id:
                raise RuntimeError("Responses tool result requires a tool_call_id.")
            return [{"type": "function_call_output", "call_id": message.tool_call_id,
                     "output": text_content(message.content)}]
        items: list[dict[str, Any]] = []
        content = message.content
        if isinstance(content, list):
            blocks = []
            for block in content:
                if block.get("type") == "text":
                    blocks.append({"type": "input_text", "text": block.get("text", "")})
                elif block.get("type") == "image_url":
                    image = block["image_url"]
                    converted = {"type": "input_image", "image_url": image["url"]}
                    if "detail" in image:
                        converted["detail"] = image["detail"]
                    blocks.append(converted)
                else:
                    raise RuntimeError(f"Unsupported Responses input block: {block.get('type')}")
            content = blocks
        if content or not message.tool_calls:
            items.append({"role": message.role, "content": content})
        for call in message.tool_calls or []:
            converted_call = _openai_tool_call(call)
            items.append({"type": "function_call", "call_id": converted_call["id"],
                          "name": converted_call["function"]["name"],
                          "arguments": converted_call["function"]["arguments"]})
        return items


class LocalRuleClient(LLMClient):
    """A tiny no-API fallback so the CLI can be exercised immediately."""

    def complete(
        self,
        model: str,
        messages: list[Message],
        tools: ToolRegistry,
    ) -> dict[str, Any]:
        tool_messages = [message for message in messages if message.role == "tool"]
        if tool_messages:
            return {"content": text_content(tool_messages[-1].content), "tool_calls": [], "usage": {}}

        text = next(
            (text_content(message.content) for message in reversed(messages) if message.role == "user"),
            "",
        )
        lowered = text.lower()
        if "time" in lowered or "date" in lowered:
            return {"content": "", "tool_calls": [{"name": "get_datetime", "arguments": {}}], "usage": {}}
        if "list" in lowered or "ls" in lowered:
            return {"content": "", "tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}], "usage": {}}
        return {
            "content": (
                "OPENAI_API_KEY is not configured, so I am running in local fallback mode. "
                "I can still handle simple requests like date/time or listing the workspace."
            ),
            "tool_calls": [],
            "usage": {},
        }


def _json_object(raw: str) -> dict[str, Any]:
    if _HAS_JSON_REPAIR:
        parsed = _repair_json(raw, return_objects=True)
        return parsed if isinstance(parsed, dict) else {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _openai_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    arguments = call.get("arguments") or {}
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
