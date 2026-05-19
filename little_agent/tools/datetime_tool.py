from __future__ import annotations

from datetime import datetime

from little_agent.tools.base import ToolContext, ToolResult


class GetDateTimeTool:
    name = "get_datetime"
    description = "Return the current local date and time."
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        return ToolResult(ok=True, content=datetime.now().astimezone().isoformat(timespec="seconds"))

