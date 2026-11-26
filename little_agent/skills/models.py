from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    when_to_use: str = ""  # Legacy headings remain supported during migration.
    allowed_tools: list[str] = field(default_factory=list)
    instructions: str = ""  # Entire Markdown body, without frontmatter.
    path: Path | None = None
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    legacy: bool = False

    def as_prompt(self) -> str:
        location = str(self.path.resolve()) if self.path else "(unknown)"
        root = str(self.path.resolve().parent) if self.path else "(unknown)"
        header = f"# Skill: {self.name}\nDescription: {self.description}\nLocation: {location}\nSkill directory: {root}"
        if self.compatibility:
            header += f"\nCompatibility: {self.compatibility}"
        if self.license:
            header += f"\nLicense: {self.license}"
        if self.metadata:
            header += "\nMetadata: " + json.dumps(self.metadata, ensure_ascii=False)
        if self.allowed_tools:
            header += "\nDeclared allowed-tools (advisory; host permissions still apply): " + " ".join(self.allowed_tools)
        return header + "\nResolve relative references against the skill directory.\n\n" + self.instructions
