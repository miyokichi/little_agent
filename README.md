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

画像をモデルに渡したい場合は、`images` にdata URIの配列を含めます（任意）。

```json
{
  "ok": true,
  "content": "Captured screen 1568x882.",
  "images": ["data:image/png;base64,..."]
}
```

この構成にすると、`skills/<skill_name>` フォルダをコピーするだけでSkillの説明、Tool定義、実行ロジックをまとめて移動できます。

### プロジェクト/タスク管理仕様（AI+人間統合）

`skills/project_manager` は、単発TODOから **AIタスクと人間タスクの依存関係付きワークフロー（DAG）** まで、プロジェクト単位で一元管理するポータブルSkillです。旧 `task_manager`（単発TODO）と旧 `workflow`（DAG）を統合したもので、プロジェクトやタスクの設定は **AI（本Skillのツール）と人間（ビューア）の両方** から行えます。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `create_project` | プロジェクトを作成（タスクDAGの一括登録も可） | 不要 |
| `add_task` | タスクを追加（`project_id` なしなら Inbox へ） | 不要 |
| `update_task` | タスクの内容を編集（タイトル、担当、期限、優先度、依存） | 不要 |
| `update_task_status` | タスクの状態と結果を記録 | 不要 |
| `add_task_comment` | タスクに進捗コメントを追記 | 不要 |
| `show_project` | 詳細と着手可能（READY）タスクを表示 | 不要 |
| `list_projects` | プロジェクト一覧と進捗を表示 | 不要 |
| `list_tasks` | 全プロジェクト横断のタスク一覧 | 不要 |
| `delete_task` | タスクを削除（他タスクの依存参照も除去） | 必要 |
| `delete_project` | プロジェクトを削除 | 必要 |
| `open_project_viewer` | ブラウザのビューア/エディタを起動 | 不要 |

データは `data/projects.json` に保存されます。主なフィールド:

- project: `id`（8桁）、`title`、`goal`、`status`（`active` / `done`。全タスク完了で自動的に `done`）、`inbox`（単発TODO受け皿フラグ）
- task: `id`（8桁）、`title`、`description`、`assignee`（`ai` / `human`）、`assignee_name`（human タスクの担当者名。例: 山田さん。表示用で ai には付かない）、`status`（`pending` / `running` / `done` / `failed` / `skipped`）、`depends_on`（タスクIDの配列）、`due`、`priority`、`result`（成果の要約）、`comments`（進捗コメントの配列 `{at, via, text}`）、`created_via` / `completed_via`（`agent` / `viewer`。誰が作成/完了したか）

状態のルール:

- **ready（着手可能）**: `pending` かつ `depends_on` がすべて `done` または `skipped`。保存されず、表示時に導出されます。
- AIタスクは、エージェントが作業の前後で `running` → `done`（+ `result`）を記録します。失敗時は `failed` と理由を記録します。
- 人間タスクは承認ゲートとして機能します。ビューアの「完了にする」ボタンで完了させると、下流のタスクが ready になります。
- 依存関係の編集はAI/人間どちらの経路でも循環（サイクル）を検証して拒否します。
- エージェントとビューアの同時書き込みは、`data/projects.json.lock` による排他と一時ファイル + `os.replace` によるatomic writeで保護されます。

旧データからの移行: 初回アクセス時に `data/workflows.json`（→ 各プロジェクト）と `data/tasks.json`（→ Inbox プロジェクト）を `data/projects.json` へ自動変換します。旧ファイルは `.bak` にリネームして退避します。

### プロジェクトビューア（人間側の編集UI）

`data/projects.json` をブラウザで可視化・編集するローカルWeb UIです（標準ライブラリのみ、追加依存なし）。人間はここからプロジェクトとタスクをフル操作（CRUD）できます。

起動方法は2つ:

```powershell
# 手動起動
python -m little_agent.viewer --workspace . --port 8765
```

```text
# または会話から
> プロジェクトのビューアを開いて
```

