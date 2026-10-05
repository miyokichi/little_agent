"""A small MCP server for the tests, speaking stdio (default) or Streamable HTTP.

    python tests/mcp_fake_server.py            # stdio
    (or McpFakeHttpServer(...) in-process)     # HTTP, JSON or SSE replies

It implements just enough of the protocol to exercise the client: paginated
tools, text/image/structured/error results, resources, prompts, a roots/list
request back to the client, and a tools/list_changed notification.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

PNG_1x1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

TOOLS_PAGE_1 = [
    {
        "name": "echo",
        "description": "Echo the text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {"name": "fail", "description": "Always reports an error.", "inputSchema": {"type": "object"}},
]
TOOLS_PAGE_2 = [
    {"name": "picture", "description": "Returns an image."},
    {"name": "stats", "description": "Returns structured content only.", "inputSchema": {"type": "object"}},
    {"name": "show.roots", "description": "Asks the client for its roots.", "inputSchema": {"type": "object"}},
    {"name": "grow", "description": "Adds a tool and announces it.", "inputSchema": {"type": "object"}},
]
EXTRA_TOOL = {"name": "late", "description": "Added at runtime.", "inputSchema": {"type": "object"}}


class FakeMcp:
    """Protocol logic, independent of transport.

    ``ask_client`` sends a request to the client and returns its response; it is
    only available on stdio.
    """

    def __init__(self, ask_client: Callable[[str, dict], dict] | None = None) -> None:
        self.ask_client = ask_client
        self.extra_tools: list[dict] = []
        self.calls: list[str] = []

    def handle(self, message: dict[str, Any], notify: Callable[[dict], None]) -> dict[str, Any] | None:
        method = message.get("method")
        params = message.get("params") or {}
        if "id" not in message:
            self.calls.append(str(method))
            return None
        self.calls.append(str(method))

        def ok(result: dict) -> dict:
            return {"jsonrpc": "2.0", "id": message["id"], "result": result}

        if method == "initialize":
            return ok(
                {
                    "protocolVersion": params.get("protocolVersion"),
                    "capabilities": {"tools": {"listChanged": True}, "resources": {}, "prompts": {}},
                    "serverInfo": {"name": "fake", "version": "1.0"},
                    "instructions": "Fake server: prefer the echo tool.",
                }
            )
        if method == "ping":
            return ok({})
        if method == "tools/list":
            if params.get("cursor") == "page2":
                return ok({"tools": TOOLS_PAGE_2 + self.extra_tools})
            return ok({"tools": TOOLS_PAGE_1, "nextCursor": "page2"})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                return ok({"content": [{"type": "text", "text": f"echo: {args.get('text')}"}]})
            if name == "fail":
                return ok({"content": [{"type": "text", "text": "it broke"}], "isError": True})
            if name == "picture":
                return ok({"content": [{"type": "image", "data": PNG_1x1, "mimeType": "image/png"}]})
            if name == "stats":
                return ok({"content": [], "structuredContent": {"count": 3}})
            if name == "show.roots":
                if self.ask_client is None:
                    return ok({"content": [{"type": "text", "text": "no roots over this transport"}]})
                answer = self.ask_client("roots/list", {})
                uris = [root["uri"] for root in answer.get("result", {}).get("roots", [])]
                return ok({"content": [{"type": "text", "text": "\n".join(uris)}]})
            if name == "grow":
                self.extra_tools = [EXTRA_TOOL]
                notify({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                return ok({"content": [{"type": "text", "text": "grown"}]})
            if name == "late":
                return ok({"content": [{"type": "text", "text": "late tool ran"}]})
            return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": f"Unknown tool {name}"}}
        if method == "resources/list":
            return ok(
                {
                    "resources": [
                        {"uri": "memo://readme", "name": "readme", "mimeType": "text/plain"},
                        {"uri": "memo://logo", "name": "logo", "mimeType": "image/png"},
                    ]
                }
            )
        if method == "resources/templates/list":
            return ok({"resourceTemplates": [{"uriTemplate": "memo://{name}", "name": "memo"}]})
        if method == "resources/read":
            uri = params.get("uri")
            if uri == "memo://readme":
                return ok({"contents": [{"uri": uri, "mimeType": "text/plain", "text": "hello from a resource"}]})
            if uri == "memo://logo":
                return ok({"contents": [{"uri": uri, "mimeType": "image/png", "blob": PNG_1x1}]})
            return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32002, "message": "Resource not found"}}
        if method == "prompts/list":
            return ok(
                {
                    "prompts": [
                        {
                            "name": "review",
                            "description": "Review a file.",
                            "arguments": [{"name": "path", "required": True}],
                        }
                    ]
                }
            )
        if method == "prompts/get":
            path = (params.get("arguments") or {}).get("path", "?")
            return ok(
                {
                    "messages": [
                        {"role": "user", "content": {"type": "text", "text": f"Please review {path}."}}
                    ]
                }
            )
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}


def serve_stdio() -> None:
    out = sys.stdout
    pending: list[dict] = []
    counter = iter(range(1, 1_000_000))

    def write(message: dict) -> None:
        out.write(json.dumps(message) + "\n")
        out.flush()

    def ask_client(method: str, params: dict) -> dict:
        request_id = f"srv-{next(counter)}"
        write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("id") == request_id and "method" not in message:
                return message
            pending.append(message)
        return {}

    server = FakeMcp(ask_client)
    print("fake server starting", file=sys.stderr, flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        pending.append(json.loads(line))
        while pending:
            reply = server.handle(pending.pop(0), write)
            if reply is not None:
                write(reply)


class McpFakeHttpServer:
    """Streamable HTTP flavour; ``sse=True`` answers requests as event streams."""

    SESSION = "fake-session-1"

    def __init__(self, sse: bool = False) -> None:
        self.fake = FakeMcp()
        self.sse = sse
        self.seen_headers: list[dict[str, str]] = []
        self.deleted = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                outer.seen_headers.append(dict(self.headers))
                length = int(self.headers.get("Content-Length") or 0)
                message = json.loads(self.rfile.read(length))
                is_init = message.get("method") == "initialize"
                if not is_init and self.headers.get("Mcp-Session-Id") != outer.SESSION:
                    self.send_response(400)
                    self.end_headers()
                    return
                notes: list[dict] = []
                reply = outer.fake.handle(message, notes.append)
                if reply is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                if outer.sse and not is_init:
                    body = "".join(
                        f"event: message\ndata: {json.dumps(item)}\n\n" for item in [*notes, reply]
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    body = json.dumps(reply).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if is_init:
                    self.send_header("Mcp-Session-Id", outer.SESSION)
                self.end_headers()
                self.wfile.write(body)

            def do_DELETE(self) -> None:
                outer.deleted = self.headers.get("Mcp-Session-Id") == outer.SESSION
                self.send_response(200)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    serve_stdio()
