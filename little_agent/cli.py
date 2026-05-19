from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from little_agent.agent import Agent
from little_agent.config import AgentConfig
from little_agent.skills.loader import SkillLoader


def _confirm(tool_name: str, arguments: dict[str, Any]) -> bool:
    print(f"\nTool confirmation required: {tool_name}")
    print(json.dumps(arguments, ensure_ascii=False, indent=2))
    answer = input("Run this tool? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def main() -> None:
    config = AgentConfig.from_env()
    config.workspace.mkdir(parents=True, exist_ok=True)
    skills = SkillLoader(Path("skills").resolve())
    agent = Agent(config=config, skills=skills, confirm=_confirm)

    mode = "OpenAI" if config.openai_api_key else "local fallback"
    print(f"Little Agent ({mode})")
    print(f"Workspace: {config.workspace}")
    print("Type /exit to quit.\n")

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
        print(agent.run(user_text))
        print()