- URL: `http://127.0.0.1:8765/`（`LITTLE_AGENT_VIEWER_PORT` で変更可）
- 表示: タスクのDAG図（Mermaid。ステータス色分け、AI=矩形 / 人間=平行四辺形、readyな人間タスクは琥珀色で強調）、「あなた待ち」パネル（readyな人間タスクと完了ボタン）、タスク詳細、全タスク一覧
- 編集: 「＋プロジェクト」でプロジェクト作成、「＋タスク」でタスク追加、行のダブルクリック（または詳細の「編集」）でタスク編集ダイアログ（タイトル、説明、担当、担当者名、状態、期限、優先度、依存タスクのチェックボックス）、タスク削除、プロジェクト削除
- 担当者名: human タスクは「担当者名（任意）」に名前（例: 山田さん）を入れると、一覧・図・詳細で「👤 山田さん」と表示されます（担当を AI にすると名前欄は隠れ、値もクリアされます）
- 進捗コメント: タスク詳細ペインにコメント欄があり、人間（`via=viewer`）とエージェント（`add_task_comment`, `via=agent`）が時系列でメモを残せます。ポーリング再描画中も入力中の下書きは保持されます
- ビューアからの変更は `created_via` / `completed_via` が `viewer` として記録され、エージェント側は次のターンで `show_project` により最新状態を把握します
- 約1.5秒間隔のポーリングで、エージェントの進捗がライブ反映されます
- バインドは `127.0.0.1` のみで、外部からはアクセスできません
- Mermaid はCDNから読み込みます。オフライン時は図の代わりに依存関係付きリスト表示へ自動フォールバックします（編集機能はオフラインでも動作します）
- 停止は、手動起動なら `Ctrl+C`。会話から起動した場合はバックグラウンド常駐なので、タスクマネージャで該当の `python` プロセスを終了します

### ファイルシステム型タスクハーネス（人間×AI）

`skills/workspace_harness` は、`project_manager` の JSON DB とは別方向の、**ディレクトリ構造そのものをタスクの真実の源にする**ハーネスです。タスク1つ=1フォルダ（`task.md` を持つ）として扱い、人間とAIが同じフォルダ上で協働します。両者は併存でき、フォルダを直接見ながら進めたい運用にはこちらが向きます。

ディレクトリ規約:

```text
tasks/                        # ハーネスroot（LITTLE_AGENT_TASKS_DIR で変更可）
  <area>/                     # 上位エリア（区分）。★人間が統制する領域★
    <task-slug>/              # タスク1つ = 1フォルダ
      task.md                 # 定義（front matter + 目的 + 進捗ログ）
      outputs/                # 成果物
      notes/                  # 作業メモ
  PROPOSALS.md                # AIが提案した新エリア（人間が判断して作る）
shared/                       # 共通資料（LITTLE_AGENT_SHARED_DIR で変更可）
```

役割分担の要点:

- **上位エリアは人間が統制**します。AIはエリアを作成・改名・削除しません。既存エリアが合わないときは `propose_area` が `tasks/PROPOSALS.md` に提案を記録するだけで、フォルダは人間が作ります。
- **タスクフォルダはAIも作れます**。既存エリア内なら `create_task_folder` で直接作成（実行前確認あり）。承認前提の提案タスクは `status: proposed` で作り、人間がOKしたら `todo` に上げます。
- **共通資料はAIが自律探索**します。タスク着手時に `search_shared` が `shared/` をファイル名・本文横断で検索し、`read_shared` で確認、`update_task_folder` の `materials_add` で task.md に紐づけます。
- `task.md` は人間もAIも編集でき、`status`（`todo`/`doing`/`review`/`blocked`/`done`/`cancelled`/`proposed`）や進捗ログを両者が読み書きして協働します。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `harness_overview` | エリア一覧・各タスクの状態・共通資料・提案を俯瞰 | 不要 |
| `list_task_folders` | タスクフォルダを一覧（area/status で絞り込み） | 不要 |
| `read_task_folder` | task.md とフォルダ内容を表示 | 不要 |
| `create_task_folder` | 既存エリア内にタスクフォルダを作成（新エリアは拒否） | 必要 |
| `update_task_folder` | status/担当/期限/tags/材料リンクを更新 | 不要 |
| `add_task_note` | task.md の進捗ログに追記 | 不要 |
| `propose_area` | 新しい上位エリアを提案として記録（作成はしない） | 不要 |
| `search_shared` | 共通資料フォルダを自律検索 | 不要 |
| `read_shared` | 共通資料のテキストファイルを読む | 不要 |

