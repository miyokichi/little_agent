# Little Agent

Windowsで動くPython製の小さなAI agent systemです。LLM、Tool、Agent Skillを分けた構成にしてあり、最初はCLIから動かせます。

## 仕様

### 実行環境

- OS: Windowsを主対象
- Python: 3.10以上
- UI: CLI
- Shell tool: PowerShell
- LLM: OpenAI互換 Chat Completions API
- HTTP client: Python標準ライブラリの `urllib`
- OpenAI SDK: 不使用
- APIキーがない場合: 簡易ローカルフォールバックで日時取得やディレクトリ一覧のみ試せます

### OpenAI互換API

`OPENAI_BASE_URL` と `OPENAI_API_KEY` を設定すると、OpenAI互換の `/chat/completions` APIに直接HTTPでアクセスします。

公式OpenAI APIの例:

```text
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
LITTLE_AGENT_MODEL=gpt-4.1-mini
```

ローカル互換サーバの例:

```text
OPENAI_API_KEY=dummy
OPENAI_BASE_URL=http://localhost:1234/v1
LITTLE_AGENT_MODEL=local-model-name
```

OpenRouterやLiteLLM proxyなども、OpenAI互換のChat Completions形式であれば同じ設定で使えます。

注意: このAgentはTool実行のために `tools` / `tool_calls` を使います。互換API側がtool callingに対応していない場合、通常の返答はできてもTool実行は動かない可能性があります。

### Agentの責務

Agentは以下を行います。

- ユーザー入力を受け取る
- 入力に関連しそうなAgent Skillを読み込む
- LLMにsystem prompt、会話履歴、Tool定義を渡す
- LLMが要求したToolを実行する
- Tool結果を `tool` メッセージとしてLLMへ戻す
- LLMが追加Toolを要求する場合は、最大ステップ数まで繰り返す
- 最終回答をユーザーに返す

### Multi-step Tool Loop

Agentは1ターン内で以下のループを実行します。

```text
user input
  -> LLM
  -> tool_calls
  -> Tool実行
  -> tool resultをLLMへ返す
  -> final answer または次のtool_calls
```

最大Toolステップ数は `LITTLE_AGENT_MAX_TOOL_STEPS` で設定します。デフォルトは `5` です。

### Tool仕様

Toolは `little_agent/tools` に実装します。各Toolは次の属性とメソッドを持ちます。

- `name`: Tool名
- `description`: LLMに渡す説明
- `parameters`: JSON Schema形式の引数定義
- `requires_confirmation`: 実行前確認が必要か
- `run(context, **kwargs)`: Tool実行本体

初期Core Toolは以下です。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `get_datetime` | 現在日時を返す | 不要 |
| `list_dir` | ワークスペース内のディレクトリ一覧 | 不要 |
| `read_file` | UTF-8テキストファイルを読む | 不要 |
| `write_file` | UTF-8テキストファイルを書く | 必要 |
| `search_files` | ワークスペース内の文字列検索 | 不要 |
| `run_powershell` | PowerShellコマンドを実行 | 必要 |

すべてのファイル操作は `LITTLE_AGENT_WORKSPACE` の内側に制限されます。

### Skill Script Tool仕様

Skillは `tools.json` と `scripts/` を持つことで、coreに実装を追加せずにToolを提供できます。

```text
skills/<skill_name>/
  SKILL.md
  tools.json
  scripts/
    tool_script.py
```

`tools.json` はTool名、説明、JSON Schema形式の引数、確認要否、実行スクリプトを定義します。Agent起動時に `skills/*/tools.json` が読み込まれ、各Toolは `ScriptSkillTool` 経由で実行されます。

スクリプトには標準入力で以下のJSONが渡されます。

```json
{
  "tool": "tool_name",
  "workspace": "C:/path/to/workspace",
  "arguments": {}
}
```

スクリプトは標準出力に以下のJSONを返します。

```json
{
  "ok": true,
  "content": "result text"
}
```

この構成にすると、`skills/<skill_name>` フォルダをコピーするだけでSkillの説明、Tool定義、実行ロジックをまとめて移動できます。

### タスク管理仕様

タスク管理は `skills/task_manager` のポータブルSkillとして実装されています。core側にはタスク管理のロジックを持たせず、以下のSkill Script Toolを `skills/task_manager/tools.json` から読み込みます。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `add_task` | タスクを `data/tasks.json` に追加 | 不要 |
| `list_tasks` | タスク一覧を表示 | 不要 |
| `complete_task` | タスクを完了にする | 不要 |
| `delete_task` | タスクを削除 | 必要 |

タスクは `data/tasks.json` に保存されます。保存される主なフィールドは以下です。

- `id`: 8桁のタスクID
- `title`: タスク名
- `status`: `open` または `done`
- `created_at`: 作成日時
- `completed_at`: 完了日時
- `due`: 期限または期限メモ
- `priority`: 優先度
- `notes`: 補足メモ

削除は元に戻せないため、`delete_task` のみ実行前確認が必要です。

### PowerShell実行の安全仕様

`run_powershell` はワークスペースをカレントディレクトリとして実行します。以下のような破壊的・危険なトークンを含むコマンドは簡易ガードでブロックします。

- `Remove-Item`
- `rm`
- `rmdir`
- `del`
- `Format-Volume`
- `Stop-Computer`
- `Restart-Computer`
- `Set-ExecutionPolicy`

このガードは完全なサンドボックスではありません。実用化する場合は、許可コマンド方式、実行ログ、権限分離、プロセス隔離を追加してください。

### Agent Skill仕様

Agent Skillは `skills/<skill_name>/SKILL.md` に配置します。

形式:

```markdown
# skill_name

## Description
Skillの説明。

## When to use
- 発動条件

## Allowed tools
- 使用してよいTool名

## Instructions
Agentが従う手順や注意点。
```

初期Skillは以下です。

- `file_manager`: ファイル探索・読み書き・検索
- `python_coder`: Python実装・調査・実行確認
- `windows_operator`: Windows/PowerShell操作
- `task_manager`: タスク、TODO、やることリストの管理
- `skill_creator`: ポータブルSkillの作成、雛形生成、簡易検証
- `excel_file`: `.xlsx` の読み取りと簡易作成
- `ppt_file`: `.pptx` のテキスト読み取りと簡易作成
- `datetime`: 現在日時・曜日・タイムゾーンの取得
- `git`: gitリポジトリの状態確認・差分・コミット
- `screen_capture`: PC画面のスクリーンショット取得とVisionによる画面説明

Skill選択は軽量なキーワードスコアリングです。将来的にはembedding検索やLLMによるSkill routingに差し替えられます。

## セットアップ

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
Copy-Item .env.example .env
```

OpenAI互換APIを使う場合は `.env` に設定します。

```text
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
LITTLE_AGENT_MODEL=gpt-4.1-mini
LITTLE_AGENT_WORKSPACE=.
LITTLE_AGENT_REQUIRE_CONFIRMATION=true
LITTLE_AGENT_MAX_TOOL_STEPS=5
LITTLE_AGENT_ENABLE_LOGGING=true
LITTLE_AGENT_LOG_DIR=logs
```

## 起動

```powershell
python main.py
```

または editable install 後:

```powershell
little-agent
```

終了:

```text
/exit
```

## 使用例

```text
> 今の日時を教えて
> ファイル一覧を見せて
> README.mdを読んで要約して
> PowerShellでPythonのバージョンを確認して
> 明日までに請求書を確認するタスクを追加して
> 未完了タスクを見せて
> reports/sales.xlsx の中身を読んで
> 箇条書きから deck/plan.pptx を作って
```

`write_file` と `run_powershell` は実行前に確認プロンプトが出ます。

### ログとToken集計

通常起動では `LITTLE_AGENT_ENABLE_LOGGING=true` により、セッション単位のJSONLログを保存します。

保存先:

```text
logs/
  conversations/<session_id>.jsonl
  tools/<session_id>.jsonl
  usage/<session_id>.jsonl
