"""Run a Little Agent profile as an A2A server.

    python -m little_agent.a2a.serve --agent office --port 8801
    little-agent --serve-a2a --agent office --port 8801

Each A2A task gets a freshly built agent and a run that keeps nothing, so tasks
never share context. Tools that need confirmation are refused unless
auto-approval is enabled, because a served agent has no human at a prompt.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from little_agent import agents
from little_agent.a2a.models import agent_card, agent_skill
from little_agent.a2a.server import DEFAULT_PORT, A2AService, serve
from little_agent.agents import AgentProfile
from little_agent.config import AgentConfig
from little_agent.factory import build_agent
from little_agent.skills.loader import SkillLoader

VERSION = "0.1.0"


def _card_skills(profile: AgentProfile) -> list[dict[str, Any]]:
    """Advertise the profile's Little Agent skills as A2A skills."""

    skills = []
    loader = SkillLoader(
        [root.resolve() for root in profile.skill_roots()], names=profile.skill_names()
    )
    for skill in loader.load_all():
        skills.append(
            agent_skill(
                skill_id=skill.name,
                name=skill.name,
                description=skill.description or skill.when_to_use or skill.name,
                tags=[tag.strip() for tag in skill.allowed_tools if tag.strip()][:8],
            )
        )
    if not skills:
        skills.append(
            agent_skill(
                skill_id="general",
                name="general",
                description="General assistance using this agent's core tools.",
            )
        )
    return skills


def build_service(
    config: AgentConfig,
    profile: AgentProfile,
    host: str,
    port: int,
    token: str | None = None,
    auto_approve: bool = False,
) -> A2AService:
    description = profile.description or f"Little Agent profile '{profile.name}'."

    def confirm(tool_name: str, _arguments: dict[str, Any]) -> bool:
        if auto_approve:
            return True
        print(f"[a2a] denied '{tool_name}' (needs confirmation; no human at this server).")
        return False

    def agent_factory(depth: int, stop: Any):
        # One fresh agent per task: its own context, its own stop controller.
        return build_agent(config, profile, confirm, stop, depth=depth)

    card = agent_card(
        name=f"little-agent/{profile.name}",
        description=description,
        url=f"http://{host}:{port}/",
        version=VERSION,
        skills=_card_skills(profile),
        requires_auth=bool(token),
    )
    return A2AService(card, agent_factory, token=token)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="little-agent-a2a")
    parser.add_argument("--agent", help="Agent profile to serve (default: the whole library).")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT}).")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Allow tools that normally require confirmation (no human is at this server).",
    )
    args = parser.parse_args(argv)

    config = AgentConfig.from_env()
    config.workspace.mkdir(parents=True, exist_ok=True)
    profile = agents.resolve_active(config, args.agent)
    token = os.getenv("LITTLE_AGENT_A2A_TOKEN") or None
    auto_approve = args.auto_approve or _env_flag("LITTLE_AGENT_A2A_AUTO_APPROVE")

    service = build_service(
        config, profile, args.host, args.port, token=token, auto_approve=auto_approve
    )
    httpd = serve(service, args.host, args.port)
    print(f"A2A agent '{service.card['name']}' on http://{args.host}:{args.port}/")
    print(f"Agent Card: http://{args.host}:{args.port}/.well-known/agent-card.json")
    print(f"Auth: {'bearer token required' if token else 'none'}")
    print(f"Confirmation-required tools: {'auto-approved' if auto_approve else 'denied'}")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()
        httpd.server_close()


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


if __name__ == "__main__":
    main()
