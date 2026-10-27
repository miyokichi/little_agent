"""Slash command framework for the CLI.

Two kinds of commands are supported:

* **Built-in commands** run inside the CLI and never call the LLM. They report
  state or switch agent (``/help``, ``/skills``, ``/agent`` ...).
* **Custom commands** are markdown files under a project ``commands/`` directory
  (and an optional global ``~/.little_agent/commands/``). Each file is a prompt
  template that is expanded with the caller's arguments and sent to
  ``agent.run()``. Custom commands are portable: dropping a ``.md`` file into the
  directory adds a command, mirroring the file-based skill philosophy.

Dispatch order for ``/name args`` input:

1. ``//text``           -> send literal ``/text`` to the agent (escape hatch).
2. built-in match       -> run handler locally, no LLM call.
3. custom match         -> expand template, send to ``agent.run()``.
4. no match             -> print an "unknown command" hint, never call the LLM.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from little_agent import agents as agent_profiles

if TYPE_CHECKING:
    from little_agent.agent import Agent
    from little_agent.session import ChatSession


@dataclass(slots=True)
class DispatchResult:
    """What the CLI should do after a slash command is handled.

    ``output`` is printed immediately. ``agent_prompt`` (if set) is sent through
    the chat session and its answer printed. ``should_exit`` ends the session.
    """

    output: str | None = None
    agent_prompt: str | None = None
    should_exit: bool = False


@dataclass(slots=True)
class CommandContext:
    """References a built-in handler may read from or mutate.

    Handlers act on the :class:`~little_agent.session.ChatSession` rather than a
    bare agent, because commands like ``/clear`` and ``/remember`` are about the
    conversation and its memory, not the runtime. ``agent`` is the session's
    current agent, for handlers that only need to inspect tools or config.

    ``activate`` is supplied by the CLI to switch the running agent to another
    profile; it swaps the session's agent and returns a status string.
    ``active_agent`` is the current profile name, or ``None`` for the library.
    """

    session: "ChatSession"
    registry: "CommandRegistry"
    active_agent: str | None = None
    activate: Callable[[str], str] | None = None

    @property
    def agent(self) -> "Agent":
        return self.session.agent


BuiltinHandler = Callable[[CommandContext, str], DispatchResult]


@dataclass(slots=True)
class BuiltinCommand:
    name: str
    description: str
    handler: BuiltinHandler
    aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class CustomCommand:
    name: str
    description: str
    template: str
    source: Path
    scope: str  # "project" or "global"

    def render(self, args: str) -> str:
        return render_template(self.template, args)


# --- template expansion -----------------------------------------------------

_POSITIONAL = re.compile(r"\$(\d+)")
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def render_template(template: str, args: str) -> str:
    """Substitute ``$ARGUMENTS`` and ``$1``, ``$2`` ... placeholders.

    ``$ARGUMENTS`` expands to the full argument string; ``$N`` to the Nth
    whitespace-separated token (empty if missing). If the template contains no
    placeholder at all and arguments were given, they are appended so a bare
    template still receives its input.
    """

    args = args.strip()
    positional = args.split()
    has_arguments = "$ARGUMENTS" in template
    has_positional = bool(_POSITIONAL.search(template))

    rendered = template.replace("$ARGUMENTS", args)

    def _replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return positional[index - 1] if 1 <= index <= len(positional) else ""

    rendered = _POSITIONAL.sub(_replace, rendered)

    if not has_arguments and not has_positional and args:
        rendered = f"{rendered.rstrip()}\n\n{args}"
    return rendered.strip()


def parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` YAML-ish frontmatter block from the body.

    Only simple ``key: value`` lines are parsed (no external YAML dependency).
    Returns ``(metadata, body)``; metadata is empty when no frontmatter exists.
    """

    match = _FRONTMATTER.match(raw)
    if not match:
        return {}, raw
    meta_block, body = match.group(1), match.group(2)
    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta, body


def load_custom_commands(scoped_dirs: list[tuple[str, Path]]) -> dict[str, CustomCommand]:
    """Load ``*.md`` command files from each ``(scope, dir)``.

    Later entries override earlier ones on a name clash, so pass global before
    project to let project commands win.
    """

    commands: dict[str, CustomCommand] = {}
    for scope, directory in scoped_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            name = path.stem.lower()
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = parse_frontmatter(raw)
            commands[name] = CustomCommand(
                name=name,
                description=meta.get("description", ""),
                template=body.strip(),
                source=path,
                scope=scope,
            )
    return commands


