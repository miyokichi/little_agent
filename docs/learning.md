# Learning Engine連携

`little-agent chat --learning` は通常のAgent RuntimeとMCPクライアントを使い、
WorkCoordinatorが仕事の状態、中断、回答確認、再開、完了とレビューを管理します。
判断ルールやRDRの評価はLearning Engineの責務です。Pythonパッケージを直接importしません。

## 実装と接続の状況

little_agentのmain `c56d8fb` を基に実装しました。確認時の
[miyokichi/learning_engine](https://github.com/miyokichi/learning_engine) main
`59c441f` には、まだ `serve-mcp` / WorkSession API がありません。
**実サーバとの接続・業務判断は未検証です。以下は実装すべきMCP契約です。**
独立したstdioプロセスとテスト用SQLiteを使うモックで、通信と制御を検証しています。
モックはLearning Engine本体の代替実装ではありません。

## 起動設定

Learning Engine側のMCP実装後、workspaceの `mcp.json` に
[mcp.learning.example.json](../mcp.learning.example.json) の設定を追加します。
環境変数 `LEARNING_ENGINE_PYTHON` / `LEARNING_ENGINE_DB` に、実環境の絶対パスを指定してください。
`.env` の例（Windows）:

```dotenv
LEARNING_ENGINE_PYTHON=C:\LearningEngine\.venv\Scripts\python.exe
LEARNING_ENGINE_DB=C:\LearningEngine\learning.sqlite3
```

Linux/macOSならそれぞれ `/absolute/path/LearningEngine/.venv/bin/python` と
`/absolute/path/LearningEngine/learning.sqlite3` などに変更します。
LE側のPython環境にLEパッケージをインストールしておく必要があります。
接続はMcpManagerが開始・終了し、失敗後は `/mcp reconnect learning_engine` で再接続します。
`learning_engine` というサーバ名はCoordinator専用として予約されています。

```bash
little-agent chat --learning
```

目的をそのまま入力するとWorkSessionを開始します。通常モード `little-agent chat`
のChat / A2A / MCP / Skill / Memoryには従来の動作を維持します。
学習対応モードではファイルMemoryを読み書きせず、Memoryツールも登録しません。
知識・判断履歴はLEのSQLite、会話履歴はChatSession、再開用実行メタデータは
`data/work/checkpoint.json` に分離します。`--no-memory` はLEの保存を無効化する指定ではありません。

## 操作

| 操作 | 動作 |
|---|---|
| 目的を入力 / `/work start <目的>` | 仕事開始、承認済み知識取得、開始時の強制評価、Tool Loop実行 |
| `/work status` | work_id、状態、質問、最後・次の作業、操作IDを表示 |
| `/work pause` | ローカルの実行を止め、LEにも一時停止を通知 |
| `/work resume` | 保存状態を取得し、再開API・必須評価を通して再実行 |
| `/work finish` | 人間が目的達成を確定し、必須評価・完了・Reflectionを実行 |
| `/work review` | 日本語の学習候補を表示。対象ごとの一時トークンを発行 |
| `/work review approve <token>` | 表示した版の候補のみ承認 |
| `/work review edit <token> <修正文>` | 候補を修正。承認は別の新しいトークンで実行 |
| `/work review defer\|reject <token>` | 個別の保留・却下 |
| `/work list` | LEのWorkSession一覧 |
| `/work open <完了済みwork_id>` | 過去の完了済み仕事のレビューを開く |
| `/work chat <文章>` | 未完了の仕事がないとき、WorkSessionを作らず通常のAgentを実行 |

NEED_INPUT / REVIEW / BLOCK で、その場のTool Loopを中断します。
同じLLM応答の後続Tool Callも実行しません。READY_TO_RESUMEは実行許可ではなく、
再開APIを通す準備状態です。EXPLOREはLEが列挙したToolとホストの読み取り専用Toolの
共通範囲だけを許可します（初期値: `read_file`, `list_files`, `get_datetime`）。

質問がある間の次の入力は回答として原文をLEに送ります。
複数の質問は一つずつ回答・確認します。解釈を見て訂正する場合は再入力し、
正しければ `/work confirm <表示されたtoken>` を実行します。
`/work answer` で確認画面を再表示できます。再表示・訂正・再起動は古いトークンを無効にします。
回答確認は知識公開への同意ではありません。

`Agent.run()` の最終文章だけで仕事を完了しません。
`/work finish` は、実行結果を持つ作業記録があり、残作業・未解決質問・不明操作がなく、
LEの完了時評価がREADYの場合だけ成立します。目的達成の最終判断はこの人間操作が担います。
完了通知の応答だけが失われた場合も、同じ完了意図で `/work finish` を再実行します。
Reflectionに失敗した場合は再接続後 `/work review` で再試行できます。

## 安全性と再開

- Agent Coreには汎用の `RunHooks` と `RunInterrupted` のみ追加します。
  `check` / `before_tool` / `after_tool` で一回の実行を制御し、履歴の完全復元は要求しません。
- LLMには `work_evaluate` と `work_record_progress` だけを公開します。
  LEの生のMCPツール・リソース・初期化時の指示はLLMに公開しません。
  再接続・Tool一覧更新後も同じ境界です。
- 承認APIと回答確認APIはCLIの明示操作からだけ呼びます。
  トークンはメモリ内に保管し、LLMの再開コンテキストに含めません。
  候補IDと版を固定し、LE側も版を検査する必要があります。
- 初回・再開・完了と、読み取り専用と明示されたTool以外の実行前に評価を強制します。
  追加の必須ポイントは `WorkCoordinator(required_check=...)` で設定できます。
  LEの評価は業務上の判断であり、既存のTool実行確認も引き続き有効です。
- Tool名とJSON引数のSHA-256を操作IDとし、実行前に `in_flight` をfsyncして保存します。
  成功済みの同一操作は結果テキストを再利用します。同一work内の同一Tool・引数は一操作と扱います。
  文字列表現が異なるパスなど、意味的に同一の別引数まで自動判定はしません。
  画像自体は再実行用チェックポイントに保存しません。
- Tool失敗は部分的副作用の可能性があるため `uncertain` とし、後続Toolを止めます。
  再起動で `in_flight` が残った場合も自動再実行しません。
  `/work resolve <操作ID> done <実行を確認した根拠>` または
  `/work resolve <操作ID> retry <未実行を確認した根拠>` の人間操作が必要です。
  `retry` 後も `/work resume` と操作前の強制評価を通ります。
- チェックポイントは一workspaceにつき一つで、OSのファイルロックで学習Chatの同時起動を防ぎます。
  再起動後に実行可能状態を自動復活させません。Pythonの実行スタックやTool内の途中状態は復元しません。
  外部システムとの厳密なexactly-onceには外部Tool側の冪等性も必要です。
- 会話履歴には人間への表示を残しますが、仕事の再実行にはその履歴を渡しません。
  Reflectionの未承認候補や未確認回答を次の仕事の知識として混入させないためです。
  LEから取得した知識は全件 `status=approved` を検査し、違反時は停止します。
- 学習データを失わずに停止する設計ですが、任意のシェルToolなどにworkspaceへの書き込みを
  許す従来の信頼境界は維持します。これは悪意あるプラグインのOSサンドボックスではありません。

## MCP契約 v1

MCPの `tools/list` に以下のToolを公開します。
入力は表のフィールドを持つobject。更新系にはさらに文字列 `request_id` が必要です。
出力は `structuredContent` のobject、または `content` のtext内のJSON objectです。
`isError=true`、不正JSON、契約不一致、切断、タイムアウトでは仕事を停止します。

| Tool | 入力（request_id以外） | 必須の応答内容 |
|---|---|---|
| `le_start_work` | work_id, conversation_id, goal: string | work_id: string（要求と同一） |
| `le_get_approved_knowledge`（参照） | work_id, query: string | knowledge: [{id, text, status: "approved"}] |
| `le_evaluate` | work_id, target: string; facts: object | state; questions?: [{id,text}]; evidence?: string[]; allowed_tools?: string[] |
| `le_pause_work` | work_id: string; checkpoint: object | object |
| `le_record_answer` | work_id, question_id, answer: string | answer_id, interpretation: string |
| `le_confirm_answer` | work_id, answer_id: string | object（成功のみ） |
| `le_get_resume_context`（参照） | work_id: string | state; goal: string; questions?: array; context?: object |
| `le_resume_work` | work_id: string; checkpoint: object | object（再開許可成功のみ） |
| `le_record_progress` | work_id, unit_id, action, reason, result, state: string; evidence: string[]; tool_results: object | object |
| `le_finish_work` | work_id: string; completion: object | object |
| `le_reflect` | work_id: string | object |
| `le_get_review`（参照） | work_id: string | candidates: [{id, revision, kind, text, status}]（各値string） |
| `le_review_candidate` | work_id, candidate_id, revision, action, text: string | object |
| `le_list_work`（参照） | なし | WorkSession一覧を含むobject |

`state` は READY / NEED_INPUT / REVIEW / EXPLORE / BLOCK / READY_TO_RESUME。
再開コンテキストの `context` は `last_action`, `next_action`, `confirmed_answers` のみ使用し、
再利用知識は別途承認済みAPIから取得します。
`le_get_resume_context` は完了済み仕事に対して `state=FINISHED` も返せます。
質問・ブロックの解除はLE側が確認し、再開不可ならエラーまたは非実行可能状態を返す必要があります。

候補の `kind` は criterion / procedure / unresolved など。
`status=pending` または `deferred` の候補を表示して操作できます。
`action` は approve / edit / defer / reject。editは公開せず次の承認待ちに戻します。
LEは同時更新による版の競合を拒否し、approveのときだけ再利用知識として公開してください。

更新リクエストは操作種別ごとのローカル永続シーケンスと入力からIDを生成します。
応答が失われた場合の同一操作の再試行ではIDを保持し、新しい評価は新しいIDを使います。
LE側で `request_id` の同一要求を重複適用せず、同一ID・異なる入力を拒否してください。
さらに work_id の再開始、answer_idの再確認、unit_idの再記録、同一workの完了・Reflectionは
意味的にも冪等にする必要があります（正常応答直後のクライアント再起動を含む）。
この契約を満たさないサーバに対して安全な再試行は保証できません。

`work_record_progress` は全Toolの転送ではなく、LLMが意味のある単位をまとめる操作です。
`state` は executed / planned / hypothesis。executedは成功したTool結果のoperation_idを
一つ以上含む必要があり、Coordinatorが実際の結果を `tool_results` として添付します。
action / reason / result の要約自体はモデルが作るため、その妥当性はLEのレビュー対象です。
通常の詳細Toolログは既存RunLoggerが担当します。

## 検証

```bash
uv sync --extra dev
uv run pytest -q
uv run pytest -q tests/test_work.py
```

`tests/learning_fake_server.py` は別プロセスとして既存McpManagerから起動します。
テスト用SQLiteにAPI受信・作業・回答・候補・承認知識を保存し、切断・不正応答を注入できます。
Viaの調査→適用性判断→質問→原文記録→解釈確認→再開→架空の計算→作業記録→完了→
Reflection→人間承認→次の仕事で知識再利用を、実際のAgent Tool Loopで検証します。
CLIの入力ループ、復元、回答訂正・複数質問・重複質問、未承認知識拒否、LLM異常出力、
Tool二重実行・部分失敗、危険な再開、必須評価、レビュー版とトークン、MCPの応答喪失も対象です。
