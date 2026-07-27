from __future__ import annotations

import argparse
import json
from dataclasses import replace
from typing import Any

from little_agent import agents
from little_agent.agent import Agent
from little_agent.agents import AgentProfile
from little_agent.commands import CommandContext, CommandRegistry
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.skills.loader import SkillLoader
from little_agent.tools.delegation import DelegateTaskTool


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


def build_agent(
    config: AgentConfig,
    profile: AgentProfile,
    confirm,
    stop: StopController,
    depth: int = 0,
) -> Agent:
    """Construct an Agent for a profile (use agents.default_profile for the library).

    Profile overrides (model / max_tool_steps / require_confirmation) are layered
    onto the base config; ``core_tools`` filters the built-in core tools; and the
    skill loader is pointed at the profile's skills directory.

    ``depth`` is the delegation depth: the agent gets a ``delegate_task`` tool that
    spawns sub-agents (each at ``depth + 1``) until ``config.max_delegation_depth``
    is reached, at which point no further delegation tool is registered.
    """

    effective = replace(
        config,
        model=profile.model or config.model,
        max_tool_steps=(
            profile.max_tool_steps if profile.max_tool_steps is not None else config.max_tool_steps
        ),
        require_confirmation=(
            profile.require_confirmation
            if profile.require_confirmation is not None
            else config.require_confirmation
        ),
    )
    skills = SkillLoader(profile.skills_dir.resolve())
    agent = Agent(
        config=effective,
        skills=skills,
        confirm=confirm,
        stop=stop,
        core_tools=profile.core_tools_set(),
    )

    if depth < config.max_delegation_depth:
        def spawn(agent_name: str | None):
            sub_profile = agents.resolve_active(config, agent_name)
            # Sub-agents share the stop flag but never touch the parent's listener.
            return build_agent(config, sub_profile, confirm, stop.child(), depth + 1)

        agent.tools.register(
            DelegateTaskTool(
                spawn=spawn,
                available_agents=lambda: agents.list_agents(config.agents_dir),
                depth=depth,
                max_depth=config.max_delegation_depth,
            )
        )
    return agent


def _select_profile_at_launch(config: AgentConfig, requested: str | None) -> AgentProfile:
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
        return agents.load_profile(config.agents_dir, available[int(choice) - 1])
    if choice in available:
        return agents.load_profile(config.agents_dir, choice)
    print(f"Unknown selection '{choice}', using '{agents.DEFAULT_AGENT_NAME}'.")
    return agents.default_profile(config)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="little-agent")
    parser.add_argument("--agent", help="Name of the agent profile to run (under agents/).")
    args = parser.parse_args(argv)

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
    print("Type /help for commands, /exit to quit. /remember saves what was learned so far.\n")

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
            print(ctx.agent.run(user_text))
            print()
            continue
        if result.output is not None:
            print(result.output)
        if result.agent_prompt is not None:
            print(ctx.agent.run(result.agent_prompt))
        if result.should_exit:
            break
        print()

    _end_session(ctx.agent)


def _end_session(agent: Agent) -> None:
    try:
        if agent.end_session():
            print("[memory] learned from this session.")
    except Exception as exc:  # noqa: BLE001 - never let auto-learning break exit.
        print(f"[memory] auto-learning skipped: {exc}")
