"""Delegate a subtask to a freshly launched, autonomous sub-agent.

The parent agent calls ``delegate_task`` with a self-contained instruction; the
tool spawns a brand-new agent (optionally a specific ``agents/`` profile), runs
it to completion with its own clean context, and returns the sub-agent's final
answer. This is in-process delegation — no agent-to-agent protocol — which keeps
long or specialized subtasks out of the parent's context window and lets focused
agent profiles handle work they are configured for.

The tool is wired up by the CLI's ``build_agent`` (which owns the sub-agent
factory and the recursion depth), so constructing an ``Agent`` directly — as the
tests do — never gets a delegation tool unless one is registered explicitly.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from little_agent.tools.base import ToolContext, ToolResult


class _Runnable(Protocol):
    def run(self, user_text: str) -> str: ...


# Build a fresh sub-agent for the given profile name (None = default library).
SpawnCallback = Callable[[str | None], _Runnable]
# List the agent profile names currently available (for hints/validation).
AvailableAgentsCallback = Callable[[], list[str]]


class DelegateTaskTool:
    name = "delegate_task"
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Full, self-contained instructions for the sub-agent. It has no memory of "
                    "this conversation, so include every detail it needs to finish on its own."
                ),
            },
            "agent": {
                "type": "string",
                "description": (
                    "Optional agent profile name to run (see agents/). Omit to use the full "
                    "default library."
                ),
            },
            "background": {
                "type": "string",
                "description": "Optional background or source material to give the sub-agent before the task.",
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        spawn: SpawnCallback,
        available_agents: AvailableAgentsCallback | None = None,
        depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        self._spawn = spawn
        self._available = available_agents or (list)
        self._depth = depth
        self._max_depth = max_depth
        self.description = self._build_description()

    def _build_description(self) -> str:
        base = (
            "Delegate a self-contained subtask to a freshly launched sub-agent that runs "
            "autonomously with its own clean context and returns its final result. Use it for "
            "focused, delegable chunks of work (research, drafting, a file or data operation), "
            "or to hand a task to a specialized agent profile. The sub-agent cannot see this "
            "conversation, so put everything it needs in 'task'."
        )
        try:
            names = self._available()
        except Exception:  # noqa: BLE001 - description must never fail to build.
            names = []
        if names:
            base += " Available agent profiles: " + ", ".join(names) + " (omit 'agent' for the default library)."
        else:
            base += " No specialized profiles exist yet; omit 'agent' to use the default library."
        return base

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return ToolResult(False, "task is required.")
        if self._depth >= self._max_depth:
            return ToolResult(
                False,
                f"Delegation depth limit ({self._max_depth}) reached. Do this work directly "
                "instead of delegating further.",
            )

        agent_name = str(kwargs.get("agent") or "").strip() or None
        extra = str(kwargs.get("background") or "").strip()
        try:
            sub_agent = self._spawn(agent_name)
        except FileNotFoundError:
            available = ", ".join(self._available()) or "(none)"
            return ToolResult(
                False,
                f"No such agent profile: {agent_name}. Available: {available}. "
                "Omit 'agent' to use the default library.",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the parent as tool failure text.
            return ToolResult(False, f"Could not launch sub-agent: {exc}")

        prompt = task if not extra else f"Background:\n{extra}\n\n---\nTask:\n{task}"
        try:
            result = sub_agent.run(prompt)
        except Exception as exc:  # noqa: BLE001 - surfaced to the parent as tool failure text.
            return ToolResult(False, f"Sub-agent run failed: {exc}")

        label = agent_name or "default"
        return ToolResult(True, f"[sub-agent '{label}' result]\n{result}")
