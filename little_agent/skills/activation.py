"""``activate_skill``: how the model loads a skill's instructions.

The catalog in the system prompt names each skill; when the model decides one
fits the task it calls this tool with the name and receives the ``SKILL.md``
body wrapped in ``<skill_content>``, with the skill directory and a listing of
its bundled files (not their contents — those are read on demand).

Which skill to use is the model's decision, not the harness's: nothing is
pre-selected by keyword matching. Activation is also what *loads* a skill — the
script tools a skill declares in ``tools.json`` are registered only when the
model activates it (``on_activate``), so a skill's tools never reach the model
until it has chosen that skill.

The ``name`` parameter is an enum of the skills that exist, so the model cannot
ask for one that does not. A skill already activated in this run is not
injected a second time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from little_agent.skills.loader import SkillLoader
from little_agent.skills.models import Skill
from little_agent.tools.base import ToolContext, ToolResult

TOOL_NAME = "activate_skill"


class ActivateSkillTool:
    name = TOOL_NAME
    requires_confirmation = False

    def __init__(
        self,
        loader: SkillLoader,
        on_activate: Callable[[Skill], list[str]] | None = None,
    ) -> None:
        self.loader = loader
        # Called with the activated skill; returns the tool names it made available.
        self.on_activate = on_activate
        self._names: list[str] = []
        self.activated: set[str] = set()
        self.refresh()

    def refresh(self, names: list[str] | None = None) -> None:
        self._names = names if names is not None else [skill.name for skill in self.loader.load_all()]

    def begin_run(self) -> None:
        """Forget what was activated: each run starts with no skill in context."""

        self.activated = set()

    @property
    def description(self) -> str:
        return (
            "Load a skill's full instructions (and the list of its bundled files) "
            "when the task matches the skill's description in <available_skills>."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": list(self._names),
                    "description": "The skill name, exactly as listed in <available_skills>.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        requested = str(kwargs.get("name") or "").strip()
        skill = self.loader.get(requested) if requested else None
        if skill is None:
            known = ", ".join(self._names) or "(none)"
            return ToolResult(False, f"Unknown skill: {requested or '(empty)'}. Available: {known}")
        if skill.name in self.activated:
            return ToolResult(True, f"Skill '{skill.name}' is already active; its instructions are above.")
        self.activated.add(skill.name)
        added = self.on_activate(skill) if self.on_activate is not None else []
        content = skill.activation_content()
        if added:
            content += f"\n\nTools now available from this skill: {', '.join(added)}"
        return ToolResult(True, content)
