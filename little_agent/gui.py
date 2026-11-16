"""Small native chat UI; all Agent/Memory/MCP work stays on one worker thread.

Tkinter is imported only when launching the GUI. Terminal and served modes do
not require a display or a Tk installation.
"""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from little_agent.commands import CommandContext
from little_agent.control import StopController


@dataclass
class Confirmation:
    name: str
    arguments: dict[str, Any]
    answered: threading.Event = field(default_factory=threading.Event)
    approved: bool = False

    def answer(self, approved: bool) -> None:
        self.approved = approved
        self.answered.set()


class ChatWorker:
    """Serialize chat/commands and marshal confirmations back to the UI thread."""

    def __init__(self, factory: Callable, stop: StopController, cleanup: Callable[[], None]) -> None:
        self.factory = factory
        self.stop = stop
        self.cleanup = cleanup
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.jobs: queue.Queue[str | None] = queue.Queue()
        self.closing = threading.Event()
        self._pending: list[Confirmation] = []
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="chat-ui", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit(self, text: str) -> None:
        if not self.closing.is_set():
            self.jobs.put(text)

    def confirm(self, name: str, arguments: dict[str, Any]) -> bool:
        request = Confirmation(name, arguments)
        with self._lock:
            if self.closing.is_set():
                return False
            self._pending.append(request)
        self.events.put(("confirm", request))
        try:
            while not request.answered.wait(0.05):
                if self.closing.is_set() or self.stop.triggered:
                    return False
            return request.approved and not self.closing.is_set() and not self.stop.triggered
        finally:
            with self._lock:
                self._pending.remove(request)

    def close(self) -> None:
        self.closing.set()
        self.stop.request_stop()
        with self._lock:
            for request in self._pending:
                request.answer(False)
        self.jobs.put(None)

    def _run(self) -> None:
        ctx = None
        try:
            ctx = self.factory(self.confirm)
            self.events.put(("ready", ctx))
            while not self.closing.is_set():
                text = self.jobs.get()
                if text is None or self.closing.is_set():
                    break
                try:
                    result = ctx.registry.dispatch(ctx, text)
                    outputs = []
                    if result is None:
                        outputs.append(ctx.session.send(text))
                    else:
                        if result.output is not None:
                            outputs.append(result.output)
                        if result.agent_prompt is not None:
                            outputs.append(ctx.session.send(result.agent_prompt))
                    self.events.put(("reply", "\n\n".join(outputs)))
                    if result is not None and result.should_exit:
                        self.events.put(("exit", None))
                        break
                except Exception as exc:  # Keep an API/Tool failure from killing the UI.
                    self.events.put(("error", str(exc)))
                finally:
                    self.events.put(("idle", ctx))
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            try:
                if ctx is not None and ctx.session.memory.enabled:
                    ctx.session.learn()
            except Exception as exc:
                self.events.put(("error", f"Memoryの保存に失敗しました: {exc}"))
            finally:
                try:
                    self.cleanup()
                finally:
                    self.events.put(("closed", None))


