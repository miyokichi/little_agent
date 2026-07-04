"""Local web viewer for little_agent workflows (data/workflows.json).

Run with: python -m little_agent.viewer [--workspace PATH] [--port N] [--open]
The workflow skill's open_workflow_viewer tool starts this module detached.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import webbrowser
from contextlib import contextmanager, suppress
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

APP_NAME = "little-agent-viewer"
FINISHED_STATUSES = {"done", "skipped"}
MAX_BODY_BYTES = 64 * 1024
DEFAULT_PORT = 8765


def load_state(workspace: Path) -> dict[str, Any]:
    """State served to the browser: workflows.json plus derived 'ready' flags."""
    path = _workflows_path(workspace)
    mtime = path.stat().st_mtime_ns if path.exists() else None
    data = _load(workspace)
    workflows = []
    for workflow in data.get("workflows", []):
        if not isinstance(workflow, dict):
            continue
        ready = _ready_ids(workflow)
        entry = dict(workflow)
        entry["tasks"] = [dict(task, ready=task.get("id") in ready) for task in workflow.get("tasks") or []]
        workflows.append(entry)
    return {"app": APP_NAME, "version": 1, "mtime": mtime, "workflows": workflows}


def complete_human_task(workspace: Path, workflow_id: str, task_id: str) -> tuple[bool, str]:
    """Strict completion used by the browser: human + pending + ready only."""
    with _locked(workspace):
        data = _load(workspace)
        workflow = next(
            (wf for wf in data.get("workflows", []) if isinstance(wf, dict) and wf.get("id") == workflow_id),
            None,
        )
        if workflow is None:
            return False, f"ワークフローが見つかりません: {workflow_id}"
        task = next((t for t in workflow.get("tasks") or [] if t.get("id") == task_id), None)
        if task is None:
            return False, f"タスクが見つかりません: {task_id}"
        if task.get("assignee") != "human":
            return False, "ブラウザから完了できるのは人間タスクのみです。"
        if task.get("status") != "pending":
            return False, f"このタスクは pending ではありません(現在: {task.get('status')})。"
        if task_id not in _ready_ids(workflow):
            return False, "依存タスクがまだ完了していません。"
        task["status"] = "done"
        task["completed_at"] = now()
        task["completed_via"] = "viewer"
        _refresh_workflow_status(workflow)
        _save(workspace, data)
    return True, "ok"


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "LittleAgentViewer/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def workspace(self) -> Path:
        return self.server.workspace  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature.
        pass

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._send_json(403, {"ok": False, "error": "forbidden host"})
            return
        route = self.path.split("?", 1)[0].split("#", 1)[0]
        if route == "/":
            self._send_bytes(200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
        elif route == "/api/state":
            try:
                self._send_json(200, load_state(self.workspace))
            except Exception as exc:  # noqa: BLE001 - reported to the browser.
                self._send_json(500, {"ok": False, "error": str(exc)})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self._host_allowed():
            self._send_json(403, {"ok": False, "error": "forbidden host"})
            return
        route = self.path.split("?", 1)[0].split("#", 1)[0]
        if route != "/api/complete":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"ok": False, "error": "invalid body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        workflow_id = str(payload.get("workflow_id") or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        if not workflow_id or not task_id:
            self._send_json(400, {"ok": False, "error": "workflow_id and task_id are required"})
            return
        try:
            ok, message = complete_human_task(self.workspace, workflow_id, task_id)
        except Exception as exc:  # noqa: BLE001 - reported to the browser.
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if ok:
            self._send_json(200, {"ok": True})
        else:
            self._send_json(409, {"ok": False, "error": message})

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host.startswith("127.0.0.1") or host.startswith("localhost")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass


def make_server(workspace: Path, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)
    server.workspace = workspace.resolve()  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="little_agent.viewer", description="Workflow viewer for little_agent.")
    parser.add_argument("--workspace", default=os.getenv("LITTLE_AGENT_WORKSPACE", "."), help="Workspace directory.")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("LITTLE_AGENT_VIEWER_PORT", str(DEFAULT_PORT))),
        help="Port to listen on (127.0.0.1 only).",
    )
    parser.add_argument("--open", action="store_true", help="Open the browser after start.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    server = make_server(workspace, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"[viewer] workspace: {workspace}")
    print(f"[viewer] url: {url} (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# NOTE: keep the storage helpers below in sync with skills/workflow/scripts/workflow_tool.py.
# The duplication is intentional: skill scripts must not import little_agent so that
# skill folders stay copy-portable.
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


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


INDEX_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>little_agent Workflow</title>
<style>
:root { --bg:#f8fafc; --card:#ffffff; --line:#e2e8f0; --text:#0f172a; --muted:#64748b; }
* { box-sizing:border-box; }
body { margin:0; font-family:"Segoe UI","Yu Gothic UI",system-ui,sans-serif; background:var(--bg); color:var(--text); }
header { display:flex; align-items:center; gap:12px; padding:10px 16px; background:var(--card);
         border-bottom:1px solid var(--line); position:sticky; top:0; flex-wrap:wrap; z-index:10; }
header h1 { font-size:16px; margin:0; }
select { padding:6px 8px; border:1px solid var(--line); border-radius:6px; font-size:14px; max-width:320px; }
#progress { color:var(--muted); font-size:13px; }
#conn { width:10px; height:10px; border-radius:50%; background:#22c55e; margin-left:auto; }
#conn.bad { background:#ef4444; }
main { display:grid; grid-template-columns:1fr 320px; gap:12px; padding:12px 16px; align-items:start; }
@media (max-width:900px) { main { grid-template-columns:1fr; } }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }
.card h2 { font-size:13px; margin:0 0 8px; color:var(--muted); font-weight:600; }
#diagram { overflow:auto; min-height:180px; }
#diagram svg { max-width:100%; height:auto; }
#diagram pre { font-size:12px; overflow:auto; }
.banner { background:#fef3c7; border:1px solid #f59e0b; color:#78350f; padding:6px 10px;
          border-radius:6px; font-size:12px; margin-bottom:8px; }
ul.flat { list-style:none; margin:0; padding:0; }
ul.flat li { padding:6px 8px; border-bottom:1px solid var(--line); font-size:13px; }
aside { display:flex; flex-direction:column; gap:12px; }
.waiting-item { border:1px solid #f59e0b; background:#fffbeb; border-radius:8px; padding:8px 10px; margin-bottom:8px; }
.waiting-item .t { font-size:14px; font-weight:600; }
.waiting-item .d { font-size:12px; color:var(--muted); margin:4px 0; white-space:pre-wrap; }
button.done { background:#f59e0b; color:#fff; border:none; border-radius:6px; padding:6px 10px;
              font-size:13px; cursor:pointer; margin-top:4px; }
button.done:hover { background:#d97706; }
#detail { font-size:13px; }
#detail dt { color:var(--muted); margin-top:8px; font-size:12px; }
#detail dd { margin:2px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; }
#table-card { margin:0 16px 16px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; font-size:12px; }
tbody tr { cursor:pointer; }
tbody tr:hover { background:#f1f5f9; }
tbody tr.sel { background:#e0f2fe; }
.chip { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; border:1px solid transparent; }
.chip.pending { background:#f1f5f9; color:#334155; border-color:#94a3b8; }
.chip.running { background:#dbeafe; color:#1e3a8a; border-color:#3b82f6; }
.chip.done { background:#dcfce7; color:#14532d; border-color:#22c55e; }
.chip.failed { background:#fee2e2; color:#7f1d1d; border-color:#ef4444; }
.chip.skipped { background:#e2e8f0; color:#64748b; border-color:#94a3b8; }
.chip.ready { background:#fef3c7; color:#78350f; border-color:#f59e0b; font-weight:600; }
#toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#0f172a; color:#fff;
         padding:8px 14px; border-radius:8px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none; }
#toast.show { opacity:0.95; }
#empty { padding:32px; color:var(--muted); text-align:center; }
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js" onerror="window.__cdnFailed=true"></script>
</head>
<body>
<header>
  <h1>🧭 little_agent Workflow</h1>
  <select id="wf-select" title="ワークフロー選択"></select>
  <span id="progress"></span>
  <span id="conn" title="接続状態"></span>
</header>
<div id="empty" hidden>ワークフローはまだありません。エージェントに「〜のワークフローを作って」と頼んでください。</div>
<main id="main">
  <section class="card" id="diagram-card">
    <h2>ワークフロー図(🤖 AI / 👤 人間)</h2>
    <div id="diagram"></div>
  </section>
  <aside>
    <section class="card">
      <h2>あなた待ちのタスク</h2>
      <div id="waiting"></div>
    </section>
    <section class="card">
      <h2>タスク詳細</h2>
      <div id="detail">タスクをクリックすると詳細を表示します。</div>
    </section>
  </aside>
</main>
<section class="card" id="table-card">
  <h2>全タスク</h2>
  <table>
    <thead><tr><th>ID</th><th>担当</th><th>状態</th><th>タイトル</th><th>依存</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
</section>
<div id="toast"></div>
<script>
const POLL_MS = 1500;
let lastMtime;
let state = null;
let currentWfId = (location.hash.match(/wf=([0-9a-fA-F]+)/) || [])[1] || null;
let selectedTaskId = null;
let optionsKey = '';
let renderSeq = 0;
let toastTimer;
const $ = (id) => document.getElementById(id);

if (window.mermaid) {
  window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral' });
}

function connOk(ok) { $('conn').classList.toggle('bad', !ok); }
function isFinished(t) { return t.status === 'done' || t.status === 'skipped'; }

async function tick(force) {
  let data;
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    if (!res.ok) throw new Error('bad status ' + res.status);
    data = await res.json();
  } catch (err) {
    connOk(false);
    return;
  }
  connOk(true);
  const changed = force || !state || data.mtime !== lastMtime;
  lastMtime = data.mtime;
  if (!changed) return;
  state = data;
  render();
}

function currentWf() {
  const wfs = (state && state.workflows) || [];
  return wfs.find(w => w.id === currentWfId)
    || wfs.find(w => w.status === 'active')
    || wfs[wfs.length - 1]
    || null;
}

function render() {
  const wfs = state.workflows || [];
  const hasAny = wfs.length > 0;
  $('empty').hidden = hasAny;
  $('main').style.display = hasAny ? '' : 'none';
  $('table-card').style.display = hasAny ? '' : 'none';
  if (!hasAny) { $('progress').textContent = ''; return; }

  const wf = currentWf();
  currentWfId = wf.id;

  const key = JSON.stringify(wfs.map(w => [w.id, w.title, w.status]));
  if (key !== optionsKey) {
    optionsKey = key;
    const sel = $('wf-select');
    sel.innerHTML = '';
    for (const w of wfs) {
      const opt = document.createElement('option');
      opt.value = w.id;
      opt.textContent = (w.status === 'done' ? '✅ ' : '') + w.title;
      sel.appendChild(opt);
    }
  }
  $('wf-select').value = wf.id;

  const doneCount = wf.tasks.filter(isFinished).length;
  $('progress').textContent = '完了 ' + doneCount + '/' + wf.tasks.length;

  renderWaiting(wf);
  renderTable(wf);
  renderDetail(wf);
  renderDiagram(wf);
}

function renderWaiting(wf) {
  const box = $('waiting');
  box.innerHTML = '';
  const ready = wf.tasks.filter(t => t.ready && t.assignee === 'human');
  if (!ready.length) {
    box.textContent = 'いまあなた待ちのタスクはありません。';
    return;
  }
  for (const t of ready) {
    const card = document.createElement('div');
    card.className = 'waiting-item';
    const title = document.createElement('div');
    title.className = 't';
    title.textContent = '👤 ' + t.title;
    card.appendChild(title);
    if (t.description) {
      const desc = document.createElement('div');
      desc.className = 'd';
      desc.textContent = t.description;
      card.appendChild(desc);
    }
    const btn = document.createElement('button');
    btn.className = 'done';
    btn.textContent = '完了にする';
    btn.addEventListener('click', () => completeTask(wf.id, t.id));
    card.appendChild(btn);
    box.appendChild(card);
  }
}

async function completeTask(wfId, taskId) {
  try {
    const res = await fetch('/api/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workflow_id: wfId, task_id: taskId })
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) toast('完了にしました');
    else toast(data.error || '完了できませんでした');
  } catch (err) {
    toast('通信エラーが発生しました');
  }
  tick(true);
}

function statusChip(t) {
  const isWait = t.ready && t.assignee === 'human';
  const span = document.createElement('span');
  span.className = 'chip ' + (isWait ? 'ready' : t.status);
  span.textContent = isWait ? 'あなた待ち' : t.status;
  return span;
}

function renderTable(wf) {
  const tbody = $('rows');
  tbody.innerHTML = '';
  const byId = {};
  for (const t of wf.tasks) byId[t.id] = t;
  for (const t of wf.tasks) {
    const tr = document.createElement('tr');
    tr.dataset.id = t.id;
    if (t.id === selectedTaskId) tr.className = 'sel';
    const cells = [
      t.id,
      t.assignee === 'human' ? '👤 人間' : '🤖 AI',
      null,
      t.title,
      (t.depends_on || []).map(d => (byId[d] || { title: d }).title).join(', ') || '-'
    ];
    cells.forEach((value, i) => {
      const td = document.createElement('td');
      if (i === 2) td.appendChild(statusChip(t));
      else td.textContent = value;
      tr.appendChild(td);
    });
    tr.addEventListener('click', () => selectTask(t.id));
    tbody.appendChild(tr);
  }
}

function selectTask(id) {
  selectedTaskId = id;
  document.querySelectorAll('#rows tr').forEach(tr => {
    tr.classList.toggle('sel', tr.dataset.id === id);
  });
  renderDetail(currentWf());
}

function renderDetail(wf) {
  const box = $('detail');
  const t = wf && wf.tasks.find(x => x.id === selectedTaskId);
  if (!t) {
    box.textContent = 'タスクをクリックすると詳細を表示します。';
    return;
  }
  const byId = {};
  for (const x of wf.tasks) byId[x.id] = x;
  box.innerHTML = '';
  const dl = document.createElement('dl');
  dl.style.margin = '0';
  const add = (label, value) => {
    if (!value) return;
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = value;
    dl.appendChild(dt);
    dl.appendChild(dd);
  };
  add('タイトル', t.title);
  add('担当 / 状態', (t.assignee === 'human' ? '👤 人間' : '🤖 AI') + ' / ' + t.status + (t.ready ? ' (着手可能)' : ''));
  add('説明', t.description);
  add('依存', (t.depends_on || []).map(d => (byId[d] || { title: d }).title).join(', '));
  add('結果', t.result);
  add('作成', t.created_at);
  add('開始', t.started_at);
  add('完了', t.completed_at
    ? t.completed_at + (t.completed_via ? (t.completed_via === 'viewer' ? ' (ブラウザから)' : ' (エージェント)') : '')
    : '');
  box.appendChild(dl);
}

function mermaidLabel(text) {
  let s = String(text || '').replace(/#/g, '#35;').replace(/"/g, '#quot;').replace(/\r?\n/g, ' ');
  if (s.length > 40) s = s.slice(0, 39) + '…';
  return s;
}

function mermaidText(wf) {
  const lines = ['flowchart TD'];
  for (const t of wf.tasks) {
    const label = '"' + (t.assignee === 'human' ? '👤 ' : '🤖 ') + mermaidLabel(t.title) + '"';
    const cls = (t.ready && t.assignee === 'human') ? 'ready' : t.status;
    const node = t.assignee === 'human'
      ? 't_' + t.id + '[/' + label + '/]'
      : 't_' + t.id + '[' + label + ']';
    lines.push('  ' + node + ':::' + cls);
  }
  for (const t of wf.tasks) {
    for (const d of t.depends_on || []) lines.push('  t_' + d + ' --> t_' + t.id);
  }
  lines.push('  classDef pending fill:#f1f5f9,stroke:#94a3b8,color:#334155;');
  lines.push('  classDef running fill:#dbeafe,stroke:#3b82f6,color:#1e3a8a,stroke-width:2px;');
  lines.push('  classDef done fill:#dcfce7,stroke:#22c55e,color:#14532d;');
  lines.push('  classDef failed fill:#fee2e2,stroke:#ef4444,color:#7f1d1d;');
  lines.push('  classDef skipped fill:#e2e8f0,stroke:#94a3b8,color:#64748b,stroke-dasharray:4 3;');
  lines.push('  classDef ready fill:#fef3c7,stroke:#f59e0b,color:#78350f,stroke-width:3px;');
  return lines.join('\n');
}

async function renderDiagram(wf) {
  const container = $('diagram');
  if (!window.mermaid) {
    renderFallback(wf, container);
    return;
  }
  const text = mermaidText(wf);
  if (container.dataset.src === text) return;
  try {
    const result = await window.mermaid.render('wfgraph' + (renderSeq++), text);
    container.innerHTML = result.svg;
  } catch (err) {
    container.innerHTML = '';
    const pre = document.createElement('pre');
    pre.textContent = text;
    container.appendChild(pre);
  }
  container.dataset.src = text;
}

function renderFallback(wf, container) {
  container.innerHTML = '';
  const banner = document.createElement('div');
  banner.className = 'banner';
  banner.textContent = 'オフライン表示: Mermaid (CDN) を読み込めないため、図の代わりに一覧を表示しています。';
  container.appendChild(banner);
  const byId = {};
  for (const t of wf.tasks) byId[t.id] = t;
  const ul = document.createElement('ul');
  ul.className = 'flat';
  for (const t of wf.tasks) {
    const li = document.createElement('li');
    const deps = (t.depends_on || []).map(d => (byId[d] || { title: d }).title).join(', ');
    li.textContent = '[' + t.status + '] ' + (t.assignee === 'human' ? '👤' : '🤖') + ' ' + t.title
      + (deps ? ' ← 依存: ' + deps : '');
    ul.appendChild(li);
  }
  container.appendChild(ul);
}

function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}

$('wf-select').addEventListener('change', (event) => {
  currentWfId = event.target.value;
  selectedTaskId = null;
  history.replaceState(null, '', '#wf=' + currentWfId);
  render();
});

tick(true);
setInterval(() => tick(false), POLL_MS);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