```

記録内容:

- `conversations`: ユーザー入力、LLM応答、最終回答、LLMエラー
- `tools`: Tool名、引数、実行結果、キャンセル、エラー
- `usage`: LLM呼び出しごとのtoken使用量と累計

token数は、OpenAI互換APIが `usage.prompt_tokens`、`usage.completion_tokens`、`usage.total_tokens` を返す場合はその値を記録します。`usage` が返らない互換APIやローカルフォールバックでは、文字数から概算した値を `estimated: true` として記録します。

### Skill作成支援

`skills/skill_creator` は、Little Agent用Skillの作成を支援するポータブルSkillです。

提供Tool:

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `create_skill` | `skills/<name>` にSkill雛形を作成 | 必要 |
| `validate_skill` | Skill構成を簡易検証 | 不要 |

例:

```text
> メール整理用のSkillを作って。scriptsとtools.jsonも含めて
> skill_creatorで task_manager を検証して
```

### OfficeファイルSkill

`skills/excel_file` と `skills/ppt_file` は、外部ライブラリなしでOffice Open XMLファイルを扱うポータブルSkillです。

Excel用Tool:

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `read_excel` | `.xlsx` のセル値をテキストとして抽出 | 不要 |
| `write_excel` | 行列データから単一シート `.xlsx` を作成 | 必要 |

PowerPoint用Tool:

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `read_ppt` | `.pptx` のスライドテキストを抽出 | 不要 |
| `write_ppt` | タイトルと箇条書きから簡易 `.pptx` を作成 | 必要 |

制限:

- `.xls` と `.ppt` は非対応です。
- 既存ファイルの高度な編集ではなく、読み取りと簡易新規作成が中心です。
- 書式、画像、グラフ、アニメーション、ノート、複雑なレイアウトの完全再現は対象外です。

### 画面取得Skill

`skills/screen_capture` は、PC画面のスクリーンショット取得と、Vision対応モデルによる画面内容の説明を行うSkillです。画面の「取得」専用で、マウス/キーボード操作は行いません。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `take_screenshot` | 画面をキャプチャしPNG保存 | 不要 |
| `describe_screen` | 画面をキャプチャしVision APIで内容を説明 | 不要 |

セットアップ:

```powershell
pip install mss Pillow
```

`describe_screen` は `OPENAI_API_KEY` を使ってVision対応モデルを呼び出します。使用モデルは `LITTLE_AGENT_VISION_MODEL`（既定は `LITTLE_AGENT_MODEL`）で指定できます。

例:

```text
> スクリーンショットを撮って
> 今の画面に何が映っているか説明して
```

制限:

- GUIセッションが必要です。ヘッドレス環境ではキャプチャに失敗します。
- 画面はVision送信前に最大幅1568pxへ縮小されます。

## テスト

```powershell
python -m pytest -q
```

## 拡張方法

### Toolを追加する

1. `little_agent/tools` にToolクラスを追加
2. `name`, `description`, `parameters`, `requires_confirmation`, `run()` を実装
3. `little_agent/tools/__init__.py` の `default_tools()` に登録

### Skillを追加する

1. `skills/<new_skill>/SKILL.md` を作成
2. `Description`, `When to use`, `Allowed tools`, `Instructions` を書く
3. CLIを再起動

## 現在の制限

- Tool実行後にLLMへ戻すmulti-step loopは最小構成です
- PowerShell安全ガードは簡易的です
- 長期記憶は未実装です
- 複数エージェントの協調は未実装です
- Web検索Toolは抽象化候補として未実装です

## 次の実装候補

- Tool実行後にLLMへ結果を戻すmulti-step loop
- 会話ログの保存
- Skillのembedding検索
- 許可制PowerShell runner
- GUIまたはWeb UI
- 複数agentのrole分担
