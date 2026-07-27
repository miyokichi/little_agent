"""Delegate a subtask to a peer agent over the A2A (Agent2Agent) protocol.

``delegate_task`` is an A2A **client**: it discovers the peer's Agent Card, sends
``message/send``, polls ``tasks/get`` until the task reaches a terminal state,
and returns the resulting artifact text.

The peer can be any A2A-compliant agent reachable by URL, or a local ``agents/``
profile — in which case a local A2A server is started on demand (see
``little_agent.a2a.peers``) so the hand-off still goes over the real protocol.
Delegation depth travels in the message metadata, so a chain of hand-offs
between Little Agents cannot recurse forever.
"""

from __future__ import annotations

from typing import Any

from little_agent.a2a.client import A2AClientError
from little_agent.a2a.models import TASK_COMPLETED, task_result_text
from little_agent.a2a.peers import PeerPool, shared_pool
from little_agent.config import AgentConfig
from little_agent.tools.base import ToolContext, ToolResult

DEFAULT_TASK_TIMEOUT = 300.0


class DelegateTaskTool:
    name = "delegate_task"
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Full, self-contained instructions for the peer agent. It does not share "
                    "this conversation, so include every detail it needs to finish on its own."
                ),
            },
            "agent": {
                "type": "string",
                "description": (
                    "Peer to delegate to: a local agent profile name or a configured remote "
                    "peer name. Omit for the default library agent."
                ),
            },
            "agent_url": {
                "type": "string",
                "description": (
                    "Base URL of an A2A agent to delegate to (e.g. http://127.0.0.1:8801/). "
                    "Overrides 'agent'. Use for peers not in the configured list."
                ),
            },
            "background": {
                "type": "string",
                "description": "Optional background or source material to send before the task.",
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        config: AgentConfig,
        depth: int = 0,
        pool: PeerPool | None = None,
        timeout: float = DEFAULT_TASK_TIMEOUT,
    ) -> None:
        self._config = config
        self._depth = depth
        self._pool = pool or shared_pool(config)
        self._timeout = timeout
        self.description = self._build_description()

    def _build_description(self) -> str:
        base = (
            "Delegate a self-contained subtask to a peer agent over the A2A (Agent2Agent) "
            "protocol and return its result. The peer runs the task independently with its own "
            "context, so put everything it needs in 'task'. Use it for focused, delegable work "
            "(research, drafting, a file or data operation) or to hand a task to a specialized agent."
        )
        try:
            names = self._pool.available()
        except Exception:  # noqa: BLE001 - description must never fail to build.
            names = []
        if names:
            base += " Available peers: " + ", ".join(names) + "."
        return base

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        task_text = str(kwargs.get("task") or "").strip()
        if not task_text:
            return ToolResult(False, "task is required.")
        if self._depth >= self._config.max_delegation_depth:
            return ToolResult(
                False,
                f"Delegation depth limit ({self._config.max_delegation_depth}) reached. "
                "Do this work directly instead of delegating further.",
            )

        agent_name = str(kwargs.get("agent") or "").strip() or None
        agent_url = str(kwargs.get("agent_url") or "").strip() or None
        background = str(kwargs.get("background") or "").strip()

        try:
            client = self._pool.connect(name=agent_name, url=agent_url)
        except FileNotFoundError:
            available = ", ".join(self._pool.available()) or "(none)"
            return ToolResult(
                False,
                f"No such agent profile: {agent_name}. Available peers: {available}. "
                "Omit 'agent' to use the default library agent.",
            )
        except A2AClientError as exc:
            return ToolResult(False, f"Could not connect to the peer agent: {exc}")
        except Exception as exc:  # noqa: BLE001 - surfaced to the parent as tool failure text.
            return ToolResult(False, f"Could not reach the peer agent: {exc}")

        prompt = task_text if not background else f"Background:\n{background}\n\n---\nTask:\n{task_text}"
        try:
            result = client.run_task(prompt, depth=self._depth + 1, timeout=self._timeout)
        except A2AClientError as exc:
            return ToolResult(False, f"A2A task failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - surfaced to the parent as tool failure text.
            return ToolResult(False, f"A2A task failed: {exc}")

        label = agent_url or agent_name or client.name
        output = task_result_text(result) or "(peer returned no content)"
        if result.get("kind") == "message":
            return ToolResult(True, f"[A2A peer '{label}' replied]\n{output}")

        state = str((result.get("status") or {}).get("state") or "unknown")
        if state != TASK_COMPLETED:
            return ToolResult(
                False, f"[A2A peer '{label}' task {state}]\n{output}"
            )
        return ToolResult(True, f"[A2A peer '{label}' result]\n{output}")
