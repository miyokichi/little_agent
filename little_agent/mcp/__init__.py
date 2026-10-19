"""Model Context Protocol (MCP) support.

Little Agent is an MCP **client**: servers listed in ``mcp.json`` are connected
and their tools, resources and prompts are offered to the agent
(:mod:`little_agent.mcp.manager`). It can also be an MCP **server**:
``little-agent serve-mcp`` exposes the agent itself as a tool to any MCP host
(:mod:`little_agent.mcp.server`).

Everything is implemented on the standard library, like the A2A stack.
"""

from little_agent.mcp.client import PROTOCOL_VERSION, McpClient, McpError
from little_agent.mcp.config import McpConfigError, McpServerConfig, load_mcp_config
from little_agent.mcp.manager import McpManager, McpView, shared_manager, shutdown_shared

__all__ = [
    "PROTOCOL_VERSION",
    "McpClient",
    "McpConfigError",
    "McpError",
    "McpManager",
    "McpServerConfig",
    "McpView",
    "load_mcp_config",
    "shared_manager",
    "shutdown_shared",
]
