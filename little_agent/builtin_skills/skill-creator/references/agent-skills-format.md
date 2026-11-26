# Agent Skills形式の確認

公式仕様: https://agentskills.io/specification

- 必須ファイルは `SKILL.md`。先頭のYAML frontmatterに `name` と `description` を書く。
- `name` は親フォルダーと一致する、小文字・数字・ハイフンからなる1〜64文字。
  先頭・末尾のハイフン、連続ハイフンは使わない。
- `description` は1〜1024文字。何をするSkillかと、使うべき状況を書く。
- 本文は任意のMarkdown。特定の見出しは必須ではない。
- 任意フィールド: `license`、`compatibility`、`metadata`、実験的な `allowed-tools`。
- `scripts/`、`references/`、`assets/` は必要に応じて追加する。相対参照はSkillフォルダー基準。
- Little Agentの `tools.json` は専用Tool登録の任意拡張であり、公式仕様の必須ファイルではない。

複数行の説明にはYAMLの `>` や `|` を使える。コロンや引用符を含む一行の値は引用する。
`metadata` のキーと値は文字列にする。`allowed-tools` は空白区切りの文字列とし、
ホスト側のアクセス権限やTool確認とは区別する。
