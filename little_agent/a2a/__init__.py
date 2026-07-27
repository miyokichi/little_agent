"""Agent2Agent (A2A) protocol support for Little Agent.

Little Agent speaks A2A in both directions:

- **Server** (``little_agent.a2a.server``): publishes an Agent Card at
  ``/.well-known/agent-card.json`` and serves JSON-RPC 2.0 ``message/send``,
  ``tasks/get`` and ``tasks/cancel`` over HTTP, running one fresh agent per task.
- **Client** (``little_agent.a2a.client``): discovers a remote agent by URL and
  drives a task to a terminal state. The ``delegate_task`` tool uses it to hand
  work to peer agents — the peer can be any A2A-compliant agent, not just
  another Little Agent.
"""

from little_agent.a2a.models import (
    PROTOCOL_VERSION,
    TERMINAL_STATES,
    A2AError,
    agent_card,
    parts_to_text,
    text_part,
)

__all__ = [
    "A2AError",
    "PROTOCOL_VERSION",
    "TERMINAL_STATES",
    "agent_card",
    "parts_to_text",
    "text_part",
]