保存先は `LITTLE_AGENT_TASKS_DIR`（既定 `tasks`）と `LITTLE_AGENT_SHARED_DIR`（既定 `shared`。ワークスペース外の絶対パスも可）で変更できます。同梱の `tasks/README.md` と `tasks/example/` が規約の見本です。

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
- `project_manager`: プロジェクト/タスクの統合管理（単発TODOから依存関係付きDAGまで、AI/人間の両方から編集可能）
- `workspace_harness`: ファイルシステム型タスクハーネス（タスク=フォルダ。上位エリアは人間が統制、AIはタスク作成/提案と共通資料の自律探索）
- `skill_creator`: ポータブルSkillの作成、雛形生成、簡易検証
- `agent_manager`: エージェントプロファイル（使うスキル・ツールの構成）の作成・管理
- `excel_file`: `.xlsx` の読み取りと簡易作成
- `ppt_file`: `.pptx` のテキスト読み取りと簡易作成
- `datetime`: 現在日時・曜日・タイムゾーンの取得
- `git`: gitリポジトリの状態確認・差分・コミット
- `screen_capture`: PC画面のスクリーンショット取得とVisionによる画面説明
- `computer_use`: マウス/キーボードによるPC操作（クリック、文字入力、キー操作）

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
LITTLE_AGENT_TASKS_DIR=tasks
LITTLE_AGENT_SHARED_DIR=shared
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

## スラッシュコマンド

`>` プロンプトで `/` から始まる入力は **スラッシュコマンド** として扱われます。コマンドは2種類あります。

| 種別 | 実行者 | LLM呼び出し | 用途 | 場所 |
| --- | --- | --- | --- | --- |
| 制御コマンド（built-in） | CLIが直接 | しない | セッション操作・状態確認 | core（`little_agent/commands.py`） |
| ユーザー定義コマンド（custom） | 展開して `agent.run()` へ | する | よく使うプロンプトの定型化 | `commands/*.md`（ポータブル） |

入力の判定:

- `/` 始まり → コマンドとして解釈
- `//` 始まり → 先頭の `/` を1つ外した本文をそのままLLMへ送る（エスケープ）
- それ以外 → 通常どおり `agent.run()`

ディスパッチ順序は「built-in → custom → どちらも無ければ `Unknown command`」です。未知コマンドはLLMに送られません（タイプミスで無駄なAPI消費や誤動作を起こさないため）。

### 制御コマンド（built-in）

| コマンド | エイリアス | 動作 |
| --- | --- | --- |
| `/help` | `/?` | 全コマンド（built-in + custom）を説明付きで一覧 |
| `/exit` | `/quit` | セッション終了 |
| `/clear` | | 会話メモリをリセットして新規コンテキストにする |
| `/skills` | | 読み込み済みSkillを一覧（`/skills <name>` で詳細） |
| `/tools` | | 登録済みToolを一覧 |
| `/memory` | | workspace/global メモリの現在値を表示 |
| `/usage` | | 当セッションのtoken累計を表示 |
| `/config` | | model・workspace・確認要否などの設定を表示 |
| `/reload` | | `commands/*.md` を再読込（再起動不要。Skillは毎ターン自動再読込） |
| `/agents` | | エージェントプロファイルを一覧（アクティブは `*`） |
| `/agent` | | アクティブなエージェントを表示 / `/agent <name>` で切替（`/agent library` でライブラリ全体に戻す） |

### ユーザー定義コマンド（custom）

`commands/<name>.md` を置くと `/name` で使えます。探索先は2か所で、名前が衝突した場合はプロジェクトが優先されます。

- プロジェクト: `{LITTLE_AGENT_WORKSPACE}/commands/`（`LITTLE_AGENT_COMMANDS_DIR` で変更可）
- グローバル: `~/.little_agent/commands/`（`LITTLE_AGENT_GLOBAL_COMMANDS_DIR` で変更可）

形式（frontmatter + 本文）:

```markdown
---
description: 指定ファイルをレビューして改善点を優先度付きで挙げる
---
次のファイルを読んでコードレビューして。
対象: $ARGUMENTS
```