# --- registry ---------------------------------------------------------------


class CommandRegistry:
    def __init__(self, project_commands_dir: Path, global_commands_dir: Path) -> None:
        self.project_commands_dir = project_commands_dir
        self.global_commands_dir = global_commands_dir
        self.builtins: dict[str, BuiltinCommand] = {}
        self.aliases: dict[str, str] = {}
        self.custom: dict[str, CustomCommand] = {}
        for command in _builtin_commands():
            self.builtins[command.name] = command
            for alias in command.aliases:
                self.aliases[alias] = command.name
        self.reload_custom()

    def reload_custom(self) -> int:
        self.custom = load_custom_commands(
            [
                ("global", self.global_commands_dir),
                ("project", self.project_commands_dir),
            ]
        )
        return len(self.custom)

    def dispatch(self, ctx: CommandContext, text: str) -> DispatchResult | None:
        """Return a result for slash input, or ``None`` for ordinary input."""

        if not text.startswith("/"):
            return None
        if text.startswith("//"):
            return DispatchResult(agent_prompt=text[1:])

        parts = text[1:].split(maxsplit=1)
        if not parts:
            return self.builtins["help"].handler(ctx, "")
        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        canonical = self.aliases.get(name, name)
        if canonical in self.builtins:
            return self.builtins[canonical].handler(ctx, args)
        if name in self.custom:
            return DispatchResult(agent_prompt=self.custom[name].render(args))
        # /<skill-name> [request]: user-explicit activation. The skill's
        # instructions are put in front of the request, so the model does not
        # have to decide to activate it.
        skill = ctx.agent.skills.get(name)
        if skill is not None:
            request = args.strip() or f"Follow the '{skill.name}' skill."
            return DispatchResult(agent_prompt=f"{skill.activation_content()}\n\n{request}")
        return DispatchResult(
            output=f"Unknown command: /{name}\nType /help to see available commands."
        )


# --- built-in handlers ------------------------------------------------------


def _cmd_help(ctx: CommandContext, args: str) -> DispatchResult:
    registry = ctx.registry
    lines = ["Built-in commands:"]
    for command in sorted(registry.builtins.values(), key=lambda c: c.name):
        names = "/" + command.name
        if command.aliases:
            names += ", " + ", ".join("/" + a for a in command.aliases)
        lines.append(f"  {names:<20} {command.description}")

    lines.append("")
    if registry.custom:
        lines.append("Custom commands:")
        for command in sorted(registry.custom.values(), key=lambda c: c.name):
            description = command.description or "(no description)"
            lines.append(f"  {'/' + command.name:<20} {description}  [{command.scope}]")
    else:
        lines.append("Custom commands: none.")
        lines.append(f"  Add {'.md'} files to: {registry.project_commands_dir}")
        lines.append(f"  or (global):         {registry.global_commands_dir}")

    lines.append("")
    skills = ctx.agent.skills.load_all()
    if skills:
        lines.append(
            f"Skills: /<skill-name> [request] runs with that skill active ({len(skills)} available, see /skills)."
        )
    lines.append("Prefix a message with // to send literal text starting with a slash.")
    return DispatchResult(output="\n".join(lines))


def _cmd_exit(ctx: CommandContext, args: str) -> DispatchResult:
    return DispatchResult(should_exit=True)


