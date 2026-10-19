"""Finding function tools in directories of ``.py`` files.

Every ``*.py`` file in a tool directory is imported, and each module-level
object made with ``@tool`` becomes a tool. Adding a tool is adding a file (or a
function to one); removing it is deleting the file, or listing its name in
``LITTLE_AGENT_DISABLED_TOOLS``.

Directories are read in order and a later one replaces an earlier tool of the
same name, so a workspace can override a built-in::

    little_agent/builtin_tools/       the tools shipped with the package
    ~/.little_agent/tools/            yours, in every workspace
    <workspace>/tools/                this project's
    LITTLE_AGENT_TOOL_DIRS            anything else, in the order given

Files whose name starts with ``_`` are not loaded (keep helpers there). A file
that fails to import is skipped and reported; it never stops the agent.

A module is executed again only when its file changes, so building an agent
per A2A task does not re-run every tool file, and ``/reload`` picks up edits.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from little_agent.config import builtin_tools_dir
from little_agent.tools.function import FunctionTool

_MODULE_PREFIX = "little_agent_tools_"


@dataclass(slots=True)
class LoadedTools:
    tools: dict[str, FunctionTool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


_cache: dict[Path, tuple[int, int, ModuleType]] = {}
_cache_lock = threading.Lock()


def _module_for(path: Path) -> ModuleType:
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        cached = _cache.get(path)
        if cached is not None and cached[:2] == key:
            return cached[2]
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
        name = f"{_MODULE_PREFIX}{digest}_{path.stem}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None:
            raise ImportError(f"cannot import {path}")
        module = importlib.util.module_from_spec(spec)
        # Compiled from source rather than through the import system, so a
        # cached .pyc can never stand in for a file that was just edited.
        code = compile(path.read_bytes(), str(path), "exec")
        # Registered before executing so dataclasses and friends can find it.
        sys.modules[name] = module
        try:
            exec(code, module.__dict__)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        _cache[path] = (*key, module)
        return module


def origin_label(directory: Path, workspace: Path | None = None) -> str:
    if directory == builtin_tools_dir():
        return "built-in"
    if directory == (Path.home() / ".little_agent" / "tools").resolve():
        return "global"
    if workspace is not None and directory == (workspace / "tools").resolve():
        return "workspace"
    return "configured"


def load_function_tools(
    directories: tuple[Path, ...] | list[Path],
    workspace: Path | None = None,
) -> LoadedTools:
    """Load every ``@tool`` from the directories; later directories win."""

    loaded = LoadedTools()
    for directory in directories:
        directory = directory.resolve()
        if not directory.is_dir():
            continue
        origin = origin_label(directory, workspace)
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                module = _module_for(path)
            except Exception as exc:  # noqa: BLE001 - a broken file is reported, not fatal
                last = traceback.format_exception_only(type(exc), exc)[-1].strip()
                loaded.errors.append(f"{path}: {last}")
                continue
            for value in vars(module).values():
                if isinstance(value, FunctionTool):
                    value.source = path
                    value.origin = origin
                    loaded.tools[value.name] = value
    return loaded