引数展開:

- `$ARGUMENTS` … コマンド名以降の全文字列
- `$1` `$2` … 空白区切りの位置引数（不足分は空文字）
- プレースホルダが1つも無く引数がある場合 … 本文末尾に引数を追記

`/review little_agent/agent.py` のように呼ぶと、テンプレートを展開した本文が `agent.run()` に渡され、Skill選択・Tool・確認プロンプトは通常ターンと同じに効きます（＝プロンプトのショートカット）。

同梱の例: `commands/review.md`、`commands/plan.md`、`commands/commit.md`。

```text
> /help
> /skills project_manager
> /review little_agent/agent.py
> /plan 新製品発表の準備
```

## エージェントの管理（プロファイル）

用途ごとに **使うスキルとツールを絞ったエージェント** を定義して切り替えられます。全スキルは1か所の **ライブラリ**（`skills/`）に置き、エージェントを構成するときに選んだスキルフォルダをそこから **コピー** します（スナップショット）。

モデルは1つに統一されています: **起動時は必ず1つのエージェントを動かす**という考え方で、ライブラリ全体（全スキル・全Tool）は組み込みの **`default` エージェント**として扱われます。何も指定しなければ `default` で動き、`agents/<name>/` に作った専用エージェントを選ぶと、そのエージェントに切り替わります。

### 構造

```text
skills/                     # ライブラリ: 全スキルのマスター
agents/                     # 各エージェント（.gitignore 対象。ユーザーデータ）
  <name>/
    agent.json              # プロファイル（説明・model・core_tools・上書き設定）
    skills/                 # ライブラリからコピーしたスキルフォルダ
```

有効なスキルは `agents/<name>/skills/` に存在するフォルダそのものです（唯一の真実の源）。コピーしたスキル由来のToolは自動で有効になり、コアTool（`read_file`, `run_powershell` など本体組み込み）は `agent.json` の `core_tools` 許可リストで絞れます（未指定なら全コアTool有効）。メモリToolは常に有効です。

`agent.json` の例（`null` は env/既定にフォールバック）:

```json
{
  "name": "office",
  "description": "Excel/PowerPoint 作業用",
  "model": null,
  "core_tools": ["read_file", "write_file", "list_dir"],
  "max_tool_steps": null,
  "require_confirmation": null
}
```

### 実行時の選択と切替

- 起動時に選択: `little-agent --agent office`（または env `LITTLE_AGENT_AGENT`）
- 指定なしなら組み込みの `default`（ライブラリ全体）で起動。`agents/` にプロファイルがある場合は起動時に選択メニューを表示（`0` = `default`）
- セッション中の切替: `/agents` で一覧（`default` ＋ 作成したエージェント。アクティブは `*`）、`/agent <name>` で切替、`/agent default` でライブラリ全体へ。切替時は会話コンテキストが新しくなります
- `default`（および `library` / `none`）は組み込みエージェント用の予約名で、この名前ではエージェントを作成できません

### 作成・構成（agent_manager スキル）

会話からの作成・管理は `skills/agent_manager` が担当します。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `create_agent` | ライブラリからスキルをコピーしてエージェントを作成 | 必要 |
| `list_agents` | エージェント一覧 | 不要 |
| `show_agent` | 1エージェントの詳細（スキル・ツール・model） | 不要 |
| `add_agent_skill` | 既存エージェントにスキルを1つ追加コピー | 不要 |
| `remove_agent_skill` | エージェントからスキルを削除 | 必要 |
| `set_agent_core_tools` | コアTool許可リストを設定（省略で全部） | 不要 |
| `delete_agent` | エージェントを削除 | 必要 |

例:

```text
> Excel と PowerPoint だけ使える office というエージェントを作って
> office に file_manager スキルを追加して
> エージェント一覧を見せて
```

ディスク上の変更が実行中のエージェントに反映されるのは、次回起動時か `/agent <name>` での切替時です。設定は env（`LITTLE_AGENT_SKILL_LIBRARY_DIR` / `LITTLE_AGENT_AGENTS_DIR` / `LITTLE_AGENT_AGENT`）で変更できます。

### サブエージェントへの委譲（delegate_task）

