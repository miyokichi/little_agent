from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "add_task":
            result = add_task(workspace, arguments)
        elif tool == "list_tasks":
            result = list_tasks(workspace, arguments)
        elif tool == "complete_task":
            result = complete_task(workspace, arguments)
        elif tool == "delete_task":
            result = delete_task(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown task tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Task script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def add_task(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    tasks = load_tasks(workspace)
    title = str(arguments.get("title") or "").strip()
    if not title:
        return {"ok": False, "content": "Task title is required."}

    task = {
        "id": uuid4().hex[:8],
        "title": title,
        "status": "open",
        "created_at": now(),
        "due": str(arguments.get("due") or "").strip(),
        "priority": str(arguments.get("priority") or "normal").strip(),
        "notes": str(arguments.get("notes") or "").strip(),
    }
    tasks.append(task)
    save_tasks(workspace, tasks)
    return {"ok": True, "content": format_task(task)}


def list_tasks(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    status = str(arguments.get("status") or "open")
    tasks = load_tasks(workspace)
    if status != "all":
        tasks = [task for task in tasks if task.get("status", "open") == status]
    if not tasks:
        return {"ok": True, "content": "(no tasks)"}
    return {"ok": True, "content": "\n".join(format_task(task) for task in tasks)}


def complete_task(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    tasks = load_tasks(workspace)
    for task in tasks:
        if task.get("id") == task_id:
            task["status"] = "done"
            task["completed_at"] = now()
            save_tasks(workspace, tasks)
            return {"ok": True, "content": format_task(task)}
    return {"ok": False, "content": f"Task not found: {task_id}"}


def delete_task(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    tasks = load_tasks(workspace)
    remaining = [task for task in tasks if task.get("id") != task_id]
    if len(remaining) == len(tasks):
        return {"ok": False, "content": f"Task not found: {task_id}"}
    save_tasks(workspace, remaining)
    return {"ok": True, "content": f"Deleted task: {task_id}"}


def tasks_path(workspace: Path) -> Path:
    path = (workspace / "data" / "tasks.json").resolve()
    if workspace not in [path, *path.parents]:
        raise ValueError("Task path escaped the workspace.")
    return path


def load_tasks(workspace: Path) -> list[dict[str, Any]]:
    path = tasks_path(workspace)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("data/tasks.json must contain a JSON list.")
    return [task for task in data if isinstance(task, dict)]


def save_tasks(workspace: Path, tasks: list[dict[str, Any]]) -> None:
    path = tasks_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def format_task(task: dict[str, Any]) -> str:
    status = "done" if task.get("status") == "done" else "open"
    fields = [f"{task.get('id')}: [{status}] {task.get('title', '')}"]
    if task.get("due"):
        fields.append(f"due={task['due']}")
    if task.get("priority"):
        fields.append(f"priority={task['priority']}")
    if task.get("notes"):
        fields.append(f"notes={task['notes']}")
    return " | ".join(fields)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())

