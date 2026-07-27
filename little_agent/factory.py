"""Agent construction from a profile.

Kept separate from ``cli`` so both the CLI and the A2A server can build agents
without importing each other.
"""

from __future__ import annotations

from dataclasses import replace

from little_agent.agent import Agent
from little_agent.agents import AgentProfile
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.skills.loader import SkillLoader


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

    ``depth`` is the A2A delegation depth of this agent. While it is below
    ``config.max_delegation_depth`` the agent gets a ``delegate_task`` tool so it
    can hand subtasks to peer agents over A2A; at the limit the tool is omitted.
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
        # Imported lazily: the delegation tools pull in the A2A client stack,
        # which in turn may spawn servers that import this module.
        from little_agent.tools.delegation import DelegateTasksTool, DelegateTaskTool

        # ``stop`` is passed so a long delegation is abandoned (and the peer's
        # task cancelled) when the emergency-stop hotkey fires.
        agent.tools.register(DelegateTaskTool(config=config, depth=depth, stop=stop))
        agent.tools.register(DelegateTasksTool(config=config, depth=depth, stop=stop))
    return agent
