"""Agent Skills YAML and metadata validation, without imposing Markdown headings."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError("YAML mapping keys must be strings")
        if key in mapping:
            raise ValueError(f"Duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    raw = raw.lstrip("\ufeff")
    match = re.match(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", raw, re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError("SKILL.md needs opening and closing YAML frontmatter delimiters")
    data = yaml.load(match.group(1), Loader=_UniqueSafeLoader)
    if not isinstance(data, dict):
        raise ValueError("Skill frontmatter must be a YAML mapping")
    for key in ("name", "description"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Skill frontmatter needs a non-empty string '{key}'")
    for key in ("license", "compatibility", "allowed-tools"):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"Skill frontmatter '{key}' must be a string")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items()):
        raise ValueError("Skill metadata must map string keys to string values")
    return data, raw[match.end():].strip()


def metadata_issues(data: dict[str, Any], directory: Path) -> list[str]:
    """Cosmetic spec violations are diagnostics, as the client guide recommends."""
    issues = []
    name = unicodedata.normalize("NFKC", data["name"])
    if len(name) > 64 or name != name.lower() or not all(c.isalnum() or c == "-" for c in name):
        issues.append("name must be lowercase letters/digits/hyphens, at most 64 characters")
    if name.startswith("-") or name.endswith("-") or "--" in name:
        issues.append("name cannot start/end with '-' or contain '--'")
    if name != unicodedata.normalize("NFKC", directory.name):
        issues.append("name must match the parent directory name")
    if len(data["description"]) > 1024:
        issues.append("description exceeds 1024 characters")
    if "compatibility" in data and not 1 <= len(data["compatibility"]) <= 500:
        issues.append("compatibility must be 1-500 characters")
    unknown = set(data) - {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    if unknown:
        issues.append("non-standard frontmatter fields: " + ", ".join(sorted(unknown)))
    return issues
