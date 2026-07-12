"""Agent profiles: per-agent skill/tool configuration backed by the filesystem.

An *agent* is a folder under ``agents/`` with an ``agent.json`` profile and a
``skills/`` directory holding skill folders **copied** from the central library
(``skills/``). Which skills an agent has is decided purely by the folders present
in ``agents/<name>/skills/`` — copying/removing folders is the whole mechanism, so
there is nothing to keep in sync inside the JSON.

This module is intentionally free of any dependency on ``Agent``/LLM code so it
can be imported anywhere (CLI, slash commands, tests) without import cycles.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

AGENT_CONFIG_FILE = "agent.json"


def normalize_name(name: str) -> str:
    """Slugify an agent name (mirrors skill_creator's normalization)."""

    lowered = name.strip().lower().replace(" ", "-")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    return normalized[:63]


def _safe_child(root: Path, name: str) -> Path:
    """Resolve ``root/name`` and refuse anything that escapes ``root``."""

    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"Invalid name: {name!r}")
    root = root.resolve()
    child = (root / name).resolve()
    if root not in [child, *child.parents]:
        raise ValueError(f"Path escaped the parent directory: {name!r}")
    return child


@dataclass(slots=True)
class AgentProfile:
    name: str
    dir: Path
    description: str = ""
    model: str | None = None
    core_tools: list[str] | None = None
    max_tool_steps: int | None = None
    require_confirmation: bool | None = None

    @property
    def skills_dir(self) -> Path:
        return self.dir / "skills"

    @property
    def config_path(self) -> Path:
        return self.dir / AGENT_CONFIG_FILE

    def enabled_skills(self) -> list[str]:
        """Skill folder names actually present in this agent's skills dir."""

        if not self.skills_dir.exists():
            return []
        return sorted(
            path.name
            for path in self.skills_dir.iterdir()
            if path.is_dir() and (path / "SKILL.md").exists()
        )

    def core_tools_set(self) -> set[str] | None:
        return set(self.core_tools) if self.core_tools is not None else None


def agent_dir(agents_dir: Path, name: str) -> Path:
    return _safe_child(agents_dir, normalize_name(name))


def profile_exists(agents_dir: Path, name: str) -> bool:
    try:
        return (agent_dir(agents_dir, name) / AGENT_CONFIG_FILE).exists()
    except ValueError:
        return False


def list_agents(agents_dir: Path) -> list[str]:
    if not agents_dir.exists():
        return []
    return sorted(
        path.name
        for path in agents_dir.iterdir()
        if path.is_dir() and (path / AGENT_CONFIG_FILE).exists()
    )


def load_profile(agents_dir: Path, name: str) -> AgentProfile:
    directory = agent_dir(agents_dir, name)
    config_path = directory / AGENT_CONFIG_FILE
    if not config_path.exists():
        raise FileNotFoundError(f"No such agent: {normalize_name(name)}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} must contain a JSON object.")
    core_tools = data.get("core_tools")
    max_steps = data.get("max_tool_steps")
    confirm = data.get("require_confirmation")
    return AgentProfile(
        name=str(data.get("name") or directory.name),
        dir=directory,
        description=str(data.get("description") or ""),
        model=(str(data["model"]) if data.get("model") else None),
        core_tools=[str(item) for item in core_tools] if isinstance(core_tools, list) else None,
        max_tool_steps=int(max_steps) if max_steps is not None else None,
        require_confirmation=bool(confirm) if confirm is not None else None,
    )


def save_profile(profile: AgentProfile) -> None:
    profile.dir.mkdir(parents=True, exist_ok=True)
    data = {
        "name": profile.name,
        "description": profile.description,
        "model": profile.model,
        "core_tools": profile.core_tools,
        "max_tool_steps": profile.max_tool_steps,
        "require_confirmation": profile.require_confirmation,
    }
    profile.config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def library_skill_dir(library_dir: Path, skill: str) -> Path:
    """Path of a skill in the library, validated to exist and be a real skill."""

    src = _safe_child(library_dir, skill)
    if not (src / "SKILL.md").exists():
        raise ValueError(f"Skill not found in library: {skill}")
    return src


def _copy_skill(library_dir: Path, dest_skills_dir: Path, skill: str) -> None:
    src = library_skill_dir(library_dir, skill)
    dst = _safe_child(dest_skills_dir, skill)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def create_agent(
    agents_dir: Path,
    library_dir: Path,
    name: str,
    description: str = "",
    skills: list[str] | None = None,
    core_tools: list[str] | None = None,
    overwrite: bool = False,
) -> AgentProfile:
    normalized = normalize_name(name)
    if not normalized:
        raise ValueError("Agent name is required.")
    directory = agent_dir(agents_dir, normalized)
    if directory.exists() and not overwrite:
        raise FileExistsError(f"Agent already exists: {normalized}")
    (directory / "skills").mkdir(parents=True, exist_ok=True)
    for skill in skills or []:
        _copy_skill(library_dir, directory / "skills", skill)
    profile = AgentProfile(
        name=normalized,
        dir=directory,
        description=description.strip(),
        core_tools=list(core_tools) if core_tools else None,
    )
    save_profile(profile)
    return profile


def add_skill(agents_dir: Path, library_dir: Path, name: str, skill: str) -> AgentProfile:
    profile = load_profile(agents_dir, name)
    _copy_skill(library_dir, profile.skills_dir, skill)
    return profile


def remove_skill(agents_dir: Path, name: str, skill: str) -> AgentProfile:
    profile = load_profile(agents_dir, name)
    target = _safe_child(profile.skills_dir, skill)
    if not target.exists():
        raise FileNotFoundError(f"Agent '{profile.name}' has no skill: {skill}")
    shutil.rmtree(target)
    return profile


def set_core_tools(agents_dir: Path, name: str, core_tools: list[str] | None) -> AgentProfile:
    profile = load_profile(agents_dir, name)
    profile.core_tools = list(core_tools) if core_tools else None
    save_profile(profile)
    return profile


def delete_agent(agents_dir: Path, name: str) -> None:
    directory = agent_dir(agents_dir, name)
    if not directory.exists():
        raise FileNotFoundError(f"No such agent: {normalize_name(name)}")
    shutil.rmtree(directory)


def resolve_active(agents_dir: Path, requested: str | None) -> AgentProfile | None:
    """Return the profile to run, or ``None`` to fall back to the full library.

    ``None``/empty ``requested`` returns ``None``. A named-but-missing agent
    raises ``FileNotFoundError`` so the caller can report it clearly.
    """

    if not requested:
        return None
    return load_profile(agents_dir, requested)
