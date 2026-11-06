"""Contract fixture only: an independent stdio MCP peer, backed by test SQLite.

This is NOT a Learning Engine implementation. It scripts a fictional Via job
and intentionally exposes privileged tools/resources to verify host isolation.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = sqlite3.connect(sys.argv[1])
DB.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
DB.execute("CREATE TABLE IF NOT EXISTS audit (name TEXT, arguments TEXT)")
CONTROL = Path(sys.argv[2])


def get(key, default=None):
    row = DB.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(key, value):
    DB.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))


FIELDS = {
    "le_start_work": {"work_id": "string", "conversation_id": "string", "goal": "string"},
    "le_pause_work": {"work_id": "string", "checkpoint": "object"},
    "le_evaluate": {"work_id": "string", "target": "string", "facts": "object"},
    "le_record_answer": {"work_id": "string", "question_id": "string", "answer": "string"},
    "le_confirm_answer": {"work_id": "string", "answer_id": "string"},
    "le_resume_work": {"work_id": "string", "checkpoint": "object"},
    "le_record_progress": {"work_id": "string", "unit_id": "string", "action": "string", "reason": "string",
                           "result": "string", "evidence": "array", "state": "string", "tool_results": "object"},
    "le_finish_work": {"work_id": "string", "completion": "object"},
    "le_reflect": {"work_id": "string"},
    "le_review_candidate": {"work_id": "string", "candidate_id": "string", "revision": "string",
                            "action": "string", "text": "string"},
    "le_get_resume_context": {"work_id": "string"},
    "le_get_approved_knowledge": {"work_id": "string", "query": "string"},
    "le_list_work": {},
    "le_get_review": {"work_id": "string"},
}
READS = {"le_get_resume_context", "le_get_approved_knowledge", "le_list_work", "le_get_review"}


def specs():
    result = []
    for name, fields in FIELDS.items():
        props = {key: {"type": kind} for key, kind in fields.items()}
        if name not in READS:
            props["request_id"] = {"type": "string"}
        result.append({"name": name, "description": name,
                       "inputSchema": {"type": "object", "properties": props,
                                       "required": list(props), "additionalProperties": False}})
    return result


def call(name, a, control):
    wid = a.get("work_id", "")
    key = "work:" + wid
    work = get(key)
    if name == "le_start_work":
        if not work:
            work = {"work_id": wid, "goal": a["goal"], "state": "READY", "confirmed": False, "units": []}
            put(key, work)
        return {"work_id": wid}
    if name == "le_get_approved_knowledge":
        knowledge = get("knowledge", [])
        if control.get("unapproved"):
            knowledge += [{"id": "draft", "text": "DO NOT TRUST DRAFT", "status": "pending"}]
        return {"knowledge": knowledge}
    if name == "le_list_work":
        return {"work_sessions": [json.loads(r[0]) for r in DB.execute("SELECT value FROM kv WHERE key LIKE 'work:%'")]}
    if work is None:
        raise ValueError("Unknown work")
    if name == "le_evaluate":
        if control.get("decision"):
            return control["decision"]
        needs_expert = a["target"] == "model_applicability" or a["target"] == "before_tool:mock_calculate"
        if needs_expert and not work["confirmed"] and not get("knowledge", []):
            q = {"id": "sidewall", "text": "側壁形状は既存モデルと同じですか？専門家の判断が必要です。"}
            work["state"] = "NEED_INPUT"
            work["questions"] = [q]
            put(key, work)
            return {"state": "NEED_INPUT", "questions": [q, q], "evidence": ["via-spec"]}
        work["state"] = "READY"
        put(key, work)
        return {"state": "READY", "evidence": [k["id"] for k in get("knowledge", [])]}
    if name == "le_record_answer":
        answer_id = a["request_id"]
        put("answer:" + answer_id, {"raw": a["answer"], "question_id": a["question_id"]})
        return {"answer_id": answer_id, "interpretation": "側壁がテーパー形状のため、断面を比較し再検証して計算する。"}
    if name == "le_confirm_answer":
        if not get("answer:" + a["answer_id"]):
            raise ValueError("Unknown answer")
        work["confirmed"] = True
        work["state"] = "READY_TO_RESUME"
        work["questions"] = []
    elif name == "le_get_resume_context":
        return {"state": work["state"], "goal": work["goal"], "questions": work.get("questions", []),
                "context": {"next_action": "断面比較後に再計算", "confirmed_answers": ["側壁形状を再検証"] if work["confirmed"] else []}}
    elif name == "le_pause_work":
        work["state"] = "READY_TO_RESUME"
    elif name == "le_resume_work":
        work["state"] = "READY"
    elif name == "le_record_progress":
        work["units"].append({k: v for k, v in a.items() if k not in {"request_id", "work_id"}})
    elif name == "le_finish_work":
        work["state"] = "FINISHED"
    elif name == "le_reflect":
        if work["state"] != "FINISHED":
            raise ValueError("Not finished")
        put("review:" + wid, [
            {"id": wid + ":criterion", "revision": "1", "kind": "criterion", "text": "側壁形状が異なる場合はモデルの再検証が必要", "status": "pending"},
            {"id": wid + ":procedure", "revision": "1", "kind": "procedure", "text": "断面比較 → 適用性判断 → 必要に応じて再計算", "status": "pending"},
            {"id": wid + ":unresolved", "revision": "1", "kind": "unresolved", "text": "形状差の数値的な許容範囲", "status": "pending"},
        ])
    elif name == "le_get_review":
        return {"candidates": get("review:" + wid, [])}
    elif name == "le_review_candidate":
        candidates = get("review:" + wid, [])
        candidate = next(c for c in candidates if c["id"] == a["candidate_id"])
        if candidate["revision"] != a["revision"]:
            raise ValueError("Stale revision")
        action = a["action"]
        candidate["status"] = {"approve": "approved", "defer": "deferred", "reject": "rejected", "edit": "pending"}[action]
        if action == "edit":
            candidate["text"] = a["text"]
        candidate["revision"] = str(int(candidate["revision"]) + 1)
        put("review:" + wid, candidates)
        if action == "approve":
            put("knowledge", get("knowledge", []) + [{k: candidate[k] for k in ("id", "text", "status")}])
    else:
        raise ValueError("Unknown tool")
    put(key, work)
    return {"ok": True}


def handle(message):
    method = message.get("method")
    if "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": message["id"]}
    if method == "initialize":
        data = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "learning-contract-fixture", "version": "1"},
                "capabilities": {"tools": {}, "resources": {}}, "instructions": "UNAPPROVED SERVER INSTRUCTIONS"}
    elif method == "tools/list":
        data = {"tools": specs()}
    elif method == "tools/call":
        control = json.loads(CONTROL.read_text()) if CONTROL.exists() else {}
        name = message["params"]["name"]
        args = message["params"]["arguments"]
        DB.execute("INSERT INTO audit VALUES (?, ?)", (name, json.dumps(args, ensure_ascii=False)))
        DB.commit()
        if control.get("fail") == name:
            response["result"] = {"isError": True, "content": [{"type": "text", "text": "injected failure"}]}
            return response
        request_id = args.get("request_id")
        cached = get("request:" + request_id) if request_id else None
        if cached is not None:
            result = cached
        else:
            try:
                result = call(name, args, control)
            except Exception as exc:
                DB.rollback()
                response["result"] = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
                return response
            if request_id:
                put("request:" + request_id, result)
            DB.commit()
        if control.get("drop_reply") == name:
            sys.exit(0)
        if control.get("malformed") == name:
            data = {"content": [{"type": "text", "text": "not JSON"}]}
        else:
            data = {"structuredContent": result}
    else:
        response["error"] = {"code": -32601, "message": "unsupported"}
        return response
    response["result"] = data
    return response


if __name__ == "__main__":
    for line in sys.stdin:
        reply = handle(json.loads(line))
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)