def _first_line(text: str, limit: int = 80) -> str:
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _cmd_skills(ctx: CommandContext, args: str) -> DispatchResult:
    loader = ctx.agent.skills
    skills = loader.load_all()
    diagnostics = loader.diagnostics()
    query = args.strip()
    if query.lower() in {"check", "doctor"}:
        if not diagnostics:
            return DispatchResult(output=f"All {len(skills)} skill(s) follow the Agent Skills format.")
        return DispatchResult(output="\n".join(["Skill diagnostics:", *(f"  {item}" for item in diagnostics)]))
    if query:
        match = loader.get(query)
        if match is None:
            return DispatchResult(output=f"No skill named '{query}'. Try /skills for the list.")
        lines = [f"{match.name}  ({match.path})", f"  {match.description}"]
        for label, value in (("license", match.license), ("compatibility", match.compatibility)):
            if value:
                lines.append(f"  {label}: {value}")
        if match.metadata:
            lines.append("  metadata: " + ", ".join(f"{k}={v}" for k, v in match.metadata.items()))
        if match.legacy:
            lines.append("  (legacy heading format — no YAML frontmatter)")
        lines.extend(["", match.activation_content()])
        return DispatchResult(output="\n".join(lines))
    if not skills:
        return DispatchResult(output="No skills loaded.")
    lines = [f"Loaded skills ({len(skills)}):"]
    for skill in skills:
        flag = "  [legacy]" if skill.legacy else ""
        lines.append(f"  {skill.name:<24} {_first_line(skill.description)}{flag}")
    lines.append("")
    lines.append("/skills <name> shows a skill, /<skill-name> [request] runs with it active.")
    if diagnostics:
        lines.append(f"{len(diagnostics)} loading problem(s): /skills check")
    return DispatchResult(output="\n".join(lines))


def _cmd_tools(ctx: CommandContext, args: str) -> DispatchResult:
    names = ctx.agent.tools.names()
    lines = [f"Registered tools ({len(names)}):"]
    for name in names:
        tool = ctx.agent.tools.get(name)
        flag = " (confirm)" if getattr(tool, "requires_confirmation", False) else ""
        lines.append(f"  {name:<22} {_first_line(tool.description)}{flag}")
    return DispatchResult(output="\n".join(lines))


def _cmd_usage(ctx: CommandContext, args: str) -> DispatchResult:
    logger = ctx.agent.logger
    if logger is None:
        return DispatchResult(output="Logging is disabled, so no usage totals are tracked.")
    totals = logger.usage_totals
    estimated = ""
    if totals.estimated_tokens:
        estimated = f" (of which ~{totals.estimated_tokens} estimated)"
    lines = [
        f"Session usage (session {logger.session_id}):",
        f"  prompt     : {totals.prompt_tokens}",
        f"  completion : {totals.completion_tokens}",
        f"  total      : {totals.total_tokens}{estimated}",
    ]
    return DispatchResult(output="\n".join(lines))


def _cmd_config(ctx: CommandContext, args: str) -> DispatchResult:
    config = ctx.agent.config
    lines = [
        "Configuration:",
        f"  model              : {config.model}",
        f"  workspace          : {config.workspace}",
        f"  readable_paths     : {_paths_text(config.readable_paths)}",
        f"  writable_paths     : {_paths_text(config.writable_paths)}",
        f"  require_confirmation: {config.require_confirmation}",
        f"  max_tool_steps     : {config.max_tool_steps}",
        f"  enable_logging     : {config.enable_logging}",
        f"  base_url           : {config.openai_base_url}",
        f"  api_key            : {'set' if config.openai_api_key else 'not set (local fallback)'}",
        f"  commands (project) : {config.commands_dir}",
        f"  commands (global)  : {config.global_commands_dir}",
        f"  memory             : {ctx.session.memory.describe()}",
        f"  conversation       : {len(ctx.session.history)} message(s) in this session",
    ]
    return DispatchResult(output="\n".join(lines))


def _cmd_reload(ctx: CommandContext, args: str) -> DispatchResult:
    count = ctx.registry.reload_custom()
    return DispatchResult(output=f"Reloaded custom commands ({count} found). Skills reload automatically each turn.")


def _paths_text(paths: tuple[Path, ...]) -> str:
    return ", ".join(str(path) for path in paths) if paths else "(none)"


def _cmd_clear(ctx: CommandContext, args: str) -> DispatchResult:
    dropped = ctx.session.clear()
    return DispatchResult(
        output=f"Conversation cleared ({dropped} message(s) dropped). Persistent memory is untouched."
    )


def _cmd_memory(ctx: CommandContext, args: str) -> DispatchResult:
    store = ctx.session.memory
    if not store.enabled:
        return DispatchResult(
            output=(
                "Persistent memory is off for this session, so nothing is loaded or saved.\n"
                "The conversation itself is still kept until you exit or /clear.\n"
                "Start without --no-memory to turn it on."
            )
        )
    sections = store.sections()
    if not sections:
        return DispatchResult(output=f"Memory is on but still empty.\n  {store.describe()}")
    lines = [store.describe(), ""]
    for heading, body in sections:
        lines.append(f"## {heading}")
        lines.append(body)
        lines.append("")
    return DispatchResult(output="\n".join(lines).rstrip())


