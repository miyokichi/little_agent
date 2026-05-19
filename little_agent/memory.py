from __future__ import annotations

from dataclasses import dataclass, field

from little_agent.messages import Message


@dataclass(slots=True)
class ConversationMemory:
    messages: list[Message] = field(default_factory=list)

    def add(self, role: str, content: str, name: str | None = None) -> None:
        self.messages.append(Message(role=role, content=content, name=name))  # type: ignore[arg-type]

    def latest_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.role == "user":
                return message.content
        return ""

