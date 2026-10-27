"""The agent runtime: one execution of the tool loop, start to finish.

This is the single runtime both front ends share::

                 Agent Runtime
                 /           \
              Chat            A2A

``Agent.run`` is one independent execution. It builds its messages from the
instruction, the caller-supplied context, any conversation ``history`` handed in
for this run, the profile's skills and tools, runs the multi-step tool loop and
returns the result. The agent itself keeps nothing between runs — whoever wants
continuity owns it and passes it back in (that is
:class:`~little_agent.session.ChatSession` for chat; an A2A task passes nothing,
so tasks never share context).

Persistence is likewise not the runtime's business: it holds a
:class:`~little_agent.memory.store.MemoryStore` and asks it for prompt sections
and tools. With the null store — ``chat --no-memory`` and every A2A task — that
is nothing at all, and no file is read or written.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from little_agent import schema as json_schema
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.llm import (
    LLMClient,
    LLMResponseError,
    LocalRuleClient,
    OpenAICompatibleChatClient,
    validate_completion,
)
from little_agent.logging import RunLogger, estimate_tokens
from little_agent.memory.store import MemoryStore, NullMemoryStore
from little_agent.messages import Message, text_content
from little_agent.skills.activation import ActivateSkillTool
from little_agent.skills.loader import SkillLoader
from little_agent.tools import default_tools
from little_agent.tools.base import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from little_agent.mcp.manager import McpView


ConfirmCallback = Callable[[str, dict[str, Any]], bool]


class StructuredOutputError(RuntimeError):
    """The final answer did not parse as JSON, or did not match ``output_schema``."""


@dataclass(slots=True)
class RunResult:
    """The outcome of one execution.

    ``text`` is always set (it is what a human or a text-only caller reads).
    ``data`` holds the validated JSON value when the caller asked for structured
    output with an ``output_schema``, and is ``None`` otherwise.
    """

    text: str
    data: Any | None = None

    def __str__(self) -> str:
        return self.text


@dataclass(slots=True)
class _Execution:
    """Per-run state. Created in ``run`` and discarded when it returns."""

    messages: list[Message] = field(default_factory=list)
    approved: bool = False


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        skills: SkillLoader,
        tools: ToolRegistry | None = None,
        llm: LLMClient | None = None,
        confirm: ConfirmCallback | None = None,
        stop: StopController | None = None,
        core_tools: set[str] | None = None,
        memory: MemoryStore | None = None,
        mcp: "McpView | None" = None,
    ) -> None:
        self.config = config
        self.skills = skills
        # No store means "remember nothing" — never a silent fallback to disk.
        self.memory: MemoryStore = memory or NullMemoryStore()
        # ``core_tools`` is a per-agent allowlist for the built-in core tools.
        self.tools = tools or default_tools(core_tools)
        # Skills are disclosed progressively and chosen by the model: a catalog
        # in the system prompt, activate_skill to load one. A skill's own script
        # tools (tools.json) are registered only once the model has activated
        # that skill, and dropped again when the next run starts.
        self._activation = ActivateSkillTool(self.skills, on_activate=self._load_skill_tools)
        self._skill_dirs: tuple[Path, ...] = ()
        self._skill_tool_names: list[str] = []
        # tool name -> skill name, for every skill tool that *could* be loaded.
        self.skill_tool_index: dict[str, str] = {}
        self._sync_skills()
        # Memory tools exist only when the store persists something.
        for tool in self.memory.tools():
            self.tools.register(tool)
        # MCP servers selected by the profile: their tools, plus the generic
        # resource tools when any of them offers resources.
        self.mcp = mcp
        self._mcp_tool_names: list[str] = []
        self._register_mcp_tools()
        self.llm = llm or self._default_llm(config)
        self.confirm = confirm or (lambda _name, _args: True)
        self.stop = stop or StopController(config.stop_hotkey)
        self.logger = RunLogger(config.log_dir or (config.workspace / "logs")) if config.enable_logging else None

    def run(
        self,
        instruction: str,
        context: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        history: Sequence[Message] | None = None,
        activate_skills: Sequence[str] = (),
    ) -> RunResult:
        """Execute one task and return its result.

        Which skills to use is normally the model's choice (``activate_skill``).
        ``activate_skills`` names skills the *user* chose explicitly (chat's
        ``/<skill-name>``): they are activated before the model starts — their
        instructions go in front of the instruction and their tools are loaded.

        ``context`` is situational data supplied by the caller for this execution
        only; it is rendered into the prompt and never persisted. When
        ``output_schema`` is given the final answer must be JSON matching it, and
        ``StructuredOutputError`` is raised when it is not.

        ``history`` is earlier conversation the caller wants this run to see. The
        agent copies it, never mutates it, and keeps nothing afterwards: a caller
        that wants continuity (chat) owns the transcript and hands it back each
        turn, and a caller that wants isolation (an A2A task) simply passes
        nothing.
        """

        if self.mcp is not None and self.mcp.refresh():
            self._register_mcp_tools()
        prompt = self._compose_prompt(instruction, context)
        if self.logger:
            self.logger.log_conversation("user_message", content=prompt)

        execution = _Execution()
        skills = self._sync_skills()
        preactivated = self._preactivate(activate_skills)
        if preactivated:
            prompt = "\n\n".join([*preactivated, prompt])
        execution.messages.append(
            self._system_prompt(skills, output_schema, conversational=bool(history))
        )
        # A system message inside the caller's history would fight the one above.
        execution.messages.extend(
            message for message in (history or ()) if message.role != "system"
        )
        execution.messages.append(Message(role="user", content=prompt))

        try:
            final = self._loop(execution)
        finally:
            # The choice of skills belongs to this run only.
            self._unload_skill_tools()
        if self.logger:
            self.logger.log_conversation("final_answer", content=final)
        if output_schema is None:
            return RunResult(final)
        return self._structured_result(final, output_schema)

    def _loop(self, execution: _Execution) -> str:
        """Run the multi-step tool loop and return the final assistant text."""

        messages = execution.messages
        final = ""
        self.stop.reset()
        self.stop.arm()
        try:
            for step in range(max(self.config.max_tool_steps, 1)):
                if self.stop.triggered:
                    return self._stop_message()
                try:
                    response = self.llm.complete(self.config.model, messages, self.tools)
                    validate_completion(response)
                    tool_calls = self._normalize_tool_calls(response.get("tool_calls") or [])
                except RuntimeError as exc:
                    if self.logger:
                        self.logger.log_conversation("llm_error", error=str(exc))
                    if isinstance(exc, LLMResponseError):
                        raise
                    return f"LLM request failed: {exc}"

                content = (response.get("content") or response.get("refusal") or "").strip()
                finish_reason = response["finish_reason"]
                if self.logger:
                    self.logger.log_conversation(
                        "assistant_message",
                        content=content,
                        tool_calls=tool_calls,
                        finish_reason=finish_reason,
                        refusal=response.get("refusal"),
                        step=step + 1,
                    )
                    self.logger.log_usage(
                        "llm_usage",
                        self._usage(response, messages, content, tool_calls),
                        model=self.config.model,
                        step=step + 1,
                    )
                if finish_reason == "stop":
                    final = content
                    break

                messages.append(Message(role="assistant", content=content, tool_calls=tool_calls))
                pending_images: list[tuple[str, str]] = []
                stopped = False
                for call in tool_calls:
                    if stopped or self.stop.triggered:
                        # Fill remaining tool calls with a stop result so the
                        # conversation stays well-formed.
                        stopped = True
                        if self.logger:
                            self.logger.log_tool(
                                "tool_stopped", tool=call["name"], arguments=call.get("arguments", {})
                            )
                        messages.append(
                            Message(
                                role="tool",
                                content=f"[{call['name']}]\nStopped by user (hotkey {self.stop.hotkey}).",
                                tool_call_id=call["id"],
                            )
                        )
                        continue
                    output, images = self._run_tool(execution, call["name"], call.get("arguments", {}))
                    messages.append(
                        Message(
                            role="tool",
                            content=f"[{call['name']}]\n{output}",
                            tool_call_id=call["id"],
                        )
                    )
                    pending_images.extend((call["name"], uri) for uri in images)
                if stopped:
                    return self._stop_message()
                # Tool messages cannot carry images in the OpenAI chat format, so image
                # outputs are forwarded in a follow-up user message after all tool replies.
                if pending_images:
                    blocks: list[dict[str, Any]] = []
                    for name, uri in pending_images:
                        blocks.append({"type": "text", "text": f"Image output from {name}:"})
                        blocks.append({"type": "image_url", "image_url": {"url": uri}})
                    messages.append(Message(role="user", content=blocks))
            else:
                final = f"Stopped after {self.config.max_tool_steps} tool step(s)."
        finally:
            self.stop.disarm()
        return final or "(no response)"

    # --- structured output ---------------------------------------------------

    def _structured_result(self, final: str, output_schema: dict[str, Any]) -> RunResult:
        parsed = _parse_json(final)
        if parsed is _INVALID:
            raise StructuredOutputError(
                "The agent's final answer was not valid JSON, but an output_schema was "
                f"requested. Answer: {final[:500]}"
            )
        errors = json_schema.validate(parsed, output_schema)
        if errors:
            raise StructuredOutputError(
                "The agent's final answer did not match output_schema: " + "; ".join(errors[:10])
            )
        return RunResult(json.dumps(parsed, ensure_ascii=False), data=parsed)

    # --- prompt assembly -----------------------------------------------------

    @staticmethod
    def _compose_prompt(instruction: str, context: dict[str, Any] | None) -> str:
        instruction = (instruction or "").strip()
        if not context:
            return instruction
        rendered = json.dumps(context, ensure_ascii=False, indent=2, default=str)
        return f"{instruction}\n\n## Context (JSON, provided for this task only)\n{rendered}"

    def _system_prompt(
        self,
        skills: list[Any],
        output_schema: dict[str, Any] | None = None,
        conversational: bool = False,
    ) -> Message:
        opening = (
            "You are Little Agent. You are in a conversation: earlier turns are above, "
            "so resolve references against them."
            if conversational
            else "You are Little Agent, a task runner. You are given one task, with "
            "everything you need to do it."
        )
        content = (
            f"{opening} Use tools when they help. Keep actions "
            "inside the configured workspace and explicitly allowed paths. "
            "Prefer concise, practical answers.\n\n"
            f"Workspace: {self.config.workspace}\n\n"
            f"Readable paths: {_paths_text(self.config.readable_paths)}\n"
            f"Writable paths: {_paths_text(self.config.writable_paths)}\n\n"
            f"Available tools:\n{self.tools.descriptions()}"
        )
        # Tier 1 of progressive disclosure: names and descriptions only. The
        # section is left out entirely when there are no skills.
        catalog = self.skills.catalog_prompt(skills)
        if catalog:
            content += (
                f"\n\n## Skills\n{catalog}\n\n"
                "Skill directories are readable (not writable) with the file tools."
            )
        # Empty for the null store, so this whole block vanishes without memory.
        for heading, body in self.memory.sections():
            content += f"\n\n## {heading}\n{body}"
        for heading, body in self.mcp.sections() if self.mcp is not None else ():
            content += f"\n\n## {heading}\n{body}"
        if output_schema is not None:
            content += (
                "\n\n## Required output format\n"
                "Your FINAL message must be a single JSON value that validates against this "
                "JSON Schema. Output the JSON only: no prose, no explanation, no code fences.\n"
                + json.dumps(output_schema, ensure_ascii=False)
            )
        return Message(role="system", content=content)

    # --- skills --------------------------------------------------------------

    def _sync_skills(self) -> list[Any]:
        """Re-read the skills, and keep activate_skill and its enum in step."""

        skills = self.skills.load_all()
        self._skill_dirs = tuple(skill.directory for skill in skills if skill.directory is not None)
        self._activation.begin_run()
        # Each run starts with no skill chosen, so no skill tools loaded.
        self._unload_skill_tools()
        self.skill_tool_index = {
            tool.name: skill.name for skill in skills for tool in self.skills.tools_for(skill)
        }
        if skills:
            self._activation.refresh([skill.name for skill in skills])
            self.tools.register(self._activation)
        else:
            self.tools.unregister(self._activation.name)
        return skills

    def _unload_skill_tools(self) -> None:
        for name in self._skill_tool_names:
            self.tools.unregister(name)
        self._skill_tool_names = []

    def _load_skill_tools(self, skill: Any) -> list[str]:
        """Register an activated skill's script tools; returns the names added.

        A tool whose name is already taken (a core tool, another skill's) is
        left as it is rather than replaced.
        """

        added: list[str] = []
        for tool in self.skills.tools_for(skill):
            if tool.name in self.tools.names():
                continue
            self.tools.register(tool)
            self._skill_tool_names.append(tool.name)
            added.append(tool.name)
        if added and self.logger:
            self.logger.log_tool("skill_tools_loaded", skill=skill.name, tools=added)
        return added

    def _preactivate(self, names: Sequence[str]) -> list[str]:
        """Activate user-chosen skills up front; returns their instruction blocks."""

        blocks: list[str] = []
        for name in names:
            skill = self.skills.get(name)
            if skill is None or skill.name in self._activation.activated:
                continue
            self._activation.activated.add(skill.name)
            added = self._load_skill_tools(skill)
            block = skill.activation_content()
            if added:
                block += f"\n\nTools now available from this skill: {', '.join(added)}"
            blocks.append(block)
        return blocks

    # --- tools ---------------------------------------------------------------

    def _register_mcp_tools(self) -> None:
        for name in self._mcp_tool_names:
            self.tools.unregister(name)
        self._mcp_tool_names = []
        if self.mcp is None:
            return
        for tool in self.mcp.tools():
            if tool.name not in self.tools.names():
                self.tools.register(tool)
                self._mcp_tool_names.append(tool.name)

    def _run_tool(
        self, execution: _Execution, name: str, arguments: dict[str, Any] | str
    ) -> tuple[str, tuple[str, ...]]:
        if name not in self.tools.names():
            if self.logger:
                self.logger.log_tool("tool_unknown", tool=name, arguments=arguments)
            owner = self.skill_tool_index.get(name)
            if owner is not None:
                return (
                    f"ERROR: Tool '{name}' belongs to the skill '{owner}', which is not active. "
                    f"Call activate_skill with name '{owner}' first if that skill fits the task."
                ), ()
            return f"Unknown tool: {name}", ()
        tool = self.tools.get(name)
        # Preserve raw JSON in the transcript; report invalid arguments so the
        # model can correct them instead of executing a repaired/default call.
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments, parse_constant=_reject_json_constant)
            except ValueError:
                return "ERROR: Tool arguments must be valid JSON.", ()
        if not isinstance(arguments, dict):
            return "ERROR: Tool arguments must be a JSON object.", ()
        errors = json_schema.validate(arguments, tool.parameters)
        if errors:
            return "ERROR: Invalid tool arguments: " + "; ".join(errors[:10]), ()
        if self.config.require_confirmation and tool.requires_confirmation and not execution.approved:
            if not self.confirm(name, arguments):
                if self.logger:
                    self.logger.log_tool("tool_cancelled", tool=name, arguments=arguments)
                return "Cancelled by user.", ()
            # Approval covers the rest of this execution only.
            execution.approved = True
        context = ToolContext(
            workspace=self.config.workspace,
            # Skill directories are readable so bundled references, scripts and
            # assets can be loaded on demand; they are never writable.
            readable_paths=(*self.config.readable_paths, *self._skill_dirs),
            writable_paths=self.config.writable_paths,
            require_confirmation=self.config.require_confirmation,
        )
        try:
            result = tool.run(context, **arguments)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as tool failure text.
            if self.logger:
                self.logger.log_tool("tool_error", tool=name, arguments=arguments, error=str(exc))
            return f"Tool failed: {exc}", ()
        prefix = "OK" if result.ok else "ERROR"
        output = f"{prefix}: {result.content}"
        if self.logger:
            self.logger.log_tool(
                "tool_result",
                tool=name,
                arguments=arguments,
                ok=result.ok,
                content=result.content,
                images=len(result.images),
            )
        return output, result.images

    @staticmethod
    def _normalize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(tool_calls, list):
            raise LLMResponseError("LLM returned invalid tool_calls.")
        normalized = []
        for index, call in enumerate(tool_calls, start=1):
            if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not call["name"]:
                raise LLMResponseError("LLM returned a tool call without a valid name.")
            arguments = call.get("arguments", {})
            normalized.append(
                {
                    "id": str(call.get("id") or f"call_{index}"),
                    "name": str(call.get("name") or ""),
                    "arguments": arguments,
                }
            )
        if len({call["id"] for call in normalized}) != len(normalized):
            raise LLMResponseError("LLM returned duplicate tool call IDs.")
        return normalized

    def _stop_message(self) -> str:
        return f"Stopped by user (hotkey {self.stop.hotkey})."

    @staticmethod
    def _usage(
        response: dict[str, Any],
        messages: list[Message],
        content: str,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, object]:
        usage = response.get("usage") or {}
        if isinstance(usage, dict) and usage:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated": False,
            }

        prompt_text = "\n".join(text_content(message.content) for message in messages)
        completion_text = content + "\n" + "\n".join(call["name"] for call in tool_calls)
        prompt_tokens = estimate_tokens(prompt_text)
        completion_tokens = estimate_tokens(completion_text)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated": True,
        }

    @staticmethod
    def _default_llm(config: AgentConfig) -> LLMClient:
        if config.openai_api_key:
            return OpenAICompatibleChatClient(
                api_key=config.openai_api_key,
                base_url=config.openai_base_url,
                timeout=config.llm_timeout_seconds,
            )
        return LocalRuleClient()


# Sentinel distinguishing "parsed to JSON null" from "did not parse".
_INVALID = object()


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Invalid JSON constant: {value}")


def _paths_text(paths: tuple[Any, ...]) -> str:
    return ", ".join(str(path) for path in paths) if paths else "(none)"


def _parse_json(raw: str) -> Any:
    """Strict JSON parse of a final answer, tolerating a fenced code block.

    Deliberately strict otherwise: a malformed answer must be rejected rather
    than repaired, so a caller asking for structured output never receives a
    guess about what the model meant.
    """

    text = (raw or "").strip()
    if text.startswith("```"):
        body = text[3:]
        if body.lower().startswith("json"):
            body = body[4:]
        end = body.rfind("```")
        text = (body[:end] if end != -1 else body).strip()
    if not text:
        return _INVALID
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _INVALID
