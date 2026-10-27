"""A skill in the Agent Skills format (https://agentskills.io/specification)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

# Files listed for the model on activation, at most; it reads them on demand.
MAX_LISTED_RESOURCES = 50
_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}


@dataclass(frozen=True, slots=True)
class Skill:
    """One discovered skill.

    ``name`` and ``description`` come from the ``SKILL.md`` frontmatter and are
    all the model sees until it activates the skill; ``body`` is the markdown
    after the frontmatter — the instructions it receives on activation.
    """

    name: str
    description: str
    body: str = ""
    path: Path | None = None
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    # ``allowed-tools`` (experimental in the spec). Shown on activation; it does
    # not bypass Little Agent's confirmation prompts.
    allowed_tools: tuple[str, ...] = ()
    # Read from the pre-standard heading layout rather than frontmatter.
    legacy: bool = False

    @property
    def directory(self) -> Path | None:
        return self.path.parent if self.path is not None else None

    @property
    def folder(self) -> str:
        """The skill's directory name (what profiles select by)."""

        return self.path.parent.name if self.path is not None else self.name

    def resources(self, limit: int = MAX_LISTED_RESOURCES) -> tuple[list[str], bool]:
        """Bundled files relative to the skill directory, and whether truncated."""

        directory = self.directory
        if directory is None or not directory.is_dir():
            return [], False
        found: list[str] = []
        for item in sorted(directory.rglob("*")):
            relative = item.relative_to(directory)
            if any(part in _SKIP_DIRS or part.startswith(".") for part in relative.parts):
                continue
            if not item.is_file() or relative.as_posix() == "SKILL.md":
                continue
            if len(found) >= limit:
                return found, True
            found.append(relative.as_posix())
        return found, False

    def catalog_entry(self) -> str:
        """Tier 1 of progressive disclosure: what the model sees up front."""

        lines = [
            "  <skill>",
            f"    <name>{escape(self.name)}</name>",
            f"    <description>{escape(self.description)}</description>",
        ]
        if self.path is not None:
            lines.append(f"    <location>{escape(str(self.path))}</location>")
        lines.append("  </skill>")
        return "\n".join(lines)

    def activation_content(self) -> str:
        """Tier 2: the instructions, wrapped so they are recognisable in context."""

        parts = [f'<skill_content name="{escape(self.name)}">', self.body.strip()]
        if self.compatibility:
            parts.append(f"Compatibility: {self.compatibility}")
        if self.allowed_tools:
            parts.append(f"Allowed tools: {' '.join(self.allowed_tools)}")
        if self.directory is not None:
            parts.append(
                f"Skill directory: {self.directory}\n"
                "Relative paths in this skill are relative to the skill directory."
            )
            files, truncated = self.resources()
            if files:
                listing = "\n".join(f"  <file>{escape(name)}</file>" for name in files)
                if truncated:
                    listing += "\n  <!-- more files not listed -->"
                parts.append(f"<skill_resources>\n{listing}\n</skill_resources>")
        parts.append("</skill_content>")
        return "\n\n".join(part for part in parts if part)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "location": str(self.path) if self.path else None,
            "license": self.license,
            "compatibility": self.compatibility,
            "metadata": dict(self.metadata),
            "allowed_tools": list(self.allowed_tools),
            "legacy": self.legacy,
        }


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    """A problem found while loading a skill. ``error`` means it was skipped."""

    path: Path
    message: str
    level: str = "warning"

    def __str__(self) -> str:
        return f"{self.level}: {self.path}: {self.message}"
