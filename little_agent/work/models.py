"""Coordinator checkpoints contain execution metadata, never a knowledge store."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


class LearningError(RuntimeError):
    """The peer is unavailable or its response violates the negotiated contract."""


def identity(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass
class WorkCheckpoint:
    work_id: str
    conversation_id: str
    goal: str
    state: str = "STARTING"
    last_action: str = ""
    next_action: str = ""
    decision_target: str = ""
    questions: list[dict[str, str]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_answer: dict[str, Any] | None = None
    recorded_units: list[str] = field(default_factory=list)
    completion: dict[str, Any] | None = None
    reflection_pending: bool = False
    rpc_sequences: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