実行中のエージェントが、**その場で新しいエージェントを立ち上げてサブタスクを丸ごと委譲**できます（プロセス内。agent-to-agentプロトコルではありません）。委譲されたサブエージェントは**まっさらなコンテキスト**で自律的に走り、最終結果だけを親に返します。長い調査や独立した作業を親の会話コンテキストから切り離したいとき、特定のエージェントプロファイルに専門作業を任せたいときに使います。

`delegate_task` は組み込みのコアToolで、`build_agent` が起動時に自動登録します（`/tools` に出ます）。

| 引数 | 内容 |
| --- | --- |
| `task` | サブエージェントへの完結した指示（会話履歴は引き継がれないので必要な情報を全部入れる） |
| `agent` | 任意。走らせるエージェントプロファイル名（`agents/` 配下）。省略すると `default`（ライブラリ全体） |
| `background` | 任意。作業前に渡す前提・素材 |

動作と安全:

- サブエージェントは自分専用の会話メモリ・ログ（`logs/` に別セッションIDで記録）を持ちます。親の会話は汚れません。
- サブエージェントの危険Tool（`write_file`, `run_powershell` など）は、サブエージェント側で改めて確認プロンプトが出ます（承認は親と共有しません）。
- 緊急停止ホットキーは親が一括管理し、押すとネストした委譲ごと中断します。
- 無限委譲を防ぐため深さ制限があります。`LITTLE_AGENT_MAX_DELEGATION_DEPTH`（既定 `2`、`0` で委譲を無効化）で調整します。

例:

```text
> 競合3社の最新プレスリリースを調べて要点をまとめる作業を、サブエージェントに任せて
> このExcelの集計は office エージェントに委譲してやって
```

## 使用例

