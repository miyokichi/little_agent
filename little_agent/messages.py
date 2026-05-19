from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class Message:
    role: Role
    content: str
    name: str | None = None

