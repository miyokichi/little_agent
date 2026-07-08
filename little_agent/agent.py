from __future__ import annotations

from collections.abc import Callable
from typing import Any

from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.llm import LLMClient, LocalRuleClient, OpenAICompatibleChatClient
from little_agent.logging import RunLogger, estimate_tokens
from little_agent.memory import ConversationMemory, MasterMemory
from little_agent.messages import Message, text_content
from little_agent.reflection import Reflector
from little_agent.skills.loader import SkillLoader
from little_agent.tools import UpdateGlobalMemoryTool, UpdateWorkspaceMemoryTool, default_tools
from little_agent.tools.base import ToolContext, ToolRegistry


ConfirmCallback = Callable[[str, dict[str, Any]], bool]


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        skills: SkillLoader,
        tools: ToolRegistry | None = None,
        llm: LLMClient | None = None,
        confirm: ConfirmCallback | None = None,
        stop: StopController | None = None,
    ) -> None:
        self.config = config
        self.skills = skills
        self.tools = tools or default_tools()
        for tool in self.skills.load_tools():
            if tool.name not in self.tools.names():
                self.tools.register(tool)
        self.llm = llm or self._default_llm(config)
        self.confirm = confirm or (lambda _name, _args: True)
        self.stop = stop or StopController(config.stop_hotkey)
        # Once the user approves a confirmation, the rest of the session runs
        # without further prompts (session-wide approval).
        self._session_approved = False
        self.memory = ConversationMemory()
        self.workspace_memory = MasterMemory(config.workspace / "memory.md")
        self.global_memory = MasterMemory(config.global_memory_path)
        self.tools.register(UpdateWorkspaceMemoryTool(self.workspace_memory))
        self.tools.register(UpdateGlobalMemoryTool(self.global_memory))
        # Auto-learning: durable profiles distilled from the session (separate
        # from the manually-edited memory.md above).
        self.workspace_profile = MasterMemory(config.workspace / "profile.md")
        self.global_profile = MasterMemory(config.global_profile_path)
        self._reflected_upto = 0
        self.logger = RunLogger(config.log_dir or (config.workspace / "logs")) if config.enable_logging else None

    def end_session(self) -> bool:
        """Distill durable profiles from the session. Call once when the session ends."""
        return self._reflect()

    def remember(self) -> bool:
        """Manually trigger auto-learning mid-session (e.g. the /remember command)."""
        return self._reflect()

    def _reflect(self) -> bool:
        if not self.config.enable_auto_learning:
            return False
        # LocalRuleClient cannot summarize; skip auto-learning without a real LLM.
        if isinstance(self.llm, LocalRuleClient):
            return False
        if len(self.memory.messages) <= self._reflected_upto:
            return False  # nothing new since the last reflection
        reflector = Reflector(self.llm, self.config.model)
        result = reflector.reflect(
            self.memory.messages,
            self.global_profile.load(),
            self.workspace_profile.load(),
        )
        # Mark progress even on no-op so we don't retry the same transcript.
        self._reflected_upto = len(self.memory.messages)
        if result is None:
            return False
        new_global, new_workspace = result
        # An empty string means "leave the existing profile untouched".
        if new_global:
            self.global_profile.save(new_global)
        if new_workspace:
            self.workspace_profile.save(new_workspace)
        if self.logger:
            self.logger.log_conversation(
                "auto_learning",
                global_updated=bool(new_global),
                workspace_updated=bool(new_workspace),
            )
        return bool(new_global or new_workspace)

    def run(self, user_text: str) -> str:
        if self.logger:
            self.logger.log_conversation("user_message", content=user_text)
        selected_skills = self.skills.select_for_text(user_text)
        system_prompt = self._system_prompt(selected_skills)
        messages = [*self.memory.messages]
        if not messages or messages[0].role != "system":
            messages.insert(0, system_prompt)
        else:
            messages[0] = system_prompt
        messages.append(type(system_prompt)(role="user", content=user_text))

        final = ""
        self.stop.reset()
        self.stop.arm()
        try:
            for _step in range(max(self.config.max_tool_steps, 1)):
                if self.stop.triggered:
                    final = self._stop_message()
                    break
                try:
                    response = self.llm.complete(self.config.model, messages, self.tools)
                except RuntimeError as exc:
                    if self.logger:
                        self.logger.log_conversation("llm_error", error=str(exc))
                    return f"LLM request failed: {exc}"

                tool_calls = self._normalize_tool_calls(response.get("tool_calls", []))
                content = response.get("content", "").strip()
                if self.logger:
                    self.logger.log_conversation(
                        "assistant_message",
                        content=content,
                        tool_calls=tool_calls,
                        step=_step + 1,
                    )
                    self.logger.log_usage(
                        "llm_usage",
                        self._usage(response, messages, content, tool_calls),
                        model=self.config.model,
                        step=_step + 1,
                    )
                if not tool_calls:
                    final = content
                    break

                messages.append(Message(role="assistant", content=content, tool_calls=tool_calls))
                pending_images: list[tuple[str, str]] = []
                stopped = False
                for call in tool_calls:
                    if stopped or self.stop.triggered:
                        # Fill remaining tool calls with a stop result so the
                        # conversation stays well-formed for the next turn.
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
                    output, images = self._run_tool(call["name"], call.get("arguments", {}))
                    messages.append(
                        Message(
                            role="tool",
                            content=f"[{call['name']}]\n{output}",
                            tool_call_id=call["id"],
                        )
                    )
                    pending_images.extend((call["name"], uri) for uri in images)
                if stopped:
                    final = self._stop_message()
                    break
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

        if not final:
            final = "(no response)"

        self.memory.add("user", user_text)
        self.memory.add("assistant", final)
        if self.logger:
            self.logger.log_conversation("final_answer", content=final)
        return final

    @staticmethod
    def _normalize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for index, call in enumerate(tool_calls, start=1):
            arguments = call.get("arguments") or {}
            normalized.append(
                {
                    "id": str(call.get("id") or f"call_{index}"),
                    "name": str(call.get("name") or ""),
                    "arguments": arguments if isinstance(arguments, dict) else {},
                }
            )
        return [call for call in normalized if call["name"]]

    def _stop_message(self) -> str:
        return f"Stopped by user (hotkey {self.stop.hotkey})."

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
        if name not in self.tools.names():
            if self.logger:
                self.logger.log_tool("tool_unknown", tool=name, arguments=arguments)
            return f"Unknown tool: {name}", ()
        tool = self.tools.get(name)
        if self.config.require_confirmation and tool.requires_confirmation and not self._session_approved:
            if not self.confirm(name, arguments):
                if self.logger:
                    self.logger.log_tool("tool_cancelled", tool=name, arguments=arguments)
                return "Cancelled by user.", ()
            # Session-wide approval: don't prompt again for the rest of the session.
            self._session_approved = True
        context = ToolContext(
            workspace=self.config.workspace,
            require_confirmation=self.config.require_confirmation,
        )
        try:
            result = tool.run(context, **arguments)
        except Exception as exc:  # noqa: BLE001 - surfaced to the CLI as tool failure text.
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

    def _system_prompt(self, selected_skills: list[Any]):
        skill_text = "\n\n".join(skill.as_prompt() for skill in selected_skills) or "(no matching skills)"

        global_mem = self.global_memory.load()
        workspace_mem = self.workspace_memory.load()
        global_prof = self.global_profile.load()
        workspace_prof = self.workspace_profile.load()
        memory_section = ""
        if global_mem:
            memory_section += f"\n\n## Global Memory\n{global_mem}"
        if workspace_mem:
            memory_section += f"\n\n## Workspace Memory\n{workspace_mem}"
        if global_prof:
            memory_section += f"\n\n## User Profile (learned)\n{global_prof}"
        if workspace_prof:
            memory_section += f"\n\n## Workspace Profile (learned)\n{workspace_prof}"

        content = (
            "You are Little Agent, a Windows-friendly Python agent system. "
            "Use tools when they help. Keep actions inside the configured workspace. "
            "Prefer concise, practical answers.\n\n"
            f"Workspace: {self.config.workspace}\n\n"
            f"Available tools:\n{self.tools.descriptions()}\n\n"
            f"Relevant skills:\n{skill_text}"
            f"{memory_section}"
        )
        return Message(role="system", content=content)

    @staticmethod
    def _default_llm(config: AgentConfig) -> LLMClient:
        if config.openai_api_key:
            return OpenAICompatibleChatClient(
                api_key=config.openai_api_key,
                base_url=config.openai_base_url,
                timeout=config.llm_timeout_seconds,
            )
        return LocalRuleClient()
