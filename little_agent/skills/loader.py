from __future__ import annotations

import json
import re
from pathlib import Path

from little_agent.skills.models import Skill
from little_agent.skills.script_tool import ScriptSkillTool


class SkillLoader:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir

    def load_all(self) -> list[Skill]:
        if not self.skills_dir.exists():
            return []
        return [self._load(path) for path in sorted(self.skills_dir.glob("*/SKILL.md"))]

    def select_for_text(self, text: str, limit: int = 3) -> list[Skill]:
        skills = self.load_all()
        scored = [(self._score(skill, text), skill) for skill in skills]
        selected = [skill for score, skill in sorted(scored, key=lambda item: item[0], reverse=True) if score > 0]
        return selected[:limit]

    def load_tools(self) -> list[ScriptSkillTool]:
        if not self.skills_dir.exists():
            return []
        tools: list[ScriptSkillTool] = []
        for manifest_path in sorted(self.skills_dir.glob("*/tools.json")):
            tools.extend(self._load_tools_manifest(manifest_path))
        return tools

    def _load(self, path: Path) -> Skill:
        raw = path.read_text(encoding="utf-8")
        name = self._title(raw) or path.parent.name
        description = self._section(raw, "Description")
        when_to_use = self._section(raw, "When to use")
        instructions = self._section(raw, "Instructions")
        allowed_tools = [
            line.strip("- ").strip()
            for line in self._section(raw, "Allowed tools").splitlines()
            if line.strip().startswith("-")
        ]
        return Skill(
            name=name,
            description=description,
            when_to_use=when_to_use,
            allowed_tools=allowed_tools,
            instructions=instructions,
            path=path,
        )

    def _load_tools_manifest(self, path: Path) -> list[ScriptSkillTool]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Skill tool manifest must be a JSON object: {path}")

        skill_dir = path.parent
        tools: list[ScriptSkillTool] = []
        for item in raw.get("tools", []):
            if not isinstance(item, dict):
                continue
            script_path = (skill_dir / str(item["script"])).resolve()
            if skill_dir.resolve() not in [script_path, *script_path.parents]:
                raise ValueError(f"Skill script is outside skill directory: {script_path}")
            tools.append(
                ScriptSkillTool(
                    name=str(item["name"]),
                    description=str(item["description"]),
                    parameters=dict(item.get("parameters") or {}),
                    requires_confirmation=bool(item.get("requires_confirmation", False)),
                    script_path=script_path,
                    timeout_seconds=int(item.get("timeout_seconds", 30)),
                )
            )
        return tools

    @staticmethod
    def _title(raw: str) -> str:
        match = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _section(raw: str, heading: str) -> str:
        pattern = rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
        match = re.search(pattern, raw, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _score(skill: Skill, text: str) -> int:
        haystack = f"{skill.name} {skill.description} {skill.when_to_use}".lower()
        words = {word for word in re.findall(r"[\w一-龯ぁ-んァ-ンー]+", text.lower()) if len(word) >= 2}
        score = sum(1 for word in words if word in haystack)
        if skill.name.lower() in text.lower():
            score += 3
        return score
