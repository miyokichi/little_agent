from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str
    workspace: Path
    require_confirmation: bool
    openai_api_key: str | None
    openai_base_url: str

    @classmethod
    def from_env(cls) -> "AgentConfig":
        load_dotenv()
        workspace = Path(os.getenv("LITTLE_AGENT_WORKSPACE", ".")).resolve()
        return cls(
            model=os.getenv("LITTLE_AGENT_MODEL", "gpt-4.1-mini"),
            workspace=workspace,
            require_confirmation=_as_bool(os.getenv("LITTLE_AGENT_REQUIRE_CONFIRMATION"), True),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
