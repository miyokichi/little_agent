"""MCP tools and resources, presented to the agent as ordinary tools.

Each tool an MCP server lists becomes one :class:`McpTool` named
``mcp__<server>__<tool>``, so tools from different servers never collide with
each other or with the core tools. Resources are reached through two generic
tools, ``list_mcp_resources`` and ``read_mcp_resource``.

The workspace boundary does not extend into an MCP server: what a server can
touch is decided by the server and by how it was started, not by Little Agent.
That is why calling an MCP tool asks for confirmation unless the server's
definition says ``"require_confirmation": false``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from little_agent.mcp.client import McpError
from little_agent.tools.base import ToolContext, ToolResult

if TYPE_CHECKING:
    from little_agent.mcp.manager import McpServerState

TOOL_PREFIX = "mcp__"
# OpenAI function names: ^[a-zA-Z0-9_-]{1,64}$
_NAME_LIMIT = 64
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def tool_name(server: str, tool: str) -> str:
    """The agent-facing name of a server's tool, kept within function-name rules."""

    name = f"{TOOL_PREFIX}{server}__{_UNSAFE.sub('_', tool)}"
    if len(name) <= _NAME_LIMIT:
        return name
    digest = hashlib.sha1(f"{server}/{tool}".encode("utf-8")).hexdigest()[:8]
    return f"{name[:_NAME_LIMIT - 9]}_{digest}"


def _input_schema(raw: Any) -> dict[str, Any]:
    schema = dict(raw) if isinstance(raw, dict) else {}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def render_content(blocks: Any) -> tuple[str, tuple[str, ...]]:
    """Turn MCP content blocks into (text, images) for the agent.

    Images (and image resources) go to the model as data URIs; text, embedded
    text resources and resource links become text. Audio and other binary data
    is described rather than inlined.
    """

    texts: list[str] = []
    images: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text", "")))
        elif kind == "image" and block.get("data"):
            mime = block.get("mimeType") or "image/png"
            images.append(f"data:{mime};base64,{block['data']}")
        elif kind == "audio":
            texts.append(f"[audio output ({block.get('mimeType', 'audio')}) omitted]")
        elif kind == "resource_link":
            label = block.get("title") or block.get("name") or block.get("uri")
            texts.append(f"[resource link] {label}: {block.get('uri')}")
        elif kind == "resource" and isinstance(block.get("resource"), dict):
            text, image = _render_resource(block["resource"])
            texts.append(text)
            if image:
                images.append(image)
    return "\n".join(texts), tuple(images)


def _render_resource(resource: dict[str, Any]) -> tuple[str, str | None]:
    uri = resource.get("uri", "")
    mime = str(resource.get("mimeType") or "")
    if "text" in resource:
        return f"[resource {uri}]\n{resource['text']}", None
    if mime.startswith("image/") and resource.get("blob"):
        return f"[resource {uri}] (image)", f"data:{mime};base64,{resource['blob']}"
    return f"[resource {uri}] binary content ({mime or 'unknown type'}) omitted", None


class McpTool:
    """One tool of one MCP server."""

    def __init__(self, state: "McpServerState", spec: dict[str, Any]) -> None:
        self._state = state
        self.server = state.config.name
        self.remote_name = str(spec["name"])
        self.name = tool_name(self.server, self.remote_name)
        title = spec.get("title") or (spec.get("annotations") or {}).get("title")
        description = str(spec.get("description") or title or self.remote_name)
        self.description = f"[MCP {self.server}] {description}"
        self.parameters = _input_schema(spec.get("inputSchema"))
        self.requires_confirmation = state.config.require_confirmation

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        client = self._state.client
        if client is None or not client.connected:
            return ToolResult(False, f"MCP server '{self.server}' is not connected.")
        try:
            result = client.call_tool(self.remote_name, kwargs)
        except McpError as exc:
            return ToolResult(False, str(exc))
        text, images = render_content(result.get("content"))
        structured = result.get("structuredContent")
        if structured is not None and not text:
            text = json.dumps(structured, ensure_ascii=False, indent=2)
        return ToolResult(
            ok=not bool(result.get("isError")),
            content=text or "(no content)",
            images=images,
        )


class ListMcpResourcesTool:
    name = "list_mcp_resources"
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "Only this MCP server. Omit to list every server's resources.",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, states: list["McpServerState"]) -> None:
        self._states = {state.config.name: state for state in states}
        self.description = (
            "List the resources (files, records, documents, ...) that the connected MCP "
            f"servers offer: {', '.join(self._states)}. Read one with read_mcp_resource."
        )

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        wanted = kwargs.get("server")
        if wanted and wanted not in self._states:
            return ToolResult(False, f"Unknown MCP server: {wanted}. Known: {', '.join(self._states)}")
        lines: list[str] = []
        for name, state in self._states.items():
            if wanted and name != wanted:
                continue
            client = state.client
            if client is None or not client.connected:
                lines.append(f"## {name}\n(not connected)")
                continue
            try:
                resources = client.list_resources()
                templates = client.list_resource_templates()
            except McpError as exc:
                lines.append(f"## {name}\nerror: {exc}")
                continue
            lines.append(f"## {name}")
            for item in resources:
                mime = f" ({item['mimeType']})" if item.get("mimeType") else ""
                about = f" — {item['description']}" if item.get("description") else ""
                lines.append(f"- {item.get('uri')}{mime}: {item.get('title') or item.get('name', '')}{about}")
            for item in templates:
                about = f" — {item['description']}" if item.get("description") else ""
                lines.append(f"- template {item.get('uriTemplate')}: {item.get('name', '')}{about}")
            if not resources and not templates:
                lines.append("(no resources)")
        return ToolResult(True, "\n".join(lines) or "(no MCP servers with resources)")


class ReadMcpResourceTool:
    name = "read_mcp_resource"
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "The MCP server that owns the resource."},
            "uri": {"type": "string", "description": "The resource URI, as listed by list_mcp_resources."},
        },
        "required": ["server", "uri"],
        "additionalProperties": False,
    }

    def __init__(self, states: list["McpServerState"]) -> None:
        self._states = {state.config.name: state for state in states}
        self.description = "Read one resource from a connected MCP server by its URI."

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        server = str(kwargs.get("server") or "")
        uri = str(kwargs.get("uri") or "")
        state = self._states.get(server)
        if state is None:
            return ToolResult(False, f"Unknown MCP server: {server}. Known: {', '.join(self._states)}")
        if state.client is None or not state.client.connected:
            return ToolResult(False, f"MCP server '{server}' is not connected.")
        try:
            result = state.client.read_resource(uri)
        except McpError as exc:
            return ToolResult(False, str(exc))
        texts: list[str] = []
        images: list[str] = []
        for item in result.get("contents") or []:
            if isinstance(item, dict):
                text, image = _render_resource(item)
                texts.append(text)
                if image:
                    images.append(image)
        return ToolResult(True, "\n\n".join(texts) or "(empty resource)", tuple(images))
