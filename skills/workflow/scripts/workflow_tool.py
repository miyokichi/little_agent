from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

STATUSES = ("pending", "running", "done", "failed", "skipped")
ASSIGNEES = ("ai", "human")
FINISHED_STATUSES = {"done", "skipped"}
RESULT_MAX_CHARS = 2000
DEFAULT_VIEWER_PORT = 8765


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "create_workflow":
            result = create_workflow(workspace, arguments)
        elif tool == "add_workflow_task":
            result = add_workflow_task(workspace, arguments)
        elif tool == "update_task_status":
            result = update_task_status(workspace, arguments)
        elif tool == "show_workflow":
            result = show_workflow(workspace, arguments)
        elif tool == "list_workflows":
            result = list_workflows(workspace, arguments)
        elif tool == "delete_workflow":
            result = delete_workflow(workspace, arguments)
        elif tool == "open_workflow_viewer":
            result = open_workflow_viewer(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown workflow tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Workflow script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def create_workflow(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()
    if not title:
        return {"ok": False, "content": "Workflow title is required."}
    goal = str(arguments.get("goal") or "").strip()
    items = arguments.get("tasks")
    if not isinstance(items, list) or not items:
        return {"ok": False, "content": "tasks must be a non-empty array."}

    error = _validate_new_tasks(items)
    if error:
        return {"ok": False, "content": error}
    keys = [str(item["key"]).strip() for item in items]
    edges = [
        (str(dep).strip(), str(item["key"]).strip())
        for item in items
        for dep in item.get("depends_on") or []
    ]
    cycle = _detect_cycle(keys, edges)
    if cycle:
        return {"ok": False, "content": f"Dependency cycle detected among tasks: {', '.join(cycle)}"}

    key_to_id = {key: _new_id() for key in keys}
    tasks = [
        _new_task(
            title=str(item["title"]).strip(),
            description=str(item.get("description") or "").strip(),
            assignee=str(item["assignee"]).strip(),
            depends_on=[key_to_id[str(dep).strip()] for dep in item.get("depends_on") or []],
            task_id=key_to_id[str(item["key"]).strip()],
        )
        for item in items
    ]
    timestamp = now()
    workflow = {
        "id": _new_id(),
        "title": title,
        "goal": goal,
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "tasks": tasks,
    }
    with _locked(workspace):
        data = _load(workspace)
        data["workflows"].append(workflow)
        _save(workspace, data)
    mapping = ", ".join(f"{key}={key_to_id[key]}" for key in keys)
    return {
        "ok": True,
        "content": f"Created workflow {workflow['id']}.\nTask ids: {mapping}\n{_format_workflow(workflow)}",
    }


def add_workflow_task(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()
    if not title:
        return {"ok": False, "content": "Task title is required."}
    assignee = str(arguments.get("assignee") or "").strip()
    if assignee not in ASSIGNEES:
        return {"ok": False, "content": f"Invalid assignee '{assignee}'. Use one of: ai, human."}
    raw_depends = arguments.get("depends_on") or []
    if not isinstance(raw_depends, list):
        return {"ok": False, "content": "depends_on must be an array of task ids."}
    depends_on = [str(dep).strip() for dep in raw_depends if str(dep).strip()]

    with _locked(workspace):
        data = _load(workspace)
        workflow, error = _find_workflow(data, arguments.get("workflow_id"))
        if workflow is None:
            return {"ok": False, "content": error}
        known = {task.get("id") for task in workflow["tasks"]}
        unknown = [dep for dep in depends_on if dep not in known]
        if unknown:
            return {
                "ok": False,
                "content": f"Unknown depends_on task ids: {', '.join(unknown)}. Check ids with show_workflow.",
            }
        task = _new_task(
            title=title,
            description=str(arguments.get("description") or "").strip(),
            assignee=assignee,
            depends_on=depends_on,
        )
        workflow["tasks"].append(task)
        _refresh_workflow_status(workflow)
        _save(workspace, data)
        content = f"Added task to workflow {workflow['id']}:\n{_format_task(task, _ready_ids(workflow))}"
    return {"ok": True, "content": content}


def update_task_status(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id:
        return {"ok": False, "content": "task_id is required."}
    status = str(arguments.get("status") or "").strip()
    if status not in STATUSES:
        return {"ok": False, "content": f"Invalid status '{status}'. Use one of: {', '.join(STATUSES)}."}
    result_text = str(arguments.get("result") or "").strip()[:RESULT_MAX_CHARS]

    with _locked(workspace):
        data = _load(workspace)
        workflow, task, error = _locate_task(data, arguments.get("workflow_id"), task_id)
        if workflow is None or task is None:
            return {"ok": False, "content": error}

        warnings: list[str] = []
        if status == "done":
            finished = {t["id"] for t in workflow["tasks"] if t.get("status") in FINISHED_STATUSES}
            unmet = [dep for dep in task.get("depends_on") or [] if dep not in finished]
            if unmet:
                warnings.append(f"Note: marked done while dependencies are unfinished: {', '.join(unmet)}")

        task["status"] = status
        if status == "running" and not task.get("started_at"):
            task["started_at"] = now()
        if status in ("pending", "running"):
            task["completed_at"] = None
            task["completed_via"] = None
        if status in ("done", "failed", "skipped"):
            task["completed_at"] = now()
            task["completed_via"] = "agent"
        if result_text:
            task["result"] = result_text
        _refresh_workflow_status(workflow)
        _save(workspace, data)

        ready = _ready_ids(workflow)
        ready_line = ", ".join(f"{t['id']}({t.get('assignee')})" for t in workflow["tasks"] if t["id"] in ready) or "-"
        content = "\n".join([_format_task(task, ready), *warnings, f"READY: {ready_line}"])
    return {"ok": True, "content": content}


def show_workflow(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    data = _load(workspace)
    workflow, error = _find_workflow(data, arguments.get("workflow_id"))
    if workflow is None:
        return {"ok": False, "content": error}
    return {"ok": True, "content": _format_workflow(workflow)}


def list_workflows(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    wanted = str(arguments.get("status") or "active")
    data = _load(workspace)
    workflows = [wf for wf in data["workflows"] if isinstance(wf, dict)]
    if wanted != "all":
        workflows = [wf for wf in workflows if wf.get("status") == wanted]
    if not workflows:
        return {"ok": True, "content": "(no workflows)"}
    lines = []
    for workflow in workflows:
        tasks = workflow.get("tasks") or []
        done_count = sum(1 for task in tasks if task.get("status") in FINISHED_STATUSES)
        lines.append(
            f"{workflow.get('id')} [{workflow.get('status')}] {workflow.get('title')} "
            f"(done {done_count}/{len(tasks)}, ready {len(_ready_ids(workflow))})"
        )
    return {"ok": True, "content": "\n".join(lines)}


def delete_workflow(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if not workflow_id:
        return {"ok": False, "content": "workflow_id is required."}
    with _locked(workspace):
        data = _load(workspace)
        remaining = [wf for wf in data["workflows"] if wf.get("id") != workflow_id]
        if len(remaining) == len(data["workflows"]):
            return {"ok": False, "content": f"Workflow not found: {workflow_id}"}
        data["workflows"] = remaining
        _save(workspace, data)
    return {"ok": True, "content": f"Deleted workflow: {workflow_id}"}


def open_workflow_viewer(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    raw_port = arguments.get("port") or os.environ.get("LITTLE_AGENT_VIEWER_PORT") or DEFAULT_VIEWER_PORT
    port = int(raw_port)
    url = f"http://127.0.0.1:{port}/"
    workflow_id = str(arguments.get("workflow_id") or "").strip()
    fragment = f"#wf={workflow_id}" if workflow_id else ""

    state = _viewer_state(port)
    if state == "other":
        return {
            "ok": False,
            "content": f"Port {port} is used by another app. Set LITTLE_AGENT_VIEWER_PORT or pass another port.",
        }
    if state == "down":
        # DEVNULL is required: the parent ScriptSkillTool waits on this script with
        # capture_output=True, so an inherited pipe would block it until the viewer exits.
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, "-m", "little_agent.viewer", "--workspace", str(workspace), "--port", str(port)],
            **popen_kwargs,
        )
        for _attempt in range(6):
            time.sleep(0.5)
            if _viewer_state(port) == "ours":
                break
        else:
            return {
                "ok": False,
                "content": (
                    f"Viewer did not start on port {port}. "
                    f"Try manually: python -m little_agent.viewer --workspace \"{workspace}\""
                ),
            }
    webbrowser.open(url + fragment)
    already = " (already running)" if state == "ours" else ""
    return {"ok": True, "content": f"Workflow viewer{already}: {url}{fragment} - opened in the browser."}


def _viewer_state(port: int) -> str:
    """Return 'ours' | 'other' | 'down' for what is listening on the port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        return "other"
    except OSError:
        return "down"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "other"
    if isinstance(payload, dict) and payload.get("app") == "little-agent-viewer":
        return "ours"
    return "other"


def _validate_new_tasks(items: list[Any]) -> str | None:
    keys: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return f"tasks[{index}] must be an object."
        key = str(item.get("key") or "").strip()
        if not key:
            return f"tasks[{index}] is missing 'key'."
        if key in keys:
            return f"Duplicate task key: {key}"
        keys.add(key)
        if not str(item.get("title") or "").strip():
            return f"Task '{key}' is missing 'title'."
        assignee = str(item.get("assignee") or "").strip()
        if assignee not in ASSIGNEES:
            return f"Task '{key}' has invalid assignee '{assignee}'. Use one of: ai, human."
        depends_on = item.get("depends_on") or []
        if not isinstance(depends_on, list):
            return f"Task '{key}' depends_on must be an array of task keys."
    for item in items:
        key = str(item.get("key")).strip()
        for dep in item.get("depends_on") or []:
            dep_key = str(dep).strip()
            if dep_key == key:
                return f"Task '{key}' depends on itself."
            if dep_key not in keys:
                return f"Task '{key}' depends on unknown key '{dep_key}'."
    return None


def _detect_cycle(nodes: list[str], edges: list[tuple[str, str]]) -> list[str] | None:
    """Kahn's algorithm. Returns the node keys stuck in a cycle, or None."""
    indegree = {node: 0 for node in nodes}
    downstream: dict[str, list[str]] = {node: [] for node in nodes}
    for prerequisite, dependent in edges:
        indegree[dependent] += 1
        downstream[prerequisite].append(dependent)
    queue = [node for node in nodes if indegree[node] == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for nxt in downstream[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen == len(nodes):
        return None
    return [node for node in nodes if indegree[node] > 0]


def _new_task(
    title: str,
    description: str,
    assignee: str,
    depends_on: list[str],
    task_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id or _new_id(),
        "title": title,
        "description": description,
        "assignee": assignee,
        "status": "pending",
        "depends_on": depends_on,
        "result": "",
        "created_at": now(),
        "started_at": None,
        "completed_at": None,
        "completed_via": None,
    }


def _find_workflow(data: dict[str, Any], workflow_id: Any) -> tuple[dict[str, Any] | None, str]:
    workflows = [wf for wf in data["workflows"] if isinstance(wf, dict)]
    wanted = str(workflow_id or "").strip()
    if wanted:
        for workflow in workflows:
            if workflow.get("id") == wanted:
                return workflow, ""
        return None, f"Workflow not found: {wanted}"
    active = [wf for wf in workflows if wf.get("status") == "active"]
    if not active:
        return None, "No active workflow. Pass workflow_id or create one with create_workflow."
    if len(active) > 1:
        listing = "; ".join(f"{wf.get('id')}: {wf.get('title')}" for wf in active)
        return None, f"Multiple active workflows. Pass workflow_id. Candidates: {listing}"
    return active[0], ""


def _locate_task(
    data: dict[str, Any], workflow_id: Any, task_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    if str(workflow_id or "").strip():
        workflow, error = _find_workflow(data, workflow_id)
        if workflow is None:
            return None, None, error
        for task in workflow.get("tasks") or []:
            if task.get("id") == task_id:
                return workflow, task, ""
        return None, None, f"Task {task_id} not found in workflow {workflow.get('id')}."
    matches = [
        (workflow, task)
        for workflow in data["workflows"]
        if isinstance(workflow, dict)
        for task in workflow.get("tasks") or []
        if task.get("id") == task_id
    ]
    if not matches:
        return None, None, f"Task not found: {task_id}"
    if len(matches) > 1:
        ids = ", ".join(workflow.get("id", "?") for workflow, _task in matches)
        return None, None, f"Task id {task_id} exists in multiple workflows ({ids}). Pass workflow_id."
    return matches[0][0], matches[0][1], ""


def _ready_ids(workflow: dict[str, Any]) -> set[str]:
    tasks = workflow.get("tasks") or []
    finished = {task["id"] for task in tasks if task.get("status") in FINISHED_STATUSES}
    return {
        task["id"]
        for task in tasks
        if task.get("status") == "pending" and all(dep in finished for dep in task.get("depends_on") or [])
    }


def _refresh_workflow_status(workflow: dict[str, Any]) -> None:
    tasks = workflow.get("tasks") or []
    all_finished = bool(tasks) and all(task.get("status") in FINISHED_STATUSES for task in tasks)
    workflow["status"] = "done" if all_finished else "active"
    workflow["updated_at"] = now()


def _format_workflow(workflow: dict[str, Any]) -> str:
    tasks = workflow.get("tasks") or []
    ready = _ready_ids(workflow)
    done_count = sum(1 for task in tasks if task.get("status") in FINISHED_STATUSES)
    lines = [f"{workflow.get('id')} [{workflow.get('status')}] {workflow.get('title')} (done {done_count}/{len(tasks)})"]
    if workflow.get("goal"):
        lines.append(f"goal: {workflow['goal']}")
    for task in tasks:
        lines.append("  " + _format_task(task, ready))
    ready_line = ", ".join(f"{task['id']}({task.get('assignee')})" for task in tasks if task["id"] in ready) or "-"
    lines.append(f"READY: {ready_line}")
    return "\n".join(lines)


def _format_task(task: dict[str, Any], ready_ids: set[str]) -> str:
    deps = ",".join(task.get("depends_on") or []) or "-"
    parts = [f"{task.get('id')} [{task.get('assignee')}/{task.get('status')}] {task.get('title')} (deps: {deps})"]
    if task.get("result"):
        result = str(task["result"])
        if len(result) > 120:
            result = result[:119] + "…"
        parts.append(f"result: {result}")
    if task.get("id") in ready_ids:
        parts.append("<- READY")
    return " ".join(parts)


# NOTE: keep the storage helpers below in sync with little_agent/viewer.py.
def _workflows_path(workspace: Path) -> Path:
    path = (workspace / "data" / "workflows.json").resolve()
    if workspace not in [path, *path.parents]:
        raise ValueError("Workflow path escaped the workspace.")
    return path


def _load(workspace: Path) -> dict[str, Any]:
    path = _workflows_path(workspace)
    if not path.exists():
        return {"version": 1, "workflows": []}
    data: Any = None
    for attempt in range(2):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            break
        except json.JSONDecodeError:
            if attempt == 1:
                raise
            time.sleep(0.05)
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
        raise ValueError("data/workflows.json must contain an object with a 'workflows' list.")
    return data


def _save(workspace: Path, data: dict[str, Any]) -> None:
    path = _workflows_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(3):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.1)


@contextmanager
def _locked(workspace: Path) -> Iterator[None]:
    """Cross-process mutation lock via an exclusively-created lock file."""
    path = _workflows_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    for _attempt in range(40):
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(handle)
            break
        except FileExistsError:
            with suppress(OSError):
                if time.time() - lock_path.stat().st_mtime > 10:
                    lock_path.unlink()
                    continue
            time.sleep(0.05)
    else:
        raise TimeoutError("Could not acquire data/workflows.json.lock within 2 seconds.")
    try:
        yield
    finally:
        with suppress(OSError):
            lock_path.unlink()


def _new_id() -> str:
    return uuid4().hex[:8]


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
