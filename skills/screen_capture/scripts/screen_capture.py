from __future__ import annotations

import base64
import io
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

_DEFAULT_SCREENSHOT_PATH = "screenshots/screen.png"
_DEFAULT_PROMPT = "この画面に何が表示されているか説明して。"
_MAX_WIDTH = 1568


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "take_screenshot":
            result = take_screenshot(workspace, arguments)
        elif tool == "describe_screen":
            result = describe_screen(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown screen tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Screen capture script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def take_screenshot(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    raw_path = str(arguments.get("path") or _DEFAULT_SCREENSHOT_PATH).strip()
    target = safe_workspace_path(workspace, raw_path)
    if target is None:
        return {"ok": False, "content": f"Path is outside workspace: {raw_path}"}

    captured = capture_image(arguments.get("region"))
    if "error" in captured:
        return {"ok": False, "content": captured["error"]}
    image = captured["image"]

    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG")
    rel = target.relative_to(workspace)
    return {"ok": True, "content": f"Saved screenshot to {rel} ({image.width}x{image.height})"}


def describe_screen(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    # Validate API config BEFORE capturing so the error path is testable headlessly.
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"ok": False, "content": "OPENAI_API_KEY is not configured; cannot describe the screen."}
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LITTLE_AGENT_VISION_MODEL") or os.getenv("LITTLE_AGENT_MODEL", "gpt-4.1-mini")
    timeout = int(os.getenv("LITTLE_AGENT_TIMEOUT_SECONDS", "60"))
    prompt = str(arguments.get("prompt") or _DEFAULT_PROMPT).strip()

    captured = capture_image(arguments.get("region"))
    if "error" in captured:
        return {"ok": False, "content": captured["error"]}
    image = downscale(captured["image"], _MAX_WIDTH)

    save_path = str(arguments.get("save_path") or "").strip()
    if save_path:
        target = safe_workspace_path(workspace, save_path)
        if target is None:
            return {"ok": False, "content": f"save_path is outside workspace: {save_path}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, format="PNG")

    data_uri = png_data_uri(image)
    return call_vision(base_url, api_key, model, timeout, prompt, data_uri)


def capture_image(region: Any) -> dict[str, Any]:
    """Return {"image": PIL.Image} or {"error": str}."""
    try:
        import mss
        from PIL import Image
    except ImportError:
        return {"error": "mss と Pillow が必要です。pip install mss Pillow"}

    bbox = parse_region(region)
    try:
        with mss.mss() as sct:
            if bbox is None:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            else:
                monitor = {"left": bbox[0], "top": bbox[1], "width": bbox[2], "height": bbox[3]}
            shot = sct.grab(monitor)
    except Exception as exc:  # noqa: BLE001 - common when headless / no display.
        return {"error": f"Could not capture the screen (no display?): {exc}"}

    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return {"image": image}


def parse_region(region: Any) -> list[int] | None:
    if not region:
        return None
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        raise ValueError("region must be [x, y, width, height].")
    return [int(value) for value in region]


def downscale(image: Any, max_width: int) -> Any:
    if image.width <= max_width:
        return image
    ratio = max_width / float(image.width)
    new_size = (max_width, max(1, int(image.height * ratio)))
    return image.resize(new_size)


def png_data_uri(image: Any) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def call_vision(
    base_url: str,
    api_key: str,
    model: str,
    timeout: int,
    prompt: str,
    data_uri: str,
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "content": f"Vision API returned HTTP {exc.code}: {detail}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "content": f"Could not reach vision API: {exc.reason}"}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "content": f"Vision API timed out after {timeout} seconds."}

    try:
        parsed = json.loads(raw)
        content = parsed["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        return {"ok": False, "content": f"Unexpected vision API response: {exc}"}

    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return {"ok": True, "content": text.strip()}


def safe_workspace_path(workspace: Path, raw: str) -> Path | None:
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    try:
        resolved = path.resolve()
    except Exception:  # noqa: BLE001
        return None
    if workspace not in [resolved, *resolved.parents]:
        return None
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
