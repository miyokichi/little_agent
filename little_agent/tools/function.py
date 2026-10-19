"""Tools written as plain Python functions.

A tool is a function with type hints and a docstring; the ``@tool`` decorator
turns it into a tool the agent can call::

    from little_agent.tools import ToolContext, tool

    @tool(requires_confirmation=True)
    def write_file(context: ToolContext, path: str, content: str) -> str:
        \"\"\"Write a UTF-8 text file inside the workspace.

        Args:
            path: Workspace-relative file path.
            content: Complete file content.
        \"\"\"
        target = context.path_policy.resolve(path, access="write")
        target.write_text(content, encoding="utf-8")
        return f"Wrote {path}"

* **name** — the function name (or ``@tool(name=...)``).
* **description** — the first paragraph of the docstring.
* **parameters** — the JSON Schema is derived from the signature: the type hint
  gives the type, a default makes it optional, and the docstring's ``Args:``
  section (or ``Annotated[str, "..."]``) gives each description.
* **context** — a parameter annotated ``ToolContext`` (or named ``context``) is
  given the run's workspace and path policy; it is not shown to the model.

The return value becomes the result: a ``str`` as is, ``None`` as an empty
success, a ``dict``/``list`` as JSON, a :class:`ToolResult` untouched. Raise
:class:`ToolError` to fail with a message the model reads; any other exception
is reported as a tool failure too.

Arguments are checked against the derived schema before the function runs, so
a model that sends the wrong shape gets told what is wrong instead of a
``TypeError`` from inside the function.
"""

from __future__ import annotations

import inspect
import json
import re
import types
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args, get_origin

from little_agent import schema as json_schema
from little_agent.tools.base import ToolContext, ToolResult

_CONTEXT_NAMES = {"context", "ctx"}
_ARGS_HEADERS = {"args:", "arguments:", "parameters:", "params:", "引数:"}


class ToolError(Exception):
    """Raise inside a function tool to fail with this message."""


def _strip_optional(hint: Any) -> tuple[Any, bool]:
    """``X | None`` / ``Optional[X]`` -> (X, True)."""

    origin = get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(args) < len(get_args(hint)):
            return (args[0] if len(args) == 1 else typing.Union[tuple(args)]), True
    return hint, False


def type_schema(hint: Any) -> dict[str, Any]:
    """JSON Schema for a Python type hint (unknown types accept anything)."""

    if hint is inspect.Parameter.empty or hint is Any:
        return {}
    hint, _ = _strip_optional(hint)
    origin = get_origin(hint)
    args = get_args(hint)
    if origin is typing.Annotated:
        schema = type_schema(args[0])
        notes = [arg for arg in args[1:] if isinstance(arg, str)]
        if notes:
            schema["description"] = notes[0]
        return schema
    if origin is typing.Literal:
        values = list(args)
        schema: dict[str, Any] = {"enum": values}
        kinds = {type(value) for value in values}
        if len(kinds) == 1:
            schema.update(type_schema(kinds.pop()))
        return schema
    if origin is typing.Union or origin is types.UnionType:
        return {"anyOf": [type_schema(arg) for arg in args]}
    if hint is bool:
        return {"type": "boolean"}
    if hint is int:
        return {"type": "integer"}
    if hint is float:
        return {"type": "number"}
    if hint is str or hint is Path or (isinstance(hint, type) and issubclass(hint, Path)):
        return {"type": "string"}
    if hint in (list, tuple, set, frozenset) or origin in (list, tuple, set, frozenset):
        schema = {"type": "array"}
        if args and args[-1] is not Ellipsis:
            schema["items"] = type_schema(args[0])
        return schema
    if hint is dict or origin is dict:
        schema = {"type": "object"}
        if len(args) == 2 and args[1] is not Any:
            schema["additionalProperties"] = type_schema(args[1])
        return schema
    return {}


def parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split a docstring into (description, {parameter: description}).

    Understands a Google-style ``Args:`` section (``name: text`` or
    ``name (type): text``, with indented continuation lines) and a reST
    ``:param name: text`` line. Everything before the first section is the
    description.
    """

    text = inspect.cleandoc(doc or "")
    if not text:
        return "", {}
    lines = text.splitlines()
    description: list[str] = []
    params: dict[str, str] = {}
    current: str | None = None
    in_args = False
    in_section = False
    for line in lines:
        stripped = line.strip()
        rest = re.match(r":param\s+(?:\w+\s+)?(\w+):\s*(.*)", stripped)
        if rest:
            params[rest.group(1)] = rest.group(2)
            current, in_section = rest.group(1), True
            continue
        if stripped.lower() in _ARGS_HEADERS:
            in_args = in_section = True
            current = None
            continue
        if re.fullmatch(r"[A-Za-z ]+:", stripped) and not line.startswith(" "):
            in_args, in_section, current = False, True, None  # Returns:, Raises:, ...
            continue
        if in_args:
            entry = re.match(r"(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)", stripped)
            if entry and not line.startswith("        "):
                current = entry.group(1).lstrip("*")
                params[current] = entry.group(2)
            elif current and stripped:
                params[current] = f"{params[current]} {stripped}".strip()
            continue
        if not in_section:
            description.append(line)
    summary = "\n".join(description).strip().split("\n\n")[0]
    return " ".join(part.strip() for part in summary.splitlines()), params


def _coerce(value: Any, schema: dict[str, Any]) -> Any:
    """Forgive the common slip of a number or flag sent as a string."""

    if not isinstance(value, str):
        return value
    kind = schema.get("type")
    try:
        if kind == "integer" and re.fullmatch(r"[+-]?\d+", value.strip()):
            return int(value)
        if kind == "number":
            return float(value)
    except ValueError:
        return value
    if kind == "boolean" and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return value


class FunctionTool:
    """A tool backed by a Python function (made by :func:`tool`)."""

    def __init__(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
        requires_confirmation: bool = False,
    ) -> None:
        self.func = func
        self.name = name or func.__name__
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError(f"Tool name {self.name!r} must match [A-Za-z0-9_-]{{1,64}}.")
        self.requires_confirmation = requires_confirmation
        # Set by the loader: the file the tool came from and which directory.
        self.source: Path | None = None
        self.origin: str = "python"

        summary, param_docs = parse_docstring(func.__doc__)
        self.description = (description or summary or self.name).strip()
        try:
            hints = typing.get_type_hints(func, include_extras=True)
        except Exception:  # noqa: BLE001 - unresolvable hints: fall back to raw annotations
            hints = dict(getattr(func, "__annotations__", {}))

        self._context_param: str | None = None
        self._accepts_extra = False
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in inspect.signature(func).parameters.values():
            hint = hints.get(param.name, param.annotation)
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                self._accepts_extra = True
                continue
            if param.kind is inspect.Parameter.VAR_POSITIONAL:
                continue
            if hint is ToolContext or (
                param.name in _CONTEXT_NAMES and hint in (inspect.Parameter.empty, ToolContext)
            ):
                self._context_param = param.name
                continue
            prop = type_schema(hint)
            if param.name in param_docs and "description" not in prop:
                prop["description"] = param_docs[param.name]
            if param.default is not inspect.Parameter.empty:
                if param.default is not None and _jsonable(param.default):
                    prop["default"] = param.default
            else:
                required.append(param.name)
            properties[param.name] = prop
        self.parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            self.parameters["required"] = required
        if not self._accepts_extra:
            self.parameters["additionalProperties"] = False

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the underlying function directly (handy in tests and scripts)."""

        return self.func(*args, **kwargs)

    def __repr__(self) -> str:
        return f"<FunctionTool {self.name}>"

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        properties = self.parameters["properties"]
        arguments = {
            key: _coerce(value, properties.get(key, {})) for key, value in kwargs.items()
        }
        # JSON null for an optional argument means "not given".
        arguments = {
            key: value
            for key, value in arguments.items()
            if not (value is None and key not in self.parameters.get("required", []))
        }
        errors = json_schema.validate(arguments, self.parameters)
        if errors:
            return ToolResult(False, "Invalid arguments: " + "; ".join(errors[:5]))
        if self._context_param:
            arguments[self._context_param] = context
        try:
            value = self.func(**arguments)
        except ToolError as exc:
            return ToolResult(False, str(exc))
        return to_result(value)


def to_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if value is None:
        return ToolResult(True, "(done)")
    if isinstance(value, str):
        return ToolResult(True, value)
    if isinstance(value, (dict, list, tuple, int, float, bool)):
        return ToolResult(True, json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return ToolResult(True, str(value))


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    requires_confirmation: bool = False,
) -> Any:
    """Make a function a tool. Use bare (``@tool``) or with options.

    ``requires_confirmation=True`` asks the user before the tool runs (and is
    refused on a server with no human, unless auto-approve is on). Use it for
    anything that writes, deletes, executes or reaches the network.
    """

    def wrap(target: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            target,
            name=name,
            description=description,
            requires_confirmation=requires_confirmation,
        )

    return wrap(func) if func is not None else wrap
