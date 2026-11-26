---
name: skill-creator
description: 'Little Agent用のポータブルSkill作成、更新、検証を支援する。`SKILL.md`、`tools.json`、`scripts/`、`references/`、`assets/` を含むSkillフォルダを作る。 使用場面: - ユーザーが新しいSkillを作りたいとき - 既存SkillにTool manifestやscriptsフォルダを追加したいとき - Skillフォルダがポータブルな構成になっているか確認したいとき - Skill名、説明、Allowed tools、Instructionsの雛形が必要なとき'
allowed-tools: create_skill validate_skill
---

# skill-creator

## Description
Little Agent用のポータブルSkill作成、更新、検証を支援する。`SKILL.md`、`tools.json`、`scripts/`、`references/`、`assets/` を含むSkillフォルダを作る。

## When to use
- ユーザーが新しいSkillを作りたいとき
- 既存SkillにTool manifestやscriptsフォルダを追加したいとき
- Skillフォルダがポータブルな構成になっているか確認したいとき
- Skill名、説明、Allowed tools、Instructionsの雛形が必要なとき

## Allowed tools
- create_skill
- validate_skill

## Instructions
- Skill名は小文字英数字とハイフンだけに正規化する。
- 既存Skillを上書きしない。上書きが必要ならユーザーの明示的な指定を待つ。
- Skillの実行ロジックが必要な場合は `scripts/` を作る。`tools.json` はLittle Agent専用の任意拡張であり、公式仕様の必須ファイルではない。
- `SKILL.md` の先頭にYAML frontmatterで `name` と `description` を書く。nameは親ディレクトリと一致させる。本文の見出しは自由に選ぶ。
- 詳細資料が必要なSkillでは `references/` を作る。
- 出力ファイルやテンプレートが必要なSkillでは `assets/` を作る。
- 作成後は `validate_skill` で構成を確認する。


形式の詳細は必要に応じて [Agent Skills形式の確認](references/agent-skills-format.md) を読む。
