"""Run with run_powershell(timeout_seconds=0); return one JSON object on stdout."""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk


def main() -> None:
    root = tk.Tk()
    root.title("Little Agent — 入力待ち")
    root.resizable(False, False)
    value = tk.StringVar()
    result = {"status": "cancelled"}

    def confirm() -> None:
        result.update(status="ok", value=value.get())
        root.destroy()

    frame = ttk.Frame(root, padding=20)
    frame.grid()
    ttk.Label(frame, text="エージェントに渡す内容を入力してください。").grid(
        row=0, column=0, columnspan=2, pady=(0, 10)
    )
    entry = ttk.Entry(frame, textvariable=value, width=40)
    entry.grid(row=1, column=0, columnspan=2, pady=(0, 12))
    ttk.Button(frame, text="確定", command=confirm).grid(row=2, column=0)
    ttk.Button(frame, text="キャンセル", command=root.destroy).grid(row=2, column=1)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.bind("<Return>", lambda _event: confirm())
    root.bind("<Escape>", lambda _event: root.destroy())
    entry.focus_set()
    root.mainloop()

    # Keep stdout machine-readable. Write any diagnostic logs to stderr.
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