class ChatWindow:
    """Tk widgets are created and touched exclusively on the main thread."""

    def __init__(self, root, worker: ChatWorker, profile_names: list[str]) -> None:
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root = root
        self.worker = worker
        self.context: CommandContext | None = None
        self.busy = True
        self.closing = False
        self.finished = False
        self.destroyed = False
        self._pending_text = ""
        root.title("Little Agent — Chat")
        root.geometry("960x720")
        root.minsize(680, 480)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root, padding=(16, 12))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Little Agent", font=("TkDefaultFont", 16, "bold")).grid(row=0, column=0, sticky="w")
        self.agent_name = tk.StringVar(value="default")
        self.agent_box = ttk.Combobox(header, textvariable=self.agent_name, values=profile_names, width=24, state="disabled")
        self.agent_box.grid(row=0, column=2, padx=(12, 0))
        self.agent_box.bind("<<ComboboxSelected>>", self.switch_agent)
        self.details = ttk.Label(header, text="起動中…", wraplength=850)
        self.details.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.transcript = ScrolledText(root, wrap="word", state="disabled", padx=14, pady=12, relief="flat", font="TkTextFont")
        self.transcript.grid(row=1, column=0, sticky="nsew", padx=16)
        self.transcript.tag_configure("user", foreground="#245dc1")
        self.transcript.tag_configure("assistant", foreground="#1e293b")
        self.transcript.tag_configure("system", foreground="#64748b")
        self.transcript.tag_configure("error", foreground="#b91c1c")

        footer = ttk.Frame(root, padding=16)
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(footer)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.add_button = ttk.Button(toolbar, text="フォルダー追加", command=self.add_directory)
        self.add_button.pack(side="left")
        self.read_only = tk.BooleanVar(value=False)
        self.read_only_box = ttk.Checkbutton(toolbar, text="読み取り専用", variable=self.read_only)
        self.read_only_box.pack(side="left", padx=10)
        self.clear_button = ttk.Button(toolbar, text="会話をクリア", command=lambda: self.submit("/clear"))
        self.clear_button.pack(side="right")
        self.input = tk.Text(footer, height=4, wrap="word", undo=True, font="TkTextFont")
        self.input.grid(row=1, column=0, sticky="ew")
        self.input.bind("<Control-Return>", self.send)
        self.input.bind("<Command-Return>", self.send)
        buttons = ttk.Frame(footer)
        buttons.grid(row=1, column=1, sticky="ns", padx=(10, 0))
        self.send_button = ttk.Button(buttons, text="送信", command=self.send)
        self.send_button.pack(fill="x")
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop)
        self.stop_button.pack(fill="x", pady=(8, 0))
        self.status = tk.StringVar(value="起動中…")
        ttk.Label(footer, textvariable=self.status).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._set_busy(True)
        root.after(50, self._poll)
        worker.start()

    def append(self, role: str, text: str) -> None:
        if not text:
            return
        label = {"user": "あなた", "assistant": "Little Agent", "system": "案内", "error": "エラー"}[role]
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{label}\n{text}\n\n", role)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        disabled = busy or self.closing or self.finished
        state = "disabled" if disabled else "normal"
        for widget in (self.send_button, self.add_button, self.clear_button, self.read_only_box):
            widget.configure(state=state)
        self.agent_box.configure(state="disabled" if disabled else "readonly")
        self.stop_button.configure(state="normal" if busy and not self.closing else "disabled")
        self.status.set("終了処理中…" if self.closing else "実行中…" if busy else "Ctrl+Enterで送信 · Enterで改行 · /help でコマンド一覧")

    def send(self, _event=None):
        if not self.busy and not self.closing and not self.finished:
            text = self.input.get("1.0", "end-1c").strip()
            if text:
                self.input.delete("1.0", "end")
                self.submit(text)
        return "break"

    def submit(self, text: str) -> None:
        if self.busy or self.closing or self.finished:
            return
        self.append("user", text)
        self._pending_text = text.strip()
        self._set_busy(True)
        self.worker.submit(text)

    def switch_agent(self, _event=None) -> None:
        self.submit("/agent " + self.agent_name.get())

    def add_directory(self) -> None:
        from tkinter import filedialog
        if self.busy or self.closing:
            return
        path = filedialog.askdirectory(parent=self.root, title="この会話で使うフォルダーを追加", mustexist=True)
        if path:
            flag = "--read-only " if self.read_only.get() else ""
            self.submit('/add_directory ' + flag + '"' + path + '"')

    def stop(self) -> None:
        self.worker.stop.request_stop()
        self.status.set("停止を要求しました。現在の処理が戻るまでお待ちください。")

    def close(self) -> None:
        from tkinter import messagebox
        if self.closing:
            return
        if self.busy and not messagebox.askyesno("終了", "処理中です。停止を要求して終了しますか？", parent=self.root):
            return
        self.closing = True
        self._set_busy(True)
        self.worker.close()

    def _update_details(self, ctx: CommandContext) -> None:
        self.context = ctx
        config = ctx.agent.config
        self.agent_name.set(ctx.active_agent or "default")
        self.details.configure(text=f"Workspace: {config.workspace}\nMemory: {ctx.session.memory.describe()} · 追加: 読取 {len(config.readable_paths)} / 読書 {len(config.writable_paths)}")

    def _poll(self) -> None:
        from tkinter import messagebox
        for _ in range(100):
            try:
                kind, value = self.worker.events.get_nowait()
            except queue.Empty:
                break
            if kind == "ready":
                self._update_details(value)
                self.append("system", "メッセージを入力してください。/add_directory と既存の /help コマンドも使えます。")
                if value.agent.mcp:
                    for warning in value.agent.mcp.warnings():
                        self.append("error", warning)
                self._set_busy(False)
                self.input.focus_set()
            elif kind == "reply":
                if self._pending_text == "/clear":
                    self.transcript.configure(state="normal")
                    self.transcript.delete("1.0", "end")
                    self.transcript.configure(state="disabled")
                self.append("assistant", value)
            elif kind == "error":
                self.append("error", value)
            elif kind == "idle":
                self._update_details(value)
                self._set_busy(False)
            elif kind == "confirm":
                if self.closing or value.answered.is_set() or self.worker.stop.triggered:
                    value.answer(False)
                    continue
                detail = ("この会話にディレクトリへのアクセスを追加します。追加は毎回確認します。"
                          if value.name == "add_directory" else "この実行内の後続Toolも承認します。")
                approved = messagebox.askyesno("Tool実行の確認",
                    f"{value.name}\n{json.dumps(value.arguments, ensure_ascii=False, indent=2)}\n\n{detail}", parent=self.root)
                value.answer(approved)
            elif kind == "exit":
                self.closing = True
                self._set_busy(True)
                self.worker.close()
            elif kind == "closed":
                if self.closing:
                    self.root.destroy()
                    self.destroyed = True
                    return
                self.finished = True
                self._set_busy(False)
                self.status.set("起動または終了処理が終了しました。ウィンドウを閉じてください。")
                # No worker remains to acknowledge a subsequent close request.
                self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.root.after(50, self._poll)


def launch(args) -> None:
    from little_agent import agents
    from little_agent.a2a.serve import with_cli_paths
    from little_agent.cli import _shutdown, build_chat_context, build_memory_store
    from little_agent.config import AgentConfig

    try:
        import tkinter as tk
    except ImportError as exc:
        raise SystemExit("GUIにはTkinterが必要です。WindowsはPythonのTcl/Tk機能、Linuxはpython3-tkをインストールしてください。") from exc
    config = with_cli_paths(AgentConfig.from_env(), args.readable_path, args.writable_path)
    config.workspace.mkdir(parents=True, exist_ok=True)
    profile = agents.resolve_active(config, args.agent or config.active_agent)
    stop = StopController(config.stop_hotkey)
    names = [agents.DEFAULT_AGENT_NAME, *agents.list_agents(config.agents_dir)]

    def factory(confirm):
        memory = build_memory_store(config, enabled=not args.no_memory)
        return build_chat_context(config, profile, confirm, stop, memory)

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit(f"GUIを起動できません。デスクトップ環境を確認してください: {exc}") from exc
    ChatWindow(root, ChatWorker(factory, stop, _shutdown), names)
    root.mainloop()