def _cmd_remember(ctx: CommandContext, args: str) -> DispatchResult:
    if not ctx.session.memory.enabled:
        return DispatchResult(output="[memory] off for this session; there is nothing to save to.")
    try:
        learned = ctx.session.learn()
    except Exception as exc:  # noqa: BLE001 - a failed reflection must not end the session.
        return DispatchResult(output=f"[memory] auto-learning skipped: {exc}")
    return DispatchResult(
        output="[memory] learned from this session." if learned else "[memory] nothing new to learn."
    )


def _cmd_agents(ctx: CommandContext, args: str) -> DispatchResult:
    config = ctx.agent.config
    # The built-in "default" agent (the whole library) is always listed first,
    # followed by any user-created agents on disk.
    profiles = [agent_profiles.default_profile(config)]
    for name in agent_profiles.list_agents(config.agents_dir):
        try:
            profiles.append(
                agent_profiles.resolve_active(config, name)
            )
        except (OSError, ValueError):
            continue
    lines = [f"Agents ({len(profiles)}):"]
    for profile in profiles:
        marker = "*" if ctx.active_agent == profile.name else " "
        tag = " [built-in]" if profile.builtin else ""
        description = profile.description or "(no description)"
        lines.append(f" {marker} {profile.name:<18} {len(profile.enabled_skills())} skill(s)  {description}{tag}")
    lines.append("")
    lines.append("Active is marked with *. Switch with /agent <name> (/agent default for the library).")
    return DispatchResult(output="\n".join(lines))


def _cmd_agent(ctx: CommandContext, args: str) -> DispatchResult:
    name = args.strip()
    if not name:
        return DispatchResult(
            output=(
                f"Active agent: {ctx.active_agent}\n"
                "Use /agents to list, /agent <name> to switch (/agent default for the library)."
            )
        )
    if ctx.activate is None:
        return DispatchResult(output="Agent switching is not available in this context.")
    try:
        return DispatchResult(output=ctx.activate(name))
    except FileNotFoundError as exc:
        return DispatchResult(output=str(exc))
    except (OSError, ValueError) as exc:
        return DispatchResult(output=f"Could not switch agent: {exc}")


_MCP_USAGE = (
    "Usage: /mcp                         servers and their status\n"
    "       /mcp tools [server]          tools offered by MCP servers\n"
    "       /mcp resources [server]      resources offered by MCP servers\n"
    "       /mcp prompts [server]        prompts offered by MCP servers\n"
    "       /mcp prompt <server> <name> [key=value ...]   run an MCP prompt\n"
    "       /mcp reconnect <server>      restart a server connection"
)


