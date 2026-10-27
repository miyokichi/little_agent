from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "create_skill":
            result = create_skill(workspace, arguments)
        elif tool == "validate_skill":
            result = validate_skill(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown skill creator tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Skill creator script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def create_skill(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = normalize_name(str(arguments.get("name") or ""))
    description = str(arguments.get("description") or "").strip()
    overwrite = bool(arguments.get("overwrite", False))
    if not name:
        return {"ok": False, "content": "Skill name is required."}
    if not description:
        return {"ok": False, "content": "Skill description is required."}

    skill_dir = safe_skill_dir(workspace, name)
    if skill_dir.exists() and not overwrite:
        return {"ok": False, "content": f"Skill already exists: skills/{name}"}

    skill_dir.mkdir(parents=True, exist_ok=True)
    write_file(skill_dir / "SKILL.md", skill_markdown(name, description), overwrite)

    created = [f"skills/{name}/SKILL.md"]
    include_scripts = bool(arguments.get("include_scripts", False))
    include_tools_manifest = bool(arguments.get("include_tools_manifest", False))

    if include_scripts:
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        write_file(scripts_dir / "example_tool.py", example_script(), overwrite)
        created.append(f"skills/{name}/scripts/example_tool.py")

    if bool(arguments.get("include_references", False)):
        (skill_dir / "references").mkdir(exist_ok=True)
        created.append(f"skills/{name}/references/")

    if bool(arguments.get("include_assets", False)):
        (skill_dir / "assets").mkdir(exist_ok=True)
        created.append(f"skills/{name}/assets/")

    if include_tools_manifest:
        if not include_scripts:
            (skill_dir / "scripts").mkdir(exist_ok=True)
            write_file(skill_dir / "scripts" / "example_tool.py", example_script(), overwrite)
            created.append(f"skills/{name}/scripts/example_tool.py")
        write_file(skill_dir / "tools.json", tools_manifest(name), overwrite)
        created.append(f"skills/{name}/tools.json")

    return {"ok": True, "content": "Created:\n" + "\n".join(created)}


def validate_skill(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = normalize_name(str(arguments.get("name") or ""))
    if not name:
        return {"ok": False, "content": "Skill name is required."}
    skill_dir = safe_skill_dir(workspace, name)
    issues: list[str] = []

    warnings: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_dir.exists():
        issues.append(f"Missing directory: skills/{name}")
    if not skill_md.exists():
        issues.append(f"Missing file: skills/{name}/SKILL.md")
    else:
        validate_frontmatter(skill_md.read_text(encoding="utf-8"), skill_dir.name, issues, warnings)

    manifest = skill_dir / "tools.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            tools = data.get("tools")
            if not isinstance(tools, list):
                issues.append("tools.json must contain a tools list.")
            else:
                for item in tools:
                    validate_tool_manifest_item(skill_dir, item, issues)
        except json.JSONDecodeError as exc:
            issues.append(f"tools.json is invalid JSON: {exc}")

    notes = "".join(f"\n- warning: {item}" for item in warnings)
    if issues:
        return {"ok": False, "content": "Validation failed:\n" + "\n".join(f"- {issue}" for issue in issues) + notes}
    return {"ok": True, "content": f"Skill is valid (Agent Skills format): skills/{name}{notes}"}


# Agent Skills specification (https://agentskills.io/specification).
SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RULE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def read_frontmatter(text: str) -> dict[str, str] | None:
    """Top-level ``key: value`` pairs of the YAML frontmatter (None if absent).

    Enough to validate the spec's fields; nested values (``metadata``) and
    block scalars are recorded as present without being interpreted.
    """

    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, str] = {}
    current: str | None = None
    for line in lines[1:]:
        if line.strip() in {"---", "..."}:
            return fields
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if current is not None and fields[current] in {"", "|", ">", "|-", ">-"}:
                fields[current] = (fields[current].lstrip("|>-") + " " + line.strip()).strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        current = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        fields[current] = value
    raise ValueError("frontmatter is not closed with '---'")


def validate_frontmatter(text: str, folder: str, issues: list[str], warnings: list[str]) -> None:
    try:
        fields = read_frontmatter(text)
    except ValueError as exc:
        issues.append(f"SKILL.md {exc}")
        return
    if fields is None:
        issues.append("SKILL.md must start with YAML frontmatter ('---' ... '---').")
        return
    name = fields.get("name", "")
    if not name:
        issues.append("Frontmatter is missing 'name'.")
    else:
        if len(name) > 64:
            issues.append("'name' must be at most 64 characters.")
        if not NAME_RULE.match(name):
            issues.append("'name' may only contain lowercase letters, digits and single hyphens, not at either end.")
        if name != folder:
            issues.append(f"'name' ({name}) must match the directory name ({folder}).")
    description = fields.get("description", "")
    if not description:
        issues.append("Frontmatter is missing 'description'.")
    elif len(description) > 1024:
        issues.append("'description' must be at most 1024 characters.")
    elif len(description) < 40:
        warnings.append("'description' is short; say what the skill does and when to use it.")
    if len(fields.get("compatibility", "")) > 500:
        issues.append("'compatibility' must be at most 500 characters.")
    unknown = sorted(set(fields) - SPEC_FIELDS)
    if unknown:
        warnings.append(f"Fields outside the spec: {', '.join(unknown)} (put custom data under 'metadata').")
    body = text.split("---", 2)[-1]
    if len(body.splitlines()) > 500:
        warnings.append("SKILL.md is over 500 lines; move detail into references/.")


def validate_tool_manifest_item(skill_dir: Path, item: Any, issues: list[str]) -> None:
    if not isinstance(item, dict):
        issues.append("Each tools.json item must be an object.")
        return
    for key in ["name", "description", "script", "parameters"]:
        if key not in item:
            issues.append(f"Tool item is missing key: {key}")
    script = skill_dir / str(item.get("script", ""))
    if item.get("script") and not script.exists():
        issues.append(f"Tool script does not exist: {script.relative_to(skill_dir)}")


def safe_skill_dir(workspace: Path, name: str) -> Path:
    skills_root = (workspace / "skills").resolve()
    skill_dir = (skills_root / name).resolve()
    if skills_root not in [skill_dir, *skill_dir.parents]:
        raise ValueError("Skill path escaped the workspace.")
    return skill_dir


def normalize_name(name: str) -> str:
    """A spec-valid skill name: lowercase letters, digits and single hyphens."""

    lowered = name.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", lowered)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized[:64].strip("-")


def write_file(path: Path, content: str, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        return
    path.write_text(content, encoding="utf-8")


def skill_markdown(name: str, description: str) -> str:
    title = name.replace("-", " ").title()
    return f"""---
name: {name}
description: {json.dumps(description, ensure_ascii=False)}
metadata:
  version: "0.1"
---
# {title}

## When to use
- TODO: このSkillを使うべきユーザー依頼や状況を書く（同じ内容を description にも含める）。

## Instructions
- TODO: Agentが従う手順、注意点、判断基準を書く。
- 必要な詳細資料は `references/` に置き、ここから相対パスで参照する（例: [詳細](references/REFERENCE.md)）。
- 実行ロジックが必要な場合は `scripts/` と `tools.json` を使う。
"""


def tools_manifest(skill_name: str) -> str:
    tool_name = normalize_name(skill_name).replace("-", "_") + "_example"
    data = {
        "tools": [
            {
                "name": tool_name,
                "description": f"Example script-backed tool for {skill_name}.",
                "script": "scripts/example_tool.py",
                "requires_confirmation": False,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Input text."}
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def example_script() -> str:
    return '''from __future__ import annotations

import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    arguments = payload.get("arguments") or {}
    text = str(arguments.get("text") or "")
    print(json.dumps({"ok": True, "content": text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


if __name__ == "__main__":
    raise SystemExit(main())

