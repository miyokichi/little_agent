from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from little_agent.tools.base import ToolContext, ToolResult


@dataclass(frozen=True, slots=True)
class ScriptSkillTool:
    name: str
    description: str
    parameters: dict[str, Any]
    script_path: Path
    requires_confirmation: bool = False
    timeout_seconds: int = 30

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        payload = {
            "tool": self.name,
            "workspace": str(context.workspace),
            "readable_paths": [str(path) for path in context.readable_paths],
            "writable_paths": [str(path) for path in context.writable_paths],
            "arguments": kwargs,
        }
        completed = subprocess.run(
            [sys.executable, str(self.script_path), self.name],
            cwd=self.script_path.parent.parent,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            return ToolResult(False, stderr or f"Script exited with code {completed.returncode}")

        raw = completed.stdout.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ToolResult(False, f"Script returned invalid JSON: {raw[:500]}")

        if not isinstance(data, dict):
            return ToolResult(False, "Script returned a non-object JSON response.")
        raw_images = data.get("images") or []
        images = tuple(str(item) for item in raw_images) if isinstance(raw_images, list) else ()
        return ToolResult(
            ok=bool(data.get("ok")),
            content=str(data.get("content", "")),
            images=images,
        )