def _cmd_mcp(ctx: CommandContext, args: str) -> DispatchResult:
    view = getattr(ctx.agent, "mcp", None)
    if view is None or (not view.states and not view.missing and not view.manager.config_error):
        return DispatchResult(
            output="No MCP servers for this agent. Define them in mcp.json "
            "(and list them in the profile's \"mcp_servers\")."
        )
    words = args.split()
    action = words[0].lower() if words else ""
    states = {state.config.name: state for state in view.states}

    def pick(name: str | None) -> list:
        if name and name not in states:
            raise KeyError(name)
        return [states[name]] if name else list(states.values())

    try:
        if not action:
            lines = ["MCP servers:"]
            for state in view.states:
                detail = f"{len(state.tool_specs)} tool(s)" if state.status == "connected" else ""
                lines.append(
                    f"  {state.config.name:<16} {state.status:<13} {state.config.transport:<5} "
                    f"{detail}  {state.config.describe()}"
                )
                if state.error:
                    lines.append(f"    error: {_first_line(state.error, 200)}")
            lines.extend("  " + warning for warning in view.warnings() if "failed to start" not in warning)
            return DispatchResult(output="\n".join(lines))
        if action == "tools":
            lines = []
            for state in pick(words[1] if len(words) > 1 else None):
                lines.append(f"{state.config.name} ({state.status}):")
                for spec in state.tool_specs:
                    lines.append(f"  {spec['name']:<24} {_first_line(str(spec.get('description', '')))}")
            return DispatchResult(output="\n".join(lines) or "No MCP tools.")
        if action in {"resources", "prompts"}:
            lines = []
            for state in pick(words[1] if len(words) > 1 else None):
                client = state.client
                if client is None or not client.connected:
                    lines.append(f"{state.config.name}: not connected")
                    continue
                lines.append(f"{state.config.name}:")
                if action == "resources":
                    for item in client.list_resources():
                        lines.append(f"  {item.get('uri')}  {item.get('title') or item.get('name', '')}")
                    for item in client.list_resource_templates():
                        lines.append(f"  {item.get('uriTemplate')}  (template) {item.get('name', '')}")
                else:
                    for item in client.list_prompts():
                        arguments = " ".join(
                            f"{arg.get('name')}{'' if arg.get('required') else '?'}=..."
                            for arg in item.get("arguments") or []
                        )
                        lines.append(
                            f"  {item.get('name'):<20} {arguments}  "
                            f"{_first_line(str(item.get('description', '')))}"
                        )
            return DispatchResult(output="\n".join(lines) or f"No MCP {action}.")
        if action == "prompt":
            if len(words) < 3:
                return DispatchResult(output=_MCP_USAGE)
            state = pick(words[1])[0]
            if state.client is None or not state.client.connected:
                return DispatchResult(output=f"MCP server '{state.config.name}' is not connected.")
            arguments = {}
            for word in words[3:]:
                key, sep, value = word.partition("=")
                if not sep:
                    return DispatchResult(output=f"Prompt arguments are key=value, got: {word}")
                arguments[key] = value
            result = state.client.get_prompt(words[2], arguments)
            from little_agent.mcp.tools import render_content

            parts = []
            for message in result.get("messages") or []:
                text, _images = render_content([message.get("content")])
                if message.get("role") == "assistant":
                    text = f"(assistant) {text}"
                parts.append(text)
            prompt = "\n\n".join(part for part in parts if part.strip())
            if not prompt:
                return DispatchResult(output="The MCP prompt returned no text.")
            return DispatchResult(agent_prompt=prompt)
        if action == "reconnect":
            if len(words) < 2:
                return DispatchResult(output=_MCP_USAGE)
            state = view.manager.reconnect(pick(words[1])[0].config.name)
            ctx.agent._register_mcp_tools()
            if state.error:
                return DispatchResult(output=f"Reconnect failed: {state.error}")
            return DispatchResult(
                output=f"Reconnected '{state.config.name}' ({len(state.tool_specs)} tool(s))."
            )
    except KeyError as exc:
        return DispatchResult(output=f"Unknown MCP server: {exc.args[0]}. Known: {', '.join(states)}")
    except Exception as exc:  # noqa: BLE001 - McpError and friends: report, keep the REPL alive.
        return DispatchResult(output=f"MCP request failed: {exc}")
    return DispatchResult(output=_MCP_USAGE)


def _builtin_commands() -> list[BuiltinCommand]:
    return [
        BuiltinCommand("help", "List available commands.", _cmd_help, aliases=("?",)),
        BuiltinCommand("exit", "Quit the session.", _cmd_exit, aliases=("quit",)),
        BuiltinCommand(
            "skills", "List skills (/skills <name> detail, /skills check problems).", _cmd_skills
        ),
        BuiltinCommand("tools", "List registered tools.", _cmd_tools),
        BuiltinCommand("usage", "Show this session's token usage.", _cmd_usage),
        BuiltinCommand("config", "Show the current configuration.", _cmd_config),
        BuiltinCommand("reload", "Reload custom commands from disk.", _cmd_reload),
        BuiltinCommand("clear", "Clear the conversation (persistent memory is kept).", _cmd_clear),
        BuiltinCommand("memory", "Show what persistent memory holds.", _cmd_memory),
        BuiltinCommand(
            "remember", "Distil durable memory from this session now.", _cmd_remember
        ),
        BuiltinCommand("agents", "List agent profiles.", _cmd_agents),
        BuiltinCommand("agent", "Show or switch the active agent (/agent <name>).", _cmd_agent),
        BuiltinCommand("mcp", "Show MCP servers, tools, resources, prompts (/mcp help).", _cmd_mcp),
    ]
