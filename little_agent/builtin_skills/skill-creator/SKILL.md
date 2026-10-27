---
name: skill-creator
description: "Agent Skills 形式（YAMLフロントマター付き SKILL.md）のSkillを作成・更新・検証する。`SKILL.md`、`scripts/`、`references/`、`assets/`、Little Agent 拡張の `tools.json` を含むSkillフォルダを作る。 Use when: ユーザーが新しいSkillを作りたいとき; 既存SkillにTool manifestやscriptsフォルダを追加したいとき; Skillが仕様（name・descriptionの規則など）に沿っているか確認したいとき; SKILL.mdの雛形が必要なとき."
allowed-tools: create_skill validate_skill
metadata:
  author: little-agent
  version: "1.0"
---
# Skill Creator

## When to use
- ユーザーが新しいSkillを作りたいとき
- 既存SkillにTool manifestやscriptsフォルダを追加したいとき
- Skillフォルダがポータブルな構成になっているか確認したいとき
- SKILL.mdの雛形が必要なとき

## Instructions
- SKILL.md は YAML フロントマターで始める。必須は `name` と `description`、任意は `license` / `compatibility` / `metadata` / `allowed-tools`（スペース区切り）。それ以外の独自項目は `metadata` の下に置く。
- `name` は小文字英数字と単一のハイフンだけ（64文字以内、先頭・末尾にハイフン不可）で、**フォルダ名と一致**させる。`create_skill` が正規化する。
- `description` は「何をするか」と「いつ使うか」を両方書き、判断の手がかりになるキーワードを入れる（1024文字以内）。エージェントはまずこれだけを見て、Skillを使うか決める。
- 本文（フロントマターの後）は手順・例・注意点。500行以内に収め、詳しい資料は `references/` に分けて相対パスで参照する。
- 既存Skillを上書きしない。上書きが必要ならユーザーの明示的な指定を待つ。
- Skillの実行ロジックが必要な場合は `scripts/` と `tools.json` を作る。
- 詳細資料が必要なSkillでは `references/` を作る。
- 出力ファイルやテンプレートが必要なSkillでは `assets/` を作る。
- 作成後は `validate_skill` で構成を確認する。
