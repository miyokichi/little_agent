"""An explicit intermediate report that keeps the agent's tool loop running."""
from little_agent.tools.base import ToolContext, ToolResult


class ReportProgressTool:
    name = "report_progress"
    description = "Show a brief progress update to the user and continue working. This is not a final answer."
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string", "description": "Brief user-facing progress update."}},
        "required": ["message"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        message = str(kwargs.get("message", "")).strip()
        if not message:
            return ToolResult(False, "A non-empty progress message is required.")
        if context.report_progress is not None:
            context.report_progress(message)
        return ToolResult(True, "Progress reported. Continue the task; give a final answer when finished.")
