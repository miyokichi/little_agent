"""Web tool: fetch an http(s) URL."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

from little_agent.tools import ToolError, tool

_DEFAULT_MAX_BYTES = 1_000_000
_USER_AGENT = "little-agent/0.1"


@tool(requires_confirmation=True)
def fetch_url(url: str, timeout_seconds: int = 30, max_bytes: int = _DEFAULT_MAX_BYTES) -> str:
    """Fetch the text content of an http(s) URL and return it (truncated if large).

    Args:
        url: The http(s) URL to fetch.
        timeout_seconds: Timeout in seconds.
        max_bytes: Maximum number of bytes to read from the response body.
    """
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ToolError(f"Only http(s) URLs are supported: {url}")

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(max_bytes + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            status = response.status
    except urllib.error.HTTPError as exc:
        raise ToolError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"Could not reach {url}: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ToolError(f"Request to {url} timed out after {timeout_seconds} seconds.") from exc

    body = raw[:max_bytes].decode(charset, errors="replace")
    if len(raw) > max_bytes:
        body = f"{body}\n... (truncated at {max_bytes} bytes)"
    return f"[HTTP {status}]\n{body}"
