from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from little_agent.agent import Agent
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.skills.loader import SkillLoader


def _make_confirm(stop_hotkey: str):
    def _confirm(tool_name: str, arguments: dict[str, Any]) -> bool:
        print(f"\nTool confirmation required: {tool_name}")
        print(json.dumps(arguments, ensure_ascii=False, indent=2))
        print(
            "Approving runs this tool AND auto-approves the rest of this session. "
            f"Emergency stop: {stop_hotkey} (or move the mouse to a screen corner)."
        )
        answer = input("Approve for this session? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    return _confirm


def main() -> None:
    config = AgentConfig.from_env()
    config.workspace.mkdir(parents=True, exist_ok=True)
    skills = SkillLoader(Path("skills").resolve())
    stop = StopController(config.stop_hotkey)
    agent = Agent(config=config, skills=skills, confirm=_make_confirm(config.stop_hotkey), stop=stop)

    mode = "OpenAI" if config.openai_api_key else "local fallback"
    print(f"Little Agent ({mode})")
    print(f"Workspace: {config.workspace}")
    print(f"Emergency stop while the agent acts: {config.stop_hotkey}")
    print("Type /exit to quit. /remember saves what was learned so far.\n")

    while True:
        try:
            user_text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_text:
            continue
        if user_text in {"/exit", "/quit"}:
            break
        if user_text == "/remember":
            print("[memory] learned from this session." if agent.remember() else "[memory] nothing new to learn.")
            print()
            continue
        print(agent.run(user_text))
        print()

    _end_session(agent)


def _end_session(agent: Agent) -> None:
    try:
        if agent.end_session():
            print("[memory] learned from this session.")
    except Exception as exc:  # noqa: BLE001 - never let auto-learning break exit.
        print(f"[memory] auto-learning skipped: {exc}")