```text
> 今の日時を教えて
> ファイル一覧を見せて
> README.mdを読んで要約して
> PowerShellでPythonのバージョンを確認して
> 明日までに請求書を確認するタスクを追加して
> 未完了タスクを見せて
> 新製品発表の準備をプロジェクトにして。私のレビューを間に挟んで
> プロジェクトのビューアを開いて
> 資料収集が終わったので done にして
> t2の期限を金曜にして、担当を私に変えて
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

### メモリと自動学習

Little Agent には2種類の永続メモリがあります。

**手動メモリ（memory.md）** — LLMが `update_workspace_memory` / `update_global_memory` ツールで明示的に書き込みます。

| ファイル | 範囲 |
| --- | --- |
| `{workspace}/memory.md` | そのワークスペース専用 |
| `~/.little_agent/memory.md`（`LITTLE_AGENT_GLOBAL_MEMORY_PATH`） | 全ワークスペース共通 |

**自動学習プロファイル（profile.md）** — セッションの内容（依頼・成果・傾向）から、恒久的に再利用できる知識を自動抽出して蓄積します。追記ではなく毎回マージ・要約するため、無制限には肥大しません。

| ファイル | 範囲 | 内容 |
| --- | --- | --- |
| `{workspace}/profile.md` | ワークスペース | プロジェクト固有のエッセンス・決定・慣習 |
| `~/.little_agent/profile.md`（`LITTLE_AGENT_GLOBAL_PROFILE_PATH`） | 全体 | ユーザーの好み・作業スタイル・繰り返す意図 |

memory.md と profile.md はいずれも次回以降のsystem promptに自動で差し込まれます。

抽出のタイミング（ハイブリッド）:

- セッション終了時（`/exit` や Ctrl+C）に自動実行
- `/remember` コマンドで任意のタイミングに手動実行

抽出はLLMを使うため、`OPENAI_API_KEY` が設定されているときのみ動作します（ローカルフォールバック時はスキップ）。`LITTLE_AGENT_AUTO_LEARNING=false` で無効化できます。保存は確認なしの自動です。

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
> skill_creatorで project_manager を検証して
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

### 画面取得Skill（マルチモーダル版）

`skills/screen_capture` は、PC画面のスクリーンショットを取得し、その画像を**本体のLLM（Vision対応モデル）に直接渡す**Skillです。画面の「取得」専用で、マウス/キーボード操作は行いません。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `take_screenshot` | 画面をキャプチャし、画像をモデルに渡す（任意でPNG保存） | 不要 |

セットアップ:

```powershell
pip install mss Pillow
```

このSkillはVision APIを別途呼ばず、取得画像を本体の会話に画像メッセージとして差し込みます。`LITTLE_AGENT_MODEL` にVision対応モデルを設定してください。

例:

```text
> スクリーンショットを撮って今の画面を説明して
> 画面の左上に何があるか見て
```

制限:

- GUIセッションが必要です。ヘッドレス環境ではキャプチャに失敗します。
- 画像はモデル送信前に最大幅1568pxへ縮小されます。

#### マルチモーダル対応の仕組み

このブランチでは、Tool結果として画像を返せるようcore側を拡張しています。

- `ToolResult` に `images`（data URIのタプル）を追加
- `Message.content` を文字列に加えて content-block のリストでも持てるよう拡張
- Tool実行後、`tool` メッセージ（テキスト）に続けて、画像を `image_url` ブロックとして持つ `user` メッセージを追加してモデルに渡す（OpenAI形式では `tool` メッセージに画像を入れられないため）
- Skill Script Toolは標準出力JSONに `images` 配列（data URI）を返すことで画像を渡せる

### PC操作Skill（computer_use）

`skills/computer_use` は、マウスとキーボードで実機を操作するSkillです。`screen_capture` で画面を「見て」、このSkillで「操作」します（見る→操作→見る のループ）。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `get_screen_info` | 実画面解像度とカーソル位置を返す | 不要 |
| `move_mouse` | カーソルを指定座標へ移動 | 不要 |
| `click` | クリック（left/right/middle、複数回、座標指定可） | 必要 |
| `type_text` | 文字列をキーボード入力 | 必要 |
| `press_keys` | キー/ホットキー（`enter`、`ctrl+c` 等） | 必要 |

セットアップ:

```powershell
pip install pyautogui
```

座標の扱い:

- 座標は **実画面ピクセル** で渡します。
- `take_screenshot` の画像が縮小されている場合（例: `Captured screen 1568x882`）、その画像上で読み取った座標に `image_size`（例 `[1568, 882]`）を添えて渡すと、**ツール側が実解像度へ自動変換**します（モデルに座標計算をさせません）。

#### 承認と緊急停止

AIのマウス/キーボード操作とユーザーの操作が同じカーソルを奪い合わないよう、承認は次の方式です。

- **セッション一括承認**: 確認が必要なTool（`click` / `type_text` / `press_keys` など）を最初に実行するとき一度だけ確認します。承認すると **そのセッション中は再確認なし** で連続実行します。これにより「ターミナルを前面化して `y/N` を打つためのマウス操作」が不要になり、AI操作との干渉が消えます。
- **緊急停止ホットキー**: AIが操作している間だけ有効なグローバルホットキー（既定 `Ctrl+Alt+Q`、`LITTLE_AGENT_STOP_HOTKEY` で変更可）。押すとTool実行の合間で中断し、`>` プロンプトに戻ります。プロンプト待機中はリスナーを張らないので普段の操作には干渉しません。
- **即時停止(failsafe)**: マウスを画面の隅へ動かすと、操作の途中でも pyautogui が即アボートします。
- 停止ホットキーは `pynput` を使います。これは中核の安全機能なので標準の依存に含まれ、`pip install -e .` で入ります。何らかの理由で使えない環境でも failsafe と `Ctrl+C` は使えます。

制限・安全:

- GUIセッションが必要です。ヘッドレス環境では操作できません。
- 日本語など非ASCIIは `type_text` で正しく入らないことがあります。

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
- 複数エージェントの協調は `delegate_task` によるサブエージェント委譲まで（同時並列実行やメッセージ往復はなく、逐次委譲）
- Web検索Toolは抽象化候補として未実装です

## 次の実装候補

- Tool実行後にLLMへ結果を戻すmulti-step loop
- 会話ログの保存
- Skillのembedding検索
- 許可制PowerShell runner
- GUIまたはWeb UI（プロジェクトビューアは実装済み。汎用の会話UIは未実装）
- readyなAIタスクをエージェントが順に自動実行するワークフローのオーケストレーション
- 複数agentのrole分担（`delegate_task` で逐次委譲は実装済み。並列実行やagent間メッセージ往復は今後）
