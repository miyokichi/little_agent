from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    max_tool_steps: int = 5
    enable_logging: bool = False
    log_dir: Path | None = None
    llm_timeout_seconds: int = 60
    global_memory_path: Path = field(default_factory=lambda: Path.home() / ".little_agent" / "memory.md")
    stop_hotkey: str = "<ctrl>+<alt>+q"

    @classmethod
    def from_env(cls) -> "AgentConfig":
        load_dotenv()
        workspace = Path(os.getenv("LITTLE_AGENT_WORKSPACE", ".")).resolve()
        configured_log_dir = Path(os.getenv("LITTLE_AGENT_LOG_DIR", "logs"))
        log_dir = configured_log_dir if configured_log_dir.is_absolute() else workspace / configured_log_dir
        log_dir = log_dir.resolve()
        raw_global_memory = os.getenv("LITTLE_AGENT_GLOBAL_MEMORY_PATH")
        global_memory_path = Path(raw_global_memory).resolve() if raw_global_memory else Path.home() / ".little_agent" / "memory.md"
        return cls(
            model=os.getenv("LITTLE_AGENT_MODEL", "gpt-4.1-mini"),
            workspace=workspace,
            require_confirmation=_as_bool(os.getenv("LITTLE_AGENT_REQUIRE_CONFIRMATION"), True),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            max_tool_steps=int(os.getenv("LITTLE_AGENT_MAX_TOOL_STEPS", "5")),
            enable_logging=_as_bool(os.getenv("LITTLE_AGENT_ENABLE_LOGGING"), True),
            log_dir=log_dir,
            llm_timeout_seconds=int(os.getenv("LITTLE_AGENT_TIMEOUT_SECONDS", "60")),
            global_memory_path=global_memory_path,
            stop_hotkey=os.getenv("LITTLE_AGENT_STOP_HOTKEY", "<ctrl>+<alt>+q"),
        )
