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
    commands_dir: Path = field(default_factory=lambda: (Path.cwd() / "commands").resolve())
    global_commands_dir: Path = field(default_factory=lambda: Path.home() / ".little_agent" / "commands")
    skill_library_dir: Path = field(default_factory=lambda: (Path.cwd() / "skills").resolve())
    agents_dir: Path = field(default_factory=lambda: (Path.cwd() / "agents").resolve())
    active_agent: str | None = None
    stop_hotkey: str = "<ctrl>+<alt>+q"
    # How deep delegate_task may nest sub-agents (0 disables delegation entirely).
    max_delegation_depth: int = 2
    # How many subtasks delegate_tasks runs concurrently.
    max_parallel_delegations: int = 4

    @classmethod
    def from_env(cls) -> "AgentConfig":
        load_dotenv()
        workspace = Path(os.getenv("LITTLE_AGENT_WORKSPACE", ".")).resolve()
        configured_log_dir = Path(os.getenv("LITTLE_AGENT_LOG_DIR", "logs"))
        log_dir = configured_log_dir if configured_log_dir.is_absolute() else workspace / configured_log_dir
        log_dir = log_dir.resolve()
        configured_commands_dir = Path(os.getenv("LITTLE_AGENT_COMMANDS_DIR", "commands"))
        commands_dir = configured_commands_dir if configured_commands_dir.is_absolute() else workspace / configured_commands_dir
        commands_dir = commands_dir.resolve()
        raw_global_commands = os.getenv("LITTLE_AGENT_GLOBAL_COMMANDS_DIR")
        global_commands_dir = Path(raw_global_commands).resolve() if raw_global_commands else Path.home() / ".little_agent" / "commands"
        configured_library_dir = Path(os.getenv("LITTLE_AGENT_SKILL_LIBRARY_DIR", "skills"))
        skill_library_dir = configured_library_dir if configured_library_dir.is_absolute() else workspace / configured_library_dir
        skill_library_dir = skill_library_dir.resolve()
        configured_agents_dir = Path(os.getenv("LITTLE_AGENT_AGENTS_DIR", "agents"))
        agents_dir = configured_agents_dir if configured_agents_dir.is_absolute() else workspace / configured_agents_dir
        agents_dir = agents_dir.resolve()
        active_agent = os.getenv("LITTLE_AGENT_AGENT") or None
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
            commands_dir=commands_dir,
            global_commands_dir=global_commands_dir,
            skill_library_dir=skill_library_dir,
            agents_dir=agents_dir,
            active_agent=active_agent,
            stop_hotkey=os.getenv("LITTLE_AGENT_STOP_HOTKEY", "<ctrl>+<alt>+q"),
            max_delegation_depth=int(os.getenv("LITTLE_AGENT_MAX_DELEGATION_DEPTH", "2")),
            max_parallel_delegations=max(
                1, int(os.getenv("LITTLE_AGENT_MAX_PARALLEL_DELEGATIONS", "4"))
            ),
        )
