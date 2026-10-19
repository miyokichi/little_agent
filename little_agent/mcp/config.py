"""MCP server definitions, read from ``mcp.json``.

The file uses the ``mcpServers`` layout most MCP hosts share, so a definition
can be copied between Little Agent, Claude Desktop, Claude Code, Cursor, ...::

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:\\\\projects"],
          "env": {"LOG_LEVEL": "warn"}
        },
        "github": {
          "url": "https://api.example.com/mcp",
          "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}
        }
      }
    }

A server with ``command`` is started as a child process and spoken to over
stdio; one with ``url`` is reached over Streamable HTTP. ``${NAME}`` in
``args`` / ``env`` / ``headers`` / ``url`` is taken from the environment, so a
secret need not be written into the file.

Little Agent's own keys (all optional):

* ``require_confirmation`` — ask before each tool call (default ``true``).
* ``tools`` — allowlist of the server's tool names to expose.
* ``disabled`` — keep the definition but do not start the server.
* ``timeout`` — seconds to wait for one request (default 60).
* ``cwd`` — working directory of a stdio server (default: the workspace).

Two files are read and merged — the global ``~/.little_agent/mcp.json`` first,
then the workspace's — so a project's definition of a name wins.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 60.0

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class McpConfigError(ValueError):
    """An ``mcp.json`` that cannot be used."""


def expand_env(value: str, environ: dict[str, str] | None = None) -> str:
    """Replace ``${NAME}`` with the environment value (empty when unset)."""

    source = os.environ if environ is None else environ
    return _ENV_REFERENCE.sub(lambda match: source.get(match.group(1), ""), value)


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    require_confirmation: bool = True
    tools: tuple[str, ...] | None = None
    disabled: bool = False
    timeout: float = DEFAULT_TIMEOUT
    source: Path | None = None

    @property
    def transport(self) -> str:
        return "stdio" if self.command else "http"

    def describe(self) -> str:
        if self.command:
            return " ".join([self.command, *self.args])
        return str(self.url)

    def tool_allowed(self, name: str) -> bool:
        return self.tools is None or name in self.tools


def parse_server(name: str, raw: Any, base: Path, source: Path | None = None) -> McpServerConfig:
    """Validate one ``mcpServers`` entry. ``base`` resolves a relative ``cwd``."""

    if not _SERVER_NAME.match(name):
        raise McpConfigError(
            f"MCP server name {name!r} may only contain letters, digits, '_' and '-'."
        )
    if not isinstance(raw, dict):
        raise McpConfigError(f"MCP server '{name}' must be a JSON object.")
    command = raw.get("command")
    url = raw.get("url")
    if bool(command) == bool(url):
        raise McpConfigError(f"MCP server '{name}' needs exactly one of 'command' or 'url'.")

    args = raw.get("args") or []
    env = raw.get("env") or {}
    headers = raw.get("headers") or {}
    if not isinstance(args, list):
        raise McpConfigError(f"MCP server '{name}': 'args' must be an array.")
    if not isinstance(env, dict) or not isinstance(headers, dict):
        raise McpConfigError(f"MCP server '{name}': 'env' and 'headers' must be objects.")
    tools = raw.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise McpConfigError(f"MCP server '{name}': 'tools' must be an array of tool names.")

    cwd = None
    if raw.get("cwd"):
        path = Path(expand_env(str(raw["cwd"]))).expanduser()
        cwd = (path if path.is_absolute() else base / path).resolve()

    return McpServerConfig(
        name=name,
        command=expand_env(str(command)) if command else None,
        args=tuple(expand_env(str(item)) for item in args),
        env={str(key): expand_env(str(value)) for key, value in env.items()},
        cwd=cwd,
        url=expand_env(str(url)) if url else None,
        headers={str(key): expand_env(str(value)) for key, value in headers.items()},
        require_confirmation=bool(raw.get("require_confirmation", True)),
        tools=tuple(str(item) for item in tools) if tools is not None else None,
        disabled=bool(raw.get("disabled", False)),
        timeout=float(raw.get("timeout") or DEFAULT_TIMEOUT),
        source=source,
    )


def load_mcp_config(paths: tuple[Path, ...] | list[Path], base: Path) -> dict[str, McpServerConfig]:
    """Read and merge ``mcp.json`` files; later files override earlier names.

    A missing file is simply skipped. A file that exists but is malformed raises
    :class:`McpConfigError` naming it, so a typo is not silently ignored.
    """

    servers: dict[str, McpServerConfig] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise McpConfigError(f"Could not read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise McpConfigError(f"{path} must contain a JSON object.")
        entries = data.get("mcpServers", data.get("servers", {}))
        if not isinstance(entries, dict):
            raise McpConfigError(f"{path}: 'mcpServers' must be an object.")
        for name, raw in entries.items():
            try:
                servers[str(name)] = parse_server(str(name), raw, base, source=path)
            except McpConfigError as exc:
                raise McpConfigError(f"{path}: {exc}") from exc
    return servers
