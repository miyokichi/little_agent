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

### ワークフロー管理仕様（AI+人間統合）

`skills/workflow` は、ゴールを **AIタスクと人間タスクの依存関係付きワークフロー（DAG）** として管理するポータブルSkillです。task_manager が単発TODO向けなのに対し、workflow は「AIが下書き → 人間がレビュー → AIが送信」のような複数タスクの進行管理に使います。

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `create_workflow` | タスクDAGを一括登録（依存参照は呼び出し内の一時キー） | 不要 |
| `add_workflow_task` | 既存ワークフローにタスクを追加 | 不要 |
| `update_task_status` | タスクの状態と結果を記録 | 不要 |
| `show_workflow` | 詳細と着手可能（READY）タスクを表示 | 不要 |
| `list_workflows` | 一覧と進捗を表示 | 不要 |
| `delete_workflow` | ワークフローを削除 | 必要 |
| `open_workflow_viewer` | ブラウザのビューアを起動 | 不要 |

データは `data/workflows.json` に保存されます。主なフィールド:

- workflow: `id`（8桁）、`title`、`goal`、`status`（`active` / `done`。全タスク完了で自動的に `done`）
- task: `id`（8桁）、`title`、`description`、`assignee`（`ai` / `human`）、`status`（`pending` / `running` / `done` / `failed` / `skipped`）、`depends_on`（タスクIDの配列）、`result`（成果の要約）、`completed_via`（`agent` / `viewer`）

状態のルール:

- **ready（着手可能）**: `pending` かつ `depends_on` がすべて `done` または `skipped`。保存されず、表示時に導出されます。
- AIタスクは、エージェントが作業の前後で `running` → `done`（+ `result`）を記録します。失敗時は `failed` と理由を記録します。
- 人間タスクは承認ゲートとして機能します。ビューアの「完了にする」ボタンで完了させると、下流のタスクが ready になります。ビューアからの完了は「human かつ pending かつ ready」のタスクのみ許可されます。
- エージェントとビューアの同時書き込みは、`data/workflows.json.lock` による排他と一時ファイル + `os.replace` によるatomic writeで保護されます。

### ワークフロービューア

`data/workflows.json` をブラウザで可視化するローカルWeb UIです（標準ライブラリのみ、追加依存なし）。

起動方法は2つ:

```powershell
# 手動起動
python -m little_agent.viewer --workspace . --port 8765
```

```text
# または会話から
> ワークフローのビューアを開いて
```

- URL: `http://127.0.0.1:8765/`（`LITTLE_AGENT_VIEWER_PORT` で変更可）
- 表示: ワークフローのDAG図（Mermaid。ステータス色分け、AI=矩形 / 人間=平行四辺形、readyな人間タスクは琥珀色で強調）、「あなた待ち」パネル（readyな人間タスクと完了ボタン）、タスク詳細、全タスク一覧
- 約1.5秒間隔のポーリングで、エージェントの進捗がライブ反映されます
- バインドは `127.0.0.1` のみで、外部からはアクセスできません
- Mermaid はCDNから読み込みます。オフライン時は図の代わりに依存関係付きリスト表示へ自動フォールバックします（完了操作などの機能はオフラインでも動作します）
- 停止は、手動起動なら `Ctrl+C`。会話から起動した場合はバックグラウンド常駐なので、タスクマネージャで該当の `python` プロセスを終了します

注意: APIキーなしのローカルフォールバック（LocalRuleClient）はworkflow系Toolを呼ばないため、このSkillはOpenAI互換APIの設定時のみ実質的に使えます。

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
- `workflow`: AIタスクと人間タスクを依存関係付きワークフロー（DAG）として管理・可視化
- `skill_creator`: ポータブルSkillの作成、雛形生成、簡易検証
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
> 新製品発表の準備をワークフローにして。私のレビューを間に挟んで
> ワークフローのビューアを開いて
> 資料収集が終わったので done にして
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
- 停止ホットキーには `pynput` が必要です（`pip install pynput`）。未導入でも failsafe と `Ctrl+C` は使えます。

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
- 複数エージェントの協調は未実装です
- Web検索Toolは抽象化候補として未実装です

## 次の実装候補

- Tool実行後にLLMへ結果を戻すmulti-step loop
- 会話ログの保存
- Skillのembedding検索
- 許可制PowerShell runner
- GUIまたはWeb UI（ワークフロービューアは実装済み。汎用の会話UIは未実装）
- readyなAIタスクをエージェントが順に自動実行するワークフローのオーケストレーション
- 複数agentのrole分担
