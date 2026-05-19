from __future__ import annotations

from collections.abc import Callable
from typing import Any

from little_agent.config import AgentConfig
from little_agent.llm import LLMClient, LocalRuleClient, OpenAICompatibleChatClient
from little_agent.memory import ConversationMemory
from little_agent.skills.loader import SkillLoader
from little_agent.tools import default_tools
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
    ) -> None:
        self.config = config
        self.skills = skills
        self.tools = tools or default_tools()
        for tool in self.skills.load_tools():
            if tool.name not in self.tools.names():
                self.tools.register(tool)
        self.llm = llm or self._default_llm(config)
        self.confirm = confirm or (lambda _name, _args: True)
        self.memory = ConversationMemory()

    def run(self, user_text: str) -> str:
        selected_skills = self.skills.select_for_text(user_text)
        system_prompt = self._system_prompt(selected_skills)
        messages = [*self.memory.messages]
        if not messages or messages[0].role != "system":
            messages.insert(0, system_prompt)
        else:
            messages[0] = system_prompt
        messages.append(type(system_prompt)(role="user", content=user_text))

        try:
            response = self.llm.complete(self.config.model, messages, self.tools)
        except RuntimeError as exc:
            return f"LLM request failed: {exc}"
        tool_outputs = []
        for call in response.get("tool_calls", []):
            output = self._run_tool(call["name"], call.get("arguments", {}))
            tool_outputs.append(f"[{call['name']}]\n{output}")

        final = response.get("content", "").strip()
        if tool_outputs:
            final = "\n\n".join([part for part in [final, *tool_outputs] if part])

        self.memory.add("user", user_text)
        self.memory.add("assistant", final)
        return final

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self.tools.names():
            return f"Unknown tool: {name}"
        tool = self.tools.get(name)
        if self.config.require_confirmation and tool.requires_confirmation:
            if not self.confirm(name, arguments):
                return "Cancelled by user."
        context = ToolContext(
            workspace=self.config.workspace,
            require_confirmation=self.config.require_confirmation,
        )
        try:
            result = tool.run(context, **arguments)
        except Exception as exc:  # noqa: BLE001 - surfaced to the CLI as tool failure text.
            return f"Tool failed: {exc}"
        prefix = "OK" if result.ok else "ERROR"
        return f"{prefix}: {result.content}"

    def _system_prompt(self, selected_skills: list[Any]):
        skill_text = "\n\n".join(skill.as_prompt() for skill in selected_skills) or "(no matching skills)"
        content = (
            "You are Little Agent, a Windows-friendly Python agent system. "
            "Use tools when they help. Keep actions inside the configured workspace. "
            "Prefer concise, practical answers.\n\n"
            f"Workspace: {self.config.workspace}\n\n"
            f"Available tools:\n{self.tools.descriptions()}\n\n"
            f"Relevant skills:\n{skill_text}"
        )
        from little_agent.messages import Message

        return Message(role="system", content=content)

    @staticmethod
    def _default_llm(config: AgentConfig) -> LLMClient:
        if config.openai_api_key:
            return OpenAICompatibleChatClient(
                api_key=config.openai_api_key,
                base_url=config.openai_base_url,
            )
        return LocalRuleClient()
