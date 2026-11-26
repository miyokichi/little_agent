"""Read instructions and bundled resources without granting workspace writes."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from little_agent.skills.models import Skill
from little_agent.tools.base import ToolContext, ToolResult


class ReadSkillTool:
    name = "read_skill"
    description = (
        "Activate a skill by name to read its full instructions. Omit path for SKILL.md. "
        "Read referenced text files on demand by supplying a path relative to that skill's directory."
    )
    requires_confirmation = False

    def __init__(self, skills: list[Skill], on_activate: Callable[[Skill], None]) -> None:
        self.skills = {s.name: s for s in skills}
        self.on_activate = on_activate
        self.parameters = {"type": "object", "properties": {
            "name": {"type": "string", "enum": list(self.skills)},
            "path": {"type": "string", "default": "SKILL.md"},
        }, "required": ["name"], "additionalProperties": False}

    def run(self, context: ToolContext, **kwargs) -> ToolResult:
        skill = self.skills.get(kwargs.get("name"))
        if skill is None or skill.path is None:
            return ToolResult(False, "Unknown or unavailable skill.")
        resource = kwargs.get("path", "SKILL.md")
        if not isinstance(resource, str) or not resource:
            return ToolResult(False, "path must be a relative file path within the skill directory.")
        root = skill.path.parent.resolve()
        try:
            requested = Path(resource)
            target = (root / requested).resolve(strict=True)
            if requested.is_absolute() or not target.is_relative_to(root):
                return ToolResult(False, "Skill resource is outside the skill directory.")
            if not target.is_file():
                return ToolResult(False, "Skill resource is not a file.")
            if target == skill.path.resolve():
                self.on_activate(skill)
                listing = self._resources(root)
                return ToolResult(True, skill.as_prompt() + "\n\nBundled resources (read only when needed):\n" + listing)
            if target.stat().st_size > 256 * 1024:
                return ToolResult(False, "Skill text resource exceeds 256 KiB; read it with an appropriate tool.")
            return ToolResult(True, f"Skill: {skill.name}\nResource: {target}\n\n" + target.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            return ToolResult(False, f"Cannot read skill resource: {exc}")

    @staticmethod
    def _resources(root: Path) -> str:
        files = []
        for count, (directory, dirs, names) in enumerate(os.walk(root, followlinks=False)):
            dirs[:] = sorted(d for d in dirs if d not in {".git", "node_modules", "__pycache__"})
            if count >= 200:
                files.append("(listing truncated)")
                break
            for name in sorted(names):
                path = Path(directory) / name
                if path.name == "SKILL.md" or not path.resolve().is_relative_to(root):
                    continue
                files.append(str(path.relative_to(root)))
                if len(files) >= 200:
                    return "\n".join(files) + "\n(listing truncated)"
        return "\n".join(files) or "(none)"
