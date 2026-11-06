"""A strict, thin MCP adapter. No imports from the Learning Engine package.

The wire contract (including idempotency) is documented in docs/learning.md.
McpManager owns subprocess lifecycle; no automatic retries of mutations here.
"""
from __future__ import annotations

import json
from typing import Any

from little_agent.mcp.client import McpError
from little_agent.mcp.manager import McpManager
from little_agent.schema import validate
from little_agent.work.models import LearningError, identity

SERVER = "learning_engine"
MUTATIONS = {
    "le_start_work", "le_pause_work", "le_evaluate", "le_record_answer",
    "le_confirm_answer", "le_resume_work", "le_record_progress", "le_finish_work",
    "le_reflect", "le_review_candidate",
}
TOOLS = MUTATIONS | {"le_get_resume_context", "le_get_approved_knowledge",
                     "le_list_work", "le_get_review"}


class LearningClient:
    def __init__(self, manager: McpManager) -> None:
        self.manager = manager

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        if name not in TOOLS:
            raise LearningError(f"Unsupported Learning Engine operation: {name}")
        if name in MUTATIONS:
            arguments.setdefault("request_id", identity([name, arguments]))
        try:
            if SERVER not in self.manager.states:
                raise LearningError("mcp.json に learning_engine (stdio) の設定がありません。")
            state = self.manager.ensure(SERVER)
            if state.config.transport != "stdio":
                raise LearningError("Learning Engine は stdio MCP で接続してください。")
            if state.status != "connected" or state.client is None:
                raise LearningError(f"Learning Engine 接続失敗: {state.error or state.status}")
            if state.client.tools_changed:
                if not self.manager.refresh_tools(state):
                    raise LearningError("Learning Engine のTool一覧を更新できません。")
            spec = next((s for s in state.tool_specs if s.get("name") == name), None)
            if spec is None:
                raise LearningError(f"Learning Engine のAPIが未実装または許可リスト外です: {name}")
            errors = validate(arguments, spec.get("inputSchema", {}))
            if errors:
                raise LearningError(f"Learning Engine API契約が一致しません ({name}): {'; '.join(errors)}")
            result = state.client.call_tool(name, arguments)
        except (McpError, OSError, ValueError) as exc:
            raise LearningError(f"Learning Engine 通信失敗 ({name}): {exc}") from exc
        if result.get("isError"):
            raise LearningError(f"Learning Engine が {name} を拒否しました: {result.get('content', [])}")
        data = result.get("structuredContent")
        if data is None:
            blocks = result.get("content", [])
            try:
                data = json.loads("\n".join(b.get("text", "") for b in blocks
                                           if isinstance(b, dict) and b.get("type") == "text"))
            except (ValueError, TypeError) as exc:
                raise LearningError(f"Learning Engine の応答がJSONではありません ({name})") from exc
        if not isinstance(data, dict):
            raise LearningError(f"Learning Engine の応答はobjectが必要です ({name})")
        return data

    def approved_knowledge(self, work_id: str, goal: str) -> list[dict[str, Any]]:
        data = self.call("le_get_approved_knowledge", work_id=work_id, query=goal)
        items = data.get("knowledge")
        if not isinstance(items, list) or any(
            not isinstance(k, dict) or k.get("status") != "approved"
            or not isinstance(k.get("id"), str) or not isinstance(k.get("text"), str)
            for k in items
        ):
            raise LearningError("承認済み知識の応答に未承認・不正な知識が混入しています。")
        # Project only fields required for reuse; omit drafts embedded in metadata.
        return [{k: item[k] for k in ("id", "text", "status")} for item in items]
