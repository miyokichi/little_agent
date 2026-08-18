from __future__ import annotations

import argparse
import json
from typing import Any

from little_agent import agents
from little_agent.agent import Agent, StructuredOutputError
from little_agent.commands import CommandContext, CommandRegistry
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.factory import build_agent


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


def _run(agent: Agent, text: str) -> str:
    """Run one execution and render its result for the terminal."""

    try:
        return agent.run(text).text
    except StructuredOutputError as exc:
        return f"Structured output error: {exc}"


def _select_profile_at_launch(config: AgentConfig, requested: str | None) -> agents.AgentProfile:
    """Pick the launch agent: explicit request > env default > picker > default."""

    name = requested or config.active_agent
    if name:
        try:
            return agents.resolve_active(config, name)
        except FileNotFoundError:
            available = agents.list_agents(config.agents_dir)
            hint = ", ".join(available) if available else "(none)"
            print(f"Agent '{name}' not found. Available: {hint}")

    available = agents.list_agents(config.agents_dir)
    if not available:
        return agents.default_profile(config)

    print("Available agents:")
    print(f"  0) {agents.DEFAULT_AGENT_NAME} (all library skills & tools)")
    for index, agent_name in enumerate(available, start=1):
        print(f"  {index}) {agent_name}")
    try:
        choice = input("Select an agent [0]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return agents.default_profile(config)
    if not choice or choice == "0":
        return agents.default_profile(config)
    if choice.isdigit() and 1 <= int(choice) <= len(available):
        return agents.load_profile(
            config.agents_dir, available[int(choice) - 1], config.skill_library_dir
        )
    if choice in available:
        return agents.load_profile(config.agents_dir, choice, config.skill_library_dir)
    print(f"Unknown selection '{choice}', using '{agents.DEFAULT_AGENT_NAME}'.")
    return agents.default_profile(config)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="little-agent")
    parser.add_argument("--agent", help="Name of the agent profile to run (under agents/).")
    parser.add_argument(
        "--serve-a2a",
        action="store_true",
        help="Serve this agent over the A2A protocol instead of starting the interactive CLI.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="A2A bind address (with --serve-a2a).")
    parser.add_argument("--port", type=int, help="A2A port (with --serve-a2a).")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="With --serve-a2a: allow tools that normally require confirmation.",
    )
    args = parser.parse_args(argv)

    if args.serve_a2a:
        from little_agent.a2a import serve as a2a_serve

        serve_argv: list[str] = ["--host", args.host]
        if args.agent:
            serve_argv += ["--agent", args.agent]
        if args.port is not None:
            serve_argv += ["--port", str(args.port)]
        if args.auto_approve:
            serve_argv.append("--auto-approve")
        a2a_serve.main(serve_argv)
        return

    config = AgentConfig.from_env()
    config.workspace.mkdir(parents=True, exist_ok=True)
    stop = StopController(config.stop_hotkey)
    confirm = _make_confirm(config.stop_hotkey)

    profile = _select_profile_at_launch(config, args.agent)
    agent = build_agent(config, profile, confirm, stop)
    registry = CommandRegistry(config.commands_dir, config.global_commands_dir)
    ctx = CommandContext(agent=agent, registry=registry, active_agent=profile.name)

    def activate(name: str) -> str:
        switched = agents.resolve_active(config, name)
        ctx.agent = build_agent(config, switched, confirm, stop)
        ctx.active_agent = switched.name
        if switched.builtin:
            return f"Switched to '{switched.name}' (all library skills & tools)."
        return f"Switched to agent '{switched.name}' ({len(switched.enabled_skills())} skill(s))."

    ctx.activate = activate

    mode = "OpenAI" if config.openai_api_key else "local fallback"
    print(f"Little Agent ({mode})")
    print(f"Workspace: {config.workspace}")
    print(f"Active agent: {ctx.active_agent}")
    print(f"Emergency stop while the agent acts: {config.stop_hotkey}")
    print("Each message is one independent run; nothing carries over between them.")
    print("Type /help for commands, /exit to quit.\n")

    while True:
        try:
            user_text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_text:
            continue

        result = registry.dispatch(ctx, user_text)
        if result is None:
            print(_run(ctx.agent, user_text))
            print()
            continue
        if result.output is not None:
            print(result.output)
        if result.agent_prompt is not None:
            print(_run(ctx.agent, result.agent_prompt))
        if result.should_exit:
            break
        print()

    _shutdown()


def _shutdown() -> None:
    try:
        from little_agent.a2a.peers import shutdown_shared

        shutdown_shared()  # stop any local A2A servers started for delegation
    except Exception as exc:  # noqa: BLE001 - never let cleanup break exit.
        print(f"[a2a] peer shutdown skipped: {exc}")
