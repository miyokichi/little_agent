"""Discovering and parsing Agent Skills.

A skill is a directory with a ``SKILL.md``: YAML frontmatter (``name``,
``description``, and the optional ``license`` / ``compatibility`` /
``metadata`` / ``allowed-tools``) followed by markdown instructions, plus any
bundled ``scripts/``, ``references/`` and ``assets/``. This is the open Agent
Skills format (https://agentskills.io/specification), so skills written for
other compliant clients load here unchanged, and the other way round.

Loading follows the format's *progressive disclosure*:

1. **Catalog** — only ``name`` + ``description`` (+ location) go into the
   system prompt (:meth:`SkillLoader.catalog_prompt`).
2. **Instructions** — the ``SKILL.md`` body is handed over when the model
   activates the skill (the ``activate_skill`` tool, or ``/<skill-name>`` in
   chat).
3. **Resources** — bundled files are listed on activation and read on demand.

Validation is lenient, as the client guide recommends: a name that breaks the
naming rules or does not match its directory is warned about and the skill
still loads; a skill with no description, or with frontmatter that cannot be
parsed, is skipped. Every problem is kept as a :class:`SkillDiagnostic`.

Little Agent extensions live beside the standard files and are optional: a
``tools.json`` declares script-backed tools (see :mod:`.script_tool`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from little_agent.skills.frontmatter import FrontmatterError, parse_frontmatter
from little_agent.skills.models import Skill, SkillDiagnostic
from little_agent.skills.script_tool import ScriptSkillTool

SKILL_FILE = "SKILL.md"
NAME_MAX = 64
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500
SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}

_NAME_RULE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def normalize_skill_name(name: str) -> str:
    """The comparison key for a skill name: case- and ``_``/``-``-insensitive.

    Lets an agent profile written for the old ``file_manager`` folders keep
    selecting the spec-style ``file-manager`` skill.
    """

    return name.strip().lower().replace("_", "-")


def name_problems(name: str, folder: str) -> list[str]:
    """Ways ``name`` breaks the spec's naming rules (empty when it is fine)."""

    problems = []
    if len(name) > NAME_MAX:
        problems.append(f"name is longer than {NAME_MAX} characters")
    if not _NAME_RULE.match(name):
        problems.append(
            "name may only contain lowercase letters, digits and single hyphens, "
            "and must not start or end with a hyphen"
        )
    if name != folder:
        problems.append(f"name {name!r} does not match its directory {folder!r}")
    return problems


class SkillLoader:
    """Loads skills from directories of skill folders.

    ``roots`` are searched in order and the first root holding a skill of a given
    name wins (a warning records the shadowed copy), so pass the most specific
    root first — an agent's own skills, then project, then user, then built-in.
    ``names`` optionally restricts loading to those skills (an agent profile's
    capability list), matched against folder or skill name.
    """

    def __init__(self, roots: Path | Sequence[Path], names: Iterable[str] | None = None) -> None:
        self.roots = [roots] if isinstance(roots, Path) else list(roots)
        self.names = set(names) if names is not None else None
        self._diagnostics: list[SkillDiagnostic] = []

    # --- discovery -----------------------------------------------------------

    def _wanted(self, skill: Skill) -> bool:
        if self.names is None:
            return True
        wanted = {normalize_skill_name(name) for name in self.names}
        return normalize_skill_name(skill.name) in wanted or normalize_skill_name(skill.folder) in wanted

    def _discover(self) -> dict[str, Skill]:
        diagnostics: list[SkillDiagnostic] = []
        found: dict[str, Skill] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for skill_file in sorted(root.glob(f"*/{SKILL_FILE}")):
                if skill_file.parent.name.startswith("."):
                    continue
                skill = parse_skill_file(skill_file, diagnostics)
                if skill is None or not self._wanted(skill):
                    continue
                key = normalize_skill_name(skill.name)
                if key in found:
                    diagnostics.append(
                        SkillDiagnostic(
                            skill_file,
                            f"skill '{skill.name}' is shadowed by {found[key].path}",
                        )
                    )
                    continue
                found[key] = skill
        self._diagnostics = diagnostics
        return found

    def load_all(self) -> list[Skill]:
        """Every loadable skill, sorted by name. Re-reads the disk each call."""

        return sorted(self._discover().values(), key=lambda skill: skill.name)

    def get(self, name: str) -> Skill | None:
        return self._discover().get(normalize_skill_name(name))

    def diagnostics(self) -> list[SkillDiagnostic]:
        """Problems found by the most recent load (call ``load_all`` first)."""

        return list(self._diagnostics)

    def warning(self) -> str | None:
        """Why this loader found nothing, or ``None`` when it found skills."""

        return library_warning(self.roots, self.load_all(), self.names)

    def skill_dirs(self) -> tuple[Path, ...]:
        """Directories of the loaded skills, for read-only access to resources."""

        return tuple(skill.directory for skill in self.load_all() if skill.directory is not None)

    # --- disclosure ----------------------------------------------------------

    def catalog_prompt(self, skills: list[Skill] | None = None) -> str:
        """The system-prompt section listing skills, or ``""`` when there are none."""

        skills = self.load_all() if skills is None else skills
        if not skills:
            return ""
        entries = "\n".join(skill.catalog_entry() for skill in skills)
        return (
            "The following skills provide specialized instructions for specific tasks. "
            "When a task matches a skill's description, call the activate_skill tool with "
            "the skill's name to load its full instructions before proceeding. Choose the "
            "skill yourself from the descriptions; activate only the skills the task needs. "
            "A skill's own tools become available only after it is activated. When a skill "
            "references relative paths, resolve them against the skill's directory and use "
            "absolute paths in tool calls.\n\n"
            f"<available_skills>\n{entries}\n</available_skills>"
        )

    # --- Little Agent extension: script tools --------------------------------

    def load_tools(self) -> list[ScriptSkillTool]:
        """Every skill's script tools (the agent loads them per skill on activation)."""

        tools: list[ScriptSkillTool] = []
        for skill in self.load_all():
            tools.extend(self.tools_for(skill))
        return tools

    def tools_for(self, skill: Skill) -> list[ScriptSkillTool]:
        """The script tools one skill declares in its ``tools.json`` (if any)."""

        manifest_path = skill.directory / "tools.json" if skill.directory else None
        if manifest_path is None or not manifest_path.is_file():
            return []
        try:
            return _load_tools_manifest(manifest_path)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"Warning: skipped invalid skill tool manifest {manifest_path}: {exc}")
            return []


