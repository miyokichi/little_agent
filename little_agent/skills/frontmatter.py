"""YAML frontmatter for ``SKILL.md``, on the standard library.

An Agent Skills ``SKILL.md`` starts with a YAML block between ``---`` lines::

    ---
    name: pdf-processing
    description: Extract PDF text, fill forms, merge files. Use when handling PDFs.
    license: Apache-2.0
    metadata:
      author: example-org
      version: "1.0"
    ---
    # PDF Processing
    ...

The fields a skill uses are small: scalars, one level of mapping
(``metadata``) and the occasional list. This parser covers exactly that
subset — plain / single-quoted / double-quoted scalars, ``|`` and ``>`` block
scalars, plain scalars continued on indented lines, nested mappings, block
lists (``- item``) and flow lists (``[a, b]``), and ``#`` comments — rather
than pulling in a YAML dependency.

It is deliberately forgiving in the way the Agent Skills client guide asks for:
a value such as ``description: Use when: the user asks`` (technically invalid
YAML because of the second colon) is read as the plain string it was meant to
be, so skills written for other clients still load.
"""

from __future__ import annotations

import json
import re
from typing import Any


class FrontmatterError(ValueError):
    """The frontmatter block exists but cannot be read."""


_KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s+|$)(.*)$")
_BLOCK_HEADER = re.compile(r"^([|>])([+-]?)\d*\s*(?:#.*)?$")


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return ``(yaml_block, body)``; ``yaml_block`` is ``None`` without frontmatter."""

    text = text.lstrip("﻿")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :]).strip()
    raise FrontmatterError("frontmatter starts with '---' but is never closed")


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    """Parse a ``SKILL.md``: ``(fields, body, had_frontmatter)``."""

    block, body = split_frontmatter(text)
    if block is None:
        return {}, body, False
    return parse_yaml_mapping(block), body, True


def parse_yaml_mapping(block: str) -> dict[str, Any]:
    lines = [_strip_comment_line(line) for line in block.splitlines()]
    value, index = _parse_mapping(lines, 0, 0)
    remaining = [line for line in lines[index:] if line.strip()]
    if remaining:
        raise FrontmatterError(f"cannot read frontmatter line: {remaining[0].strip()!r}")
    return value


def _strip_comment_line(line: str) -> str:
    return "" if line.lstrip().startswith("#") else line.rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _next_content(lines: list[str], index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _parse_mapping(lines: list[str], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while True:
        index = _next_content(lines, index)
        if index >= len(lines):
            return result, index
        line = lines[index]
        current = _indent(line)
        if current < indent:
            return result, index
        if current > indent:
            raise FrontmatterError(f"unexpected indentation: {line.strip()!r}")
        if "\t" in line[: current + 1]:
            raise FrontmatterError("tabs are not allowed for indentation")
        match = _KEY.match(line.strip())
        if not match:
            raise FrontmatterError(f"expected 'key: value', got {line.strip()!r}")
        key, rest = match.group(1), match.group(2).strip()
        index += 1
        if rest == "":
            nxt = _next_content(lines, index)
            if nxt < len(lines) and _indent(lines[nxt]) > indent:
                child_indent = _indent(lines[nxt])
                if lines[nxt].strip().startswith("- ") or lines[nxt].strip() == "-":
                    value, index = _parse_list(lines, nxt, child_indent)
                else:
                    value, index = _parse_mapping(lines, nxt, child_indent)
            elif nxt < len(lines) and _indent(lines[nxt]) == indent and lines[nxt].strip().startswith("- "):
                value, index = _parse_list(lines, nxt, indent)  # list at the key's own indent
            else:
                value = None
        elif _BLOCK_HEADER.match(rest):
            value, index = _parse_block_scalar(lines, index, indent, rest)
        else:
            value, index = _parse_inline(lines, index, indent, rest)
        result[key] = value


def _parse_list(lines: list[str], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while True:
        index = _next_content(lines, index)
        if index >= len(lines):
            return items, index
        line = lines[index]
        stripped = line.strip()
        if _indent(line) != indent or not (stripped.startswith("- ") or stripped == "-"):
            return items, index
        rest = stripped[1:].strip()
        index += 1
        if not rest:
            items.append(None)
        elif _KEY.match(rest) and not rest.startswith(("'", '"')):
            # "- key: value" -> a mapping item; its other keys align after "- ".
            child_indent = indent + 2
            lines = lines[:index - 1] + [" " * child_indent + rest] + lines[index:]
            value, index = _parse_mapping(lines, index - 1, child_indent)
            items.append(value)
        else:
            value, index = _parse_inline(lines, index, indent, rest)
            items.append(value)


def _parse_block_scalar(lines: list[str], index: int, indent: int, header: str) -> tuple[str, int]:
    match = _BLOCK_HEADER.match(header)
    assert match is not None
    style, chomp = match.group(1), match.group(2)
    collected: list[str] = []
    block_indent: int | None = None
    while index < len(lines):
        line = lines[index]
        if line.strip():
            if _indent(line) <= indent:
                break
            if block_indent is None:
                block_indent = _indent(line)
            collected.append(line[block_indent:] if _indent(line) >= block_indent else line.strip())
        else:
            collected.append("")
        index += 1
    while collected and collected[-1] == "" and chomp != "+":
        collected.pop()
    if style == "|":
        text = "\n".join(collected)
    else:
        paragraphs: list[str] = []
        current: list[str] = []
        for line in collected:
            if line == "":
                paragraphs.append(" ".join(current))
                current = []
            else:
                current.append(line.strip())
        paragraphs.append(" ".join(current))
        text = "\n".join(paragraphs)
    return (text if chomp == "-" else text + "\n") if text else "", index


def _parse_inline(lines: list[str], index: int, indent: int, rest: str) -> tuple[Any, int]:
    if rest.startswith(("'", '"')) and not _closed_quote(rest):
        # A quoted scalar spanning lines: gather until the quote closes.
        parts = [rest]
        while index < len(lines) and not _closed_quote(" ".join(parts)):
            parts.append(lines[index].strip())
            index += 1
        return _scalar(" ".join(parts)), index
    if rest.startswith("[") or rest.startswith("{"):
        return _flow(rest), index
    # Plain scalar, possibly continued on more-indented lines (folded with spaces).
    parts = [_strip_inline_comment(rest)]
    while index < len(lines):
        line = lines[index]
        if not line.strip() or _indent(line) <= indent:
            break
        parts.append(line.strip())
        index += 1
    return _scalar(" ".join(part for part in parts if part)), index


def _closed_quote(text: str) -> bool:
    quote = text[0]
    if quote == "'":
        body = text[1:].replace("''", "")
        return "'" in body
    escaped = False
    for char in text[1:]:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return True
    return False


def _strip_inline_comment(text: str) -> str:
    return re.split(r"\s+#", text, maxsplit=1)[0].strip()


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text.startswith('"'):
        end = _quote_end(text, '"')
        try:
            return json.loads(text[: end + 1])
        except json.JSONDecodeError:
            return text[1:end]
    if text.startswith("'"):
        end = _quote_end(text, "'")
        return text[1:end].replace("''", "'")
    lowered = text.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", text):
        return float(text)
    return text


def _quote_end(text: str, quote: str) -> int:
    index = 1
    while index < len(text):
        char = text[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if char == quote:
            if quote == "'" and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            return index
        index += 1
    return len(text) - 1


def _flow(text: str) -> Any:
    text = _strip_inline_comment(text) if not text.rstrip().endswith(("]", "}")) else text.strip()
    opener, closer = text[0], "]" if text[0] == "[" else "}"
    if not text.endswith(closer):
        return _scalar(text)
    inner = text[1:-1]
    items = [item.strip() for item in _split_flow(inner) if item.strip()]
    if opener == "[":
        return [_scalar(item) for item in items]
    mapping: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition(":")
        if not sep:
            raise FrontmatterError(f"cannot read flow mapping entry {item!r}")
        mapping[str(_scalar(key))] = _scalar(value)
    return mapping


def _split_flow(text: str) -> list[str]:
    parts, current, quote = [], [], ""
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == ",":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts
