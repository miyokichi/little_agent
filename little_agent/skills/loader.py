from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml

from little_agent.skills.frontmatter import parse_frontmatter, metadata_issues
from little_agent.skills.models import Skill
from little_agent.skills.script_tool import ScriptSkillTool


class SkillLoader:
    """Loads skills from one or more directories of skill folders.

    ``roots`` are directories that each contain ``<skill>/SKILL.md`` folders,
    searched in order; the first root holding a given skill folder wins, so an
    agent's own copy shadows the shared library. ``names`` optionally restricts
    loading to those skill folder names (the agent profile's capability list).
    """

    def __init__(self, roots: Path | Sequence[Path], names: Iterable[str] | None = None) -> None:
        self.roots = [roots] if isinstance(roots, Path) else list(roots)
        self.names = set(names) if names is not None else None
        self.diagnostics: list[str] = []
        self._tool_diagnostics: list[str] = []

    def load_all(self) -> list[Skill]:
        self.diagnostics = []
        if self.names == set():
            return []
        found: dict[str, Skill] = {}
        folders: dict[str, Path] = {}
        wanted = {self._key(n) for n in self.names} if self.names is not None else None
        for root in self.roots:
            for path in sorted(root.glob("*/SKILL.md")):
                # Choose a whole folder, not independent SKILL.md/tools.json
                # winners. An override without a manifest must not inherit code.
                folder = self._key(path.parent.name)
                if folder in folders:
                    if folders[folder] != path:
                        self.diagnostics.append(f"Shadowed skill folder: {path} (using {folders[folder]})")
                    continue
                folders[folder] = path
                try:
                    skill = self._load(path)
                except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                    self.diagnostics.append(f"Skipped skill {path}: {exc}")
                    continue
                key = self._key(skill.name)
                if wanted is not None and key not in wanted and folder not in wanted:
                    continue
                if key in found:
                    self.diagnostics.append(f"Shadowed skill name {skill.name}: {path} (using {found[key].path})")
                    continue
                found[key] = skill
        return sorted(found.values(), key=lambda s: s.name)

    @staticmethod
    def _key(name: str) -> str:
        # Compatibility for existing profiles naming built-ins with underscores.
        return name.replace("_", "-")

    def get(self, name: str) -> Skill:
        match = next((s for s in self.load_all() if self._key(s.name) == self._key(name)), None)
        if match is None:
            raise ValueError(f"Unknown or unavailable skill: {name}")
        return match

    def warning(self) -> str | None:
        skills = self.load_all()
        lines = [*self.diagnostics, *self._tool_diagnostics]
        empty = library_warning(self.roots, skills, self.names)
        if empty:
            lines.append(empty)
        return "\n".join(lines) or None

    def select_for_text(self, text: str, limit: int = 3) -> list[Skill]:
        """Compatibility API: explicit $skill mentions only, never keyword guessing."""
        mentions = {self._key(n) for n in re.findall(r"(?<![\w-])\$([\w-]+)", text)}
        return [s for s in self.load_all() if self._key(s.name) in mentions][:limit]

    def load_tools(self) -> list[ScriptSkillTool]:
        """tools.json is an optional Little Agent extension, not a skill requirement."""
        tools: list[ScriptSkillTool] = []
        self._tool_diagnostics = []
        for skill in self.load_all():
            manifest_path = skill.path.parent / "tools.json"
            if not manifest_path.is_file():
                continue
            try:
                tools.extend(self._load_tools_manifest(manifest_path))
            except (OSError, KeyError, TypeError, ValueError) as exc:
                self._tool_diagnostics.append(f"Skipped skill tool manifest {manifest_path}: {exc}")
        return tools

    def _load(self, path: Path) -> Skill:
        if not path.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError("SKILL.md resolves outside its skill directory")
        raw = path.read_text(encoding="utf-8-sig")
        if raw.startswith("---"):
            data, body = parse_frontmatter(raw)
            self.diagnostics.extend(f"{path}: {issue}" for issue in metadata_issues(data, path.parent))
            return Skill(name=data["name"], description=data["description"],
                         instructions=body, allowed_tools=data.get("allowed-tools", "").split(),
                         path=path, license=data.get("license"), compatibility=data.get("compatibility"),
                         metadata=data.get("metadata", {}))
        # Migration support for pre-standard Little Agent skills. Keep the full
        # body so examples and headings outside 'Instructions' are never lost.
        description = self._section(raw, "Description")
        if not description:
            raise ValueError("Missing YAML frontmatter and legacy Description")
        self.diagnostics.append(f"{path}: legacy skill; migrate to YAML frontmatter with name/description")
        allowed = [line.strip("- ").strip() for line in self._section(raw, "Allowed tools").splitlines()
                   if line.strip().startswith("-")]
        return Skill(name=self._title(raw) or path.parent.name, description=description,
                     when_to_use=self._section(raw, "When to use"), instructions=raw.strip(),
                     allowed_tools=allowed, path=path, legacy=True)

    def _load_tools_manifest(self, path: Path) -> list[ScriptSkillTool]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Skill tool manifest must be a JSON object: {path}")

        skill_dir = path.parent
        tools: list[ScriptSkillTool] = []
        manifest_tools = raw.get("tools", [])
        if not isinstance(manifest_tools, list):
            raise ValueError("'tools' must be a list.")
        for item in manifest_tools:
            if not isinstance(item, dict):
                continue
            parameters = item.get("parameters") or {}
            if not isinstance(parameters, dict):
                raise ValueError(f"Tool '{item.get('name', '(unknown)')}' parameters must be a JSON Schema object.")
            script_path = (skill_dir / str(item["script"])).resolve()
            if skill_dir.resolve() not in [script_path, *script_path.parents]:
                raise ValueError(f"Skill script is outside skill directory: {script_path}")
            if not script_path.exists():
                raise ValueError(f"Skill script does not exist: {script_path}")
            tools.append(
                ScriptSkillTool(
                    name=str(item["name"]),
                    description=str(item["description"]),
                    parameters=parameters,
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



def library_warning(roots: list[Path], skills: list[Skill], names: set[str] | None) -> str | None:
    """A warning for a library that produced no skills, or ``None`` when fine.

    An agent with no skills still runs — it just has none of the knowledge the
    library carries — so this is a warning rather than a failure. A profile that
    deliberately declares ``"skills": []`` is not warned about: it asked for
    nothing and got nothing.
    """

    if skills or names == set():
        return None
    searched = ", ".join(str(root) for root in roots) or "(nowhere)"
    missing = [str(root) for root in roots if not root.is_dir()]
    lines = [f"Warning: no skills were loaded. Searched: {searched}"]
    if missing:
        lines.append(f"  These directories do not exist: {', '.join(missing)}")
    if names:
        lines.append(f"  The agent profile asked for: {', '.join(sorted(names))}")
    lines.append(
        "  Skills are resolved as LITTLE_AGENT_SKILL_LIBRARY_DIR, then "
        "<workspace>/.agents/skills, <workspace>/skills, then the built-in library."
    )
    return "\n".join(lines)