def parse_skill_file(path: Path, diagnostics: list[SkillDiagnostic] | None = None) -> Skill | None:
    """Parse one ``SKILL.md``; ``None`` (with an error diagnostic) when unusable."""

    notes = diagnostics if diagnostics is not None else []
    folder = path.parent.name
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        notes.append(SkillDiagnostic(path, f"cannot read SKILL.md: {exc}", "error"))
        return None
    try:
        fields, body, has_frontmatter = parse_frontmatter(raw)
    except FrontmatterError as exc:
        notes.append(SkillDiagnostic(path, f"frontmatter cannot be parsed: {exc}", "error"))
        return None
    if not has_frontmatter:
        return _parse_legacy(path, raw, notes)

    name = _text(fields.get("name"))
    if not name:
        notes.append(SkillDiagnostic(path, f"frontmatter has no 'name'; using the directory name {folder!r}"))
        name = folder
    for problem in name_problems(name, folder):
        notes.append(SkillDiagnostic(path, problem))

    description = _text(fields.get("description"))
    if not description:
        notes.append(SkillDiagnostic(path, "frontmatter has no 'description'; skill skipped", "error"))
        return None
    if len(description) > DESCRIPTION_MAX:
        notes.append(SkillDiagnostic(path, f"description is longer than {DESCRIPTION_MAX} characters"))

    compatibility = _text(fields.get("compatibility")) or None
    if compatibility and len(compatibility) > COMPATIBILITY_MAX:
        notes.append(SkillDiagnostic(path, f"compatibility is longer than {COMPATIBILITY_MAX} characters"))

    metadata_raw = fields.get("metadata")
    metadata: dict[str, str] = {}
    if isinstance(metadata_raw, dict):
        metadata = {str(key): _text(value) for key, value in metadata_raw.items()}
    elif metadata_raw is not None:
        notes.append(SkillDiagnostic(path, "metadata must be a mapping of strings; ignored"))

    unknown = sorted(set(fields) - SPEC_FIELDS)
    if unknown:
        notes.append(SkillDiagnostic(path, f"unknown frontmatter field(s): {', '.join(unknown)}"))

    return Skill(
        name=name,
        description=description,
        body=body,
        path=path.resolve(),
        license=_text(fields.get("license")) or None,
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=_tool_list(fields.get("allowed-tools")),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _tool_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str):
        return tuple(value.split())
    return ()


_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _section(raw: str, heading: str) -> str:
    pattern = rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
    match = re.search(pattern, raw, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_legacy(path: Path, raw: str, notes: list[SkillDiagnostic]) -> Skill | None:
    """Read the pre-standard layout (``# name`` + ``## Description`` ...)."""

    title = _TITLE.search(raw)
    name = title.group(1).strip() if title else path.parent.name
    description = _section(raw, "Description")
    when = _section(raw, "When to use")
    if when:
        bullets = "; ".join(line.strip("-* ").strip() for line in when.splitlines() if line.strip())
        description = f"{description} Use when: {bullets}".strip()
    if not description:
        notes.append(SkillDiagnostic(path, "no frontmatter and no '## Description'; skill skipped", "error"))
        return None
    tools = tuple(
        line.strip("-* ").strip()
        for line in _section(raw, "Allowed tools").splitlines()
        if line.strip().startswith(("-", "*"))
    )
    body = _section(raw, "Instructions") or raw.strip()
    notes.append(
        SkillDiagnostic(
            path,
            "no YAML frontmatter; read as the legacy heading format. Add a "
            "'---' block with name and description (Agent Skills format).",
        )
    )
    return Skill(
        name=name,
        description=description,
        body=body,
        path=path.resolve(),
        allowed_tools=tools,
        legacy=True,
    )


def _load_tools_manifest(path: Path) -> list[ScriptSkillTool]:
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
        "  The library is LITTLE_AGENT_SKILL_LIBRARY_DIR, else <workspace>/skills, "
        "else the built-in library; <workspace>/.agents/skills and ~/.agents/skills "
        "are searched too."
    )
    return "\n".join(lines)
