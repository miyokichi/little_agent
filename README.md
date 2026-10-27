# Little Agent

Skill と Tool を LLM に与えて仕事を実行する、軽量な Agent Runtime です。

Runtime は**1本**で、その上に **Chat**（人と対話する）、**A2A**（外部システムから1件の仕事を受ける）、**MCP**（MCPホストからToolとして呼ばれる）という3つの入口が載ります。LLM・Tool Loop・Skill実行・Workspace・緊急停止・usage計測は、どの入口でも同じコードです。

```text
                         Agent Runtime
                （LLM / Tool Loop / Skill / Workspace
                  / MCP client / cancellation / usage）
                 /               |                \
              Chat              A2A               MCP
   little-agent chat   little-agent serve-a2a   little-agent serve-mcp
         │                       │                    │
 会話履歴あり／永続memory可   Taskごとに独立      呼び出しごとに独立
```

さらに Runtime 自身が **MCPクライアント**でもあり、`mcp.json` に書いたMCPサーバのTool・Resource・Promptをエージェントが使えます（[MCP](#mcpmodel-context-protocol)）。

構成は6つの領域に分かれます。

| 領域 | 場所 | 役割 |
| --- | --- | --- |
| **Core Runtime** | `little_agent/agent.py`, `factory.py` | 1回の実行。system prompt構築 → tool loop → 結果 |
| **Chat** | `little_agent/session.py`, `cli.py`, `commands.py` | 会話履歴を持ち、毎ターン Runtime に渡す |
| **A2A** | `little_agent/a2a/` | Agent Card・JSON-RPC・Task・workspace grant |
| **Memory** | `little_agent/memory/` | 永続memoryの抽象。Runtimeとは疎結合 |
| **Workspace** | `little_agent/tools/base.py`, `paths.py`, `a2a/grant.py` | 読み書きできる範囲の決定と検証 |
| **Skills** | `little_agent/skills/`（loader）, `little_agent/builtin_skills/`（実装） | [Agent Skills](https://agentskills.io/specification) 形式のSkillの探索・検証・段階的な読み込み。coreから分離 |
| **MCP** | `little_agent/mcp/` | MCPクライアント（外部サーバのTool/Resource/Prompt）と `serve-mcp` |

Runtime 自身は実行の間に何も持ち越しません。会話を続けたい側（Chat）が履歴を保持して渡し、独立させたい側（A2A）は何も渡しません。

## 実行環境

- OS: Windowsを主対象
- Python: 3.10以上
- Shell tool: PowerShell
- LLM: OpenAI互換 Chat Completions API
- HTTP client: Python標準ライブラリの `urllib`（OpenAI SDK不使用、A2Aサーバも `http.server`）
- APIキーがない場合: 簡易ローカルフォールバックで日時取得やディレクトリ一覧のみ試せます

### OpenAI互換API

`OPENAI_BASE_URL` と `OPENAI_API_KEY` を設定すると、OpenAI互換の `/chat/completions` APIに直接HTTPでアクセスします。

```text
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
LITTLE_AGENT_MODEL=gpt-4.1-mini
```

ローカル互換サーバ（LM Studio、LiteLLM proxy、OpenRouter など）も同じ設定で使えます。

```text
OPENAI_API_KEY=dummy
OPENAI_BASE_URL=http://localhost:1234/v1
LITTLE_AGENT_MODEL=local-model-name
```

注意: このAgentはTool実行のために `tools` / `tool_calls` を使います。互換API側がtool callingに対応していない場合、通常の返答はできてもTool実行は動かない可能性があります。

終了判定は Chat Completions の `choices[0].finish_reason` に従います。

- `stop`: 正常終了。
- `tool_calls`（旧 `function_call` も変換して対応）: ツール実行後に再問い合わせ。
- `length`: トークン上限による未完了としてエラーを表示。
- `content_filter`: フィルタによる停止としてエラーを表示。
- 終了理由の欠落・未知の値・ツール呼び出しとの矛盾: 不正な応答としてエラーを表示。

未完了・不正な応答に含まれるツールは実行しません。`LITTLE_AGENT_MAX_TOOL_STEPS` は引き続きループ上限です。独自の `LLMClient` も `finish_reason` を返す必要があります。

ツール未登録時は `tools` / `tool_choice` を送信しません。拒否応答の `refusal` は表示します。ツール引数は元の JSON 文字列を会話に保持し、実行前に JSON オブジェクトであることと対応するスキーマ項目を検証します。不正な引数は補修や `{}` への置換をせず、ツールエラーとしてモデルに返します。

仕様: [OpenAI Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

## 起動モード

```powershell
little-agent chat                                   # 対話。永続memoryあり
little-agent chat --no-memory                       # 対話。永続memoryなし
little-agent serve-a2a --agent observer --port 8801 # A2Aサーバとして公開
little-agent serve-mcp --agent observer             # MCPサーバ（stdio）として公開
```

引数なしの `little-agent` は `chat` として起動します（`python -m little_agent` / `python main.py` も同じ）。

| | 会話履歴 | 永続memory | 1回の実行の独立性 |
| --- | --- | --- | --- |
| `chat` | セッション中は保持 | `FileMemoryStore`（読み書きする） | 会話として連続 |
| `chat --no-memory` | セッション中は保持 | `NullMemoryStore`（一切触らない） | 会話として連続 |
| `serve-a2a` | 持たない | `NullMemoryStore`（一切触らない） | Taskごとに完全独立 |
| `serve-mcp` | 持たない | `NullMemoryStore`（一切触らない） | `tools/call` ごとに完全独立 |

`--no-memory` は「保存を我慢する」モードではありません。`NullMemoryStore` が入るため、memoryファイルは**開かれもせず**、`update_workspace_memory` / `update_global_memory` Tool はそもそも存在しません。A2Aサーバも同じで、誰かの永続memoryを読み書きすることはありません。

`chat` / `serve-a2a` 共通のオプション:

| オプション | 内容 |
| --- | --- |
| `--agent <name>` | 使うAgent Profile（省略時は `default`、chatでは対話選択） |
| `--readable-path <PATH>` | ワークスペース外で**読める**パスを追加（繰り返し可）。A2Aで参照用に渡せる範囲でもある |
| `--writable-path <PATH>` | ワークスペース外で**読み書きできる**パスを追加（繰り返し可）。A2Aで作業場所として渡せる範囲でもある |

`serve-a2a` のみ: `--host`、`--port`、`--auto-approve`、`--allow-any-path`。`serve-mcp` は `--auto-approve`、`--allow-any-path` を受け付けます。

## Agent execution

`agent.run()` の1回が、独立した1回の実行です。

```text
instruction + context + history + Agent Profile(Skills/Tools) + Memory
        ↓
     Agent Loop（LLM → tool_calls → Tool実行 → tool result → …）
        ↓
      Result
```

```python
from little_agent.agent import Agent

result = agent.run(
    "このObservationを解釈してください",
    context={"observation": {...}, "world_state": {...}},
    output_schema={"type": "object", "required": ["state_deltas"]},  # 任意
    history=None,  # 任意。Chatだけが渡す
)
result.text  # 常に文字列
result.data  # output_schema を渡したときだけ、検証済みのJSON値
```

- **Runtime自身は状態を持ちません**: 実行中のmessage履歴（user → tool_call → tool_result → …）は実行内でのみ保持され、終了時に破棄されます。
- **継続は呼ぶ側の責任**: 会話を続けたいときは `history` に過去のやり取りを渡します。Runtimeはそれを**コピーして使い、書き換えず、実行後に忘れます**。A2Aは何も渡さないので、Task同士は構造的に混ざりません。
- **context は毎回外部から**: `context` はそのまま実行用プロンプトにJSONとして差し込まれ、永続化されません。
- **確認の一括承認も実行単位**: 確認が必要なToolを一度承認すると、その実行の残りは再確認なしで進みますが、次の実行では再び確認されます。

最大Toolステップ数は `LITTLE_AGENT_MAX_TOOL_STEPS`（既定 `5`）です。

### Structured Output

`output_schema`（JSON Schema相当）を渡すと、最終出力を machine-readable な形で要求できます。

```text
LLM final output
 ↓ JSON parse（壊れたJSONは修復せず拒否）
 ↓ schema validation（little_agent/schema.py の内蔵バリデータ）
 ↓ result.data / A2AではDataPart
```

- schemaはsystem promptにも提示しますが、**特定LLMのstructured-output機能には依存せず、必ずlittle_agent側で検証**します。
- パース失敗・検証失敗は `StructuredOutputError` になります（A2Aでは `failed` タスク）。曖昧な結果を推測して返すことはしません。
- schemaを渡さない通常実行は、従来どおりテキストを返します。

内蔵バリデータが扱うキーワード: `type`（単数/配列）、`enum`、`const`、`properties`、`required`、`additionalProperties`、`items`、`minItems`/`maxItems`、`minLength`/`maxLength`、`minimum`/`maximum`、`anyOf`/`oneOf`/`allOf`。未知のキーワードは無視します。

## Memory

Agent Core は永続memoryを直接知りません。`MemoryStore` を1つ持ち、そこに3つのことだけを聞きます。

```text
MemoryStore
├─ FileMemoryStore   … markdownファイルに保存する（chat 既定）
└─ NullMemoryStore   … 何も持たない（chat --no-memory / A2A）
```

| 呼び出し | 内容 |
| --- | --- |
| `sections()` | system promptに差し込む見出しと本文 |
| `tools()` | memoryを書き換えるTool |
| `learn()` | セッションを要約して恒久プロファイルに落とす |

`NullMemoryStore` はこの3つすべてに「無い」と答えるので、memory機能はコードから消えるのではなく**その実行に存在しなくなります**。

`FileMemoryStore` が扱うファイル:

| ファイル | 内容 | 誰が書くか |
| --- | --- | --- |
| `{workspace}/memory.md` | このワークスペース固有の記憶 | `update_workspace_memory` Tool / 人が直接編集 |
| `~/.little_agent/memory.md` | 全ワークスペース共通の記憶 | `update_global_memory` Tool / 人が直接編集 |
| `{workspace}/profile.md` | 会話から自動抽出したワークスペース像 | セッション終了時と `/remember` |
| `~/.little_agent/profile.md` | 会話から自動抽出したユーザー像 | セッション終了時と `/remember` |

前2つは「意図して書く」memory、後2つは auto-learning（reflection）が更新するプロファイルです。グローバル側のパスは `LITTLE_AGENT_GLOBAL_MEMORY_PATH` / `LITTLE_AGENT_GLOBAL_PROFILE_PATH`、auto-learningの有無は `LITTLE_AGENT_AUTO_LEARNING` で変えられます。

## Workspace

すべてのファイル操作は、明示的に許可された範囲の内側に制限されます。

```text
readable  = workspace + LITTLE_AGENT_READABLE_PATHS + LITTLE_AGENT_WRITABLE_PATHS
writable  = workspace + LITTLE_AGENT_WRITABLE_PATHS
```

- 区切りはWindowsなら `;`、それ以外は `:`。相対パスはワークスペース基準です。
- ディレクトリを指定すればその配下すべて、単一ファイルを指定すればそのファイルだけが対象になります。
- 到達できる範囲は、そのまま**A2Aで他エージェントに渡せる範囲**になります（[workspace / allowed_paths](#ワークの受け渡し先を指定するworkspace--allowed_paths)）。書けるパスは作業場所として、読めるパスは参照用として渡せます。
- 有効な範囲は system prompt にも出るので、モデルは自分がどこまで触れるかを知った上で動きます。

## Tool仕様

Toolは `little_agent/tools` に実装します。各Toolは次の属性とメソッドを持ちます。

- `name`: Tool名
- `description`: LLMに渡す説明
- `parameters`: JSON Schema形式の引数定義
- `requires_confirmation`: 実行前確認が必要か
- `run(context, **kwargs)`: Tool実行本体

Core Tool:

| Tool | 内容 | 確認 |
| --- | --- | --- |
| `list_dir` | ディレクトリ一覧 | 不要 |
| `read_file` | UTF-8テキストファイルを読む | 不要 |
| `write_file` | UTF-8テキストファイルを書く | 必要 |
| `append_to_file` | テキストファイルに追記 | 必要 |
| `move_file` / `delete_file` | ファイルの移動・削除 | 必要 |
| `search_files` | 文字列検索 | 不要 |
| `run_powershell` | PowerShellコマンドを実行 | 必要 |
| `fetch_url` | http(s) URLの取得 | 不要 |
| `delegate_task` | サブタスク1件をA2Aで別のエージェントに委譲 | 必要 |
| `delegate_tasks` | 独立した複数サブタスクをA2Aで**並列**委譲 | 必要 |

memory ONのchatではこれに `update_workspace_memory` / `update_global_memory` が加わります（MemoryStoreが供給するので、他のモードには存在しません）。

MCPサーバが設定されていれば、そのToolが `mcp__<server>__<tool>` として、Resourceを持つサーバがあれば `list_mcp_resources` / `read_mcp_resource` が加わります（[MCP](#mcpmodel-context-protocol)）。

どのCore Toolを使えるかは Agent Profile の `core_tools` で絞れます（`delegate_task` / `delegate_tasks` も対象）。

### PowerShell実行の安全仕様

`run_powershell` はワークスペースをカレントディレクトリとして実行し、`Remove-Item` / `rm` / `rmdir` / `del` / `Format-Volume` / `Stop-Computer` / `Restart-Computer` / `Set-ExecutionPolicy` などの破壊的トークンを簡易ガードでブロックします。完全なサンドボックスではありません。

標準入出力は UTF-8 に統一します。PowerShell のコンソールとパイプの文字コードを設定し、子プロセスには `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` を渡すため、Python の日本語・絵文字出力も扱えます。不正な出力バイトは置換文字として取り込みます。CP932 固定の外部プログラムや、スクリプト内で明示した別の文字コードは個別の調整が必要です。ファイル読み書きの PowerShell コマンドレットの既定エンコーディングは変更しません。

参考: [PowerShell の `$OutputEncoding`](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_preference_variables#outputencoding)、[Python の入出力エンコーディング設定](https://docs.python.org/3.11/using/cmdline.html#envvar-PYTHONIOENCODING)。

## Skills

Skillは公開仕様 **[Agent Skills](https://agentskills.io/specification)** の形式（YAMLフロントマター付きの `SKILL.md` を持つフォルダ）です。Claude Code や Codex など、ほかの対応クライアント向けに書かれたSkillをそのまま使え、こちらで作ったSkillもほかのクライアントで使えます。

同梱Skillは **`little_agent/builtin_skills/` に入っており**、pipでコードと一緒にインストールされます。Skillは**workspaceのデータではなくRuntimeの資源**なので、作業ディレクトリをどこに向けてもSkillが消えることはありません。

Skill実装とCore Runtimeは分離されており、Skill Loader だけが両者をつなぎます。

```text
little_agent/
├─ agent.py
├─ ...
├─ skills/               # Skillの読み込み機構（core側）
│  ├─ loader.py        # 探索・パース・検証・カタログ
│  ├─ frontmatter.py   # SKILL.md のYAMLフロントマター（標準ライブラリのみ）
│  ├─ activation.py    # activate_skill Tool
│  ├─ models.py
│  └─ script_tool.py   # tools.json（Little Agent 拡張）
│
└─ builtin_skills/       # 同梱Skillの実装（データ側／パッケージに同梱）
   ├─ file-manager/
   ├─ python-coder/
   ├─ windows-operator/
   ├─ excel-file/
   ├─ ppt-file/
   └─ ...
```

- Core Runtime に個別Skillの依存を足すことはしません。Skillスクリプトも `little_agent` を import しません（依存は一方向）。
- 新しいSkillの追加はフォルダを足すだけで、coreの変更も登録作業も不要です。
- 外部リポジトリ化やgit submodule化はしていません。

### SKILL.md（Agent Skills 形式）

```markdown
---
name: pdf-processing
description: PDFからテキストや表を抜き出し、フォームを埋め、結合する。PDFやフォームの話題が出たときに使う。
license: Apache-2.0                      # 任意
compatibility: Requires poppler          # 任意（500文字以内）
metadata:                                # 任意（文字列→文字列）
  author: example-org
  version: "1.0"
allowed-tools: read_file write_file      # 任意（スペース区切り。実験的）
---
# PDF Processing

## Instructions
1. まず [リファレンス](references/REFERENCE.md) を読む。
2. 抽出は `scripts/extract.py` を使う。
```

```text
pdf-processing/
├─ SKILL.md        # 必須
├─ scripts/        # 任意: 実行するコード
├─ references/     # 任意: 必要なときに読む資料
├─ assets/         # 任意: テンプレート・画像・データ
└─ tools.json      # 任意: Little Agent 拡張（下の Skill Script Tool）
```

| 項目 | 必須 | 規則 |
| --- | --- | --- |
| `name` | ○ | 小文字英数字と単一のハイフン、64文字以内、先頭・末尾にハイフン不可。**フォルダ名と一致** |
| `description` | ○ | 1024文字以内。「何をするか」と「いつ使うか」を両方書く（エージェントはこれだけを見て使うか決める） |
| `license` / `compatibility` / `metadata` / `allowed-tools` | | 仕様どおり。独自の項目は `metadata` の下に置く |

本文（フロントマターの後）はSkillが有効になったときに渡される手順です。500行以内を目安にし、詳しい資料は `references/` に分けて相対パスで参照します。

### 段階的な読み込み（Progressive disclosure）

仕様どおり、Skillは3段階で読み込まれます。

1. **カタログ** … 毎回の実行で、全Skillの `name` / `description` / 場所だけを `<available_skills>` として system prompt に入れます（1件あたり数十〜百トークン）。Skillが無ければこの節も出しません。
2. **本文** … モデルが「このSkillが合う」と判断したら `activate_skill` Toolを名前付きで呼び、`SKILL.md` の本文を `<skill_content>` で受け取ります。Skillのディレクトリと同梱ファイルの一覧（中身ではなく名前だけ）も付きます。`name` 引数は実在するSkill名の enum なので、存在しないSkillは呼べません。同じ実行で2回呼んでも本文は1回しか入りません。
3. **リソース** … 本文が参照する `references/` などは、モデルが `read_file` で必要なときに読みます。**Skillディレクトリは読み取り専用で許可**されるので、ワークスペース外の同梱Skillでも読めますが、書き換えはできません。

### どのSkillを使うかはLLMが決める

Runtime 側ではSkillを選びません（キーワード一致による事前選択や、本文の自動注入はしません）。全Skillのカタログを見たモデルが、タスクに合うものを自分で選んで `activate_skill` を呼びます。不要なSkillは有効にしません。

**Skillが持つTool（`tools.json`）も、そのSkillが有効になって初めて読み込まれます。** 実行の開始時点でモデルに見えるのは Core Tool と `activate_skill` などだけで、たとえば `read_excel` は `excel-file` を有効にした後にしか現れません。

1. 実行開始 … カタログだけを提示。Skill Tool はどれも未登録
2. モデルが `activate_skill(name="excel-file")` … 本文を受け取り、同時に `read_excel` / `write_excel` が登録される（結果に `Tools now available from this skill: ...` と出る）
3. 以降のステップでそのToolを使える
4. 実行が終わるとSkill Toolは外れ、次の実行（chat の次のターン、次のA2Aタスク）はまた何も選ばれていない状態から始まる

有効にしていないSkillのToolをモデルが呼ぼうとした場合は、実行せずに「そのToolは `excel-file` Skill のものなので、合うなら先に `activate_skill` を」と返します。Agent Profile の `"skills"` で許可されていないSkillのToolは、有効化の候補にもなりません。

chat では `/<skill-name> 依頼` で、**ユーザーが**Skillを選べます（例: `/excel-file 売上.xlsx を集計して`）。そのターンの開始前にSkillが有効になり、本文が依頼の前に置かれ、Toolも読み込まれます。会話履歴には依頼の文だけが残ります。`/tools` は、登録済みToolに加えて「Skillを有効にすると読み込まれるTool」も一覧します。

### 検証（寛容モード）

仕様のクライアント向けガイドに従い、ほかのクライアント向けに書かれたSkillも読めるよう**寛容に**検証します。

| 状況 | 扱い |
| --- | --- |
| `name` が命名規則に合わない／フォルダ名と違う／長すぎる | 警告して読み込む |
| 仕様外のフロントマター項目 | 警告して読み込む |
| `description` が無い・空 | **読み込まない**（カタログに出せないため） |
| フロントマターが解釈できない（閉じていない等） | **読み込まない** |
| `description: Use when: ...` のようにコロンを含む未クォート値 | 意図どおりの文字列として読む |
| フロントマターの無い旧形式（`# name` / `## Description` / `## When to use` / `## Allowed tools` / `## Instructions`） | 旧形式として読み込み、移行を促す警告を出す |

問題は起動時に件数を表示し、chat の `/skills check` で一覧できます。`skill-creator` の `validate_skill` は仕様どおり厳密に検査します。

### Skillの探索順序

```text
1. agents/<name>/skills/           … そのエージェント専用（Agent Profile）
2. <workspace>/.agents/skills/     … プロジェクト（クライアント共通の置き場所）
3. Skillライブラリ                 … 下の1つ（ワークスペース側なら 4 より優先）
4. ~/.agents/skills/               … ユーザー（クライアント共通の置き場所）
5. （ライブラリが同梱の場合）little_agent/builtin_skills/
```

同じ名前のSkillは**先に見つかったものが勝ち**、隠れた側は警告に記録されます。つまりプロジェクト ＞ ユーザー ＞ 同梱 です。`.agents/skills/` はほかの対応クライアントと共有される置き場所で、`LITTLE_AGENT_SHARED_SKILLS=false` で探さないようにできます。

Skillライブラリは次のうち1つです。

```text
1. LITTLE_AGENT_SKILL_LIBRARY_DIR   … 明示指定。最優先
2. <workspace>/skills               … そのディレクトリが存在する場合
3. little_agent/builtin_skills/     … 同梱のBuiltin Skills
```

- **1** は存在しないパスを指していても採用します（typoが黙って無視されず、空ライブラリとして表面化するため）。相対パスはworkspace基準です。
- **2** はプロジェクト固有のSkillライブラリを持ちたいときに使います。存在しなければ次へ進みます。
- **3** が既定です。`LITTLE_AGENT_WORKSPACE` を別ディレクトリに向けても、Builtin Skillsはそのまま使えます。

起動時にどこから読んだかを表示します。

```text
Skills: C:\Users\me\...\little_agent\builtin_skills (built-in)
Skills: D:\my-project\skills (workspace)
Skills: D:\shared\skill-library (configured)
```

括弧内は探索順序のどの段が採用されたかで、設定が効いているかを一目で確認できます。

**Skillが0件のときは黙って起動しません。** 探した場所・存在しなかったディレクトリ・プロファイルが要求したSkill名を挙げて警告します（`"skills": []` と明示したプロファイルは意図的な選択なので警告しません）。

プロジェクトのSkill（`<workspace>/skills`、`<workspace>/.agents/skills`）はそのままエージェントへの指示になり、`tools.json` があればスクリプトも実行されます。信頼できないリポジトリをワークスペースにするときは注意してください。

### Skill Script Tool仕様

Agent Skills 仕様の外にある **Little Agent 独自の拡張**です。Skillは `tools.json` と `scripts/` を持つことで、coreに実装を追加せずにToolを提供できます（仕様は任意のファイルを置くことを認めているので、`tools.json` があってもほかのクライアントではそのまま読めます）。これらのToolは、そのSkillをモデル（または `/<skill-name>` でユーザー）が有効にしたときにだけ登録されます。

```text
skills/<skill-name>/
  SKILL.md
  tools.json
  scripts/
    tool_script.py
```

スクリプトには標準入力で以下のJSONが渡されます。

```json
{
  "tool": "tool_name",
  "workspace": "C:/path/to/workspace",
  "readable_paths": ["C:/path/to/reference"],
  "writable_paths": ["C:/path/to/shared"],
  "arguments": {}
}
```

スクリプトは標準出力に以下のJSONを返します。画像をモデルに渡す場合は `images` にdata URIの配列を含めます（任意）。

```json
{
  "ok": true,
  "content": "result text",
  "images": ["data:image/png;base64,..."]
}
```

標準入出力は**両側ともUTF-8に固定**されるので、日本語を返すSkillもコンソールのコードページに影響されません。`scripts/` の外を指すスクリプトを書いたmanifestは読み込み時に拒否されます。

`skills/<skill-name>` フォルダをコピーするだけで、Skillの説明・Tool定義・実行ロジックをまとめて移動できます。

### 同梱Skill

フォルダ名は仕様の命名規則（ハイフン区切り）に合わせています。Agent Profile の `"skills"` に旧名（`excel_file` など、`_` 区切り）を書いていても同じSkillとして扱います。

| Skill | 内容 |
| --- | --- |
| `file-manager` | ファイル探索・読み書き・検索 |
| `python-coder` | Python実装・調査・実行確認 |
| `windows-operator` | Windows/PowerShell操作 |
| `excel-file` | `.xlsx` の読み取りと簡易作成（`read_excel` / `write_excel`） |
| `ppt-file` | `.pptx` のテキスト読み取りと簡易作成（`read_ppt` / `write_ppt`） |
| `workspace-harness` | 「1タスク=1フォルダ」のファイルシステム型タスクハーネス。人がエリアを統制し、AIはその中で作業する |
| `project-manager` | `data/projects.json` による依存関係付きプロジェクト/タスク管理。ローカルWebビューアで人も編集できる |
| `agent-manager` | Agent Profile（`agents/<name>/agent.json`）の作成・編集 |
| `skill-creator` | Agent Skills 形式のSkillの作成と、仕様に沿った検証（`create_skill` / `validate_skill`） |
| `datetime` | 現在日時・曜日・タイムゾーンの取得 |
| `git` | gitリポジトリの状態確認・差分・コミット |
| `screen-capture` | PC画面のスクリーンショット取得（Vision対応モデルへ画像を直接渡す） |
| `computer-use` | マウス/キーボードによるPC操作 |
| `outlook-email-extractor` | Outlookからのメール抽出 |

Officeファイル系は外部ライブラリなしでOffice Open XMLを扱います（`.xls` / `.ppt` は非対応、読み取りと簡易新規作成が中心）。`screen-capture` は `mss` + `Pillow`、`computer-use` は `pyautogui` が必要です。`project-manager` のビューアは `skills/project-manager/scripts/viewer.py` としてSkillフォルダ内に同梱されています（coreには入っていません）。

`project-manager` のビューアのポートは `LITTLE_AGENT_VIEWER_PORT`（既定 `8765`）で変えられます。`workspace-harness` は `tasks/`（`LITTLE_AGENT_TASKS_DIR`）と `shared/`（`LITTLE_AGENT_SHARED_DIR`）を使います。リポジトリにサンプルが入っています。これらはworkspace側に作られ、無ければ自動生成されます。

## Agent Profile

Agent Profile は「**そのエージェントが何のSkillとToolを使えるか**」を定義する能力contractです。Runtime側は profile を読むだけで、書き換えません。

```text
little_agent/builtin_skills/ # ライブラリ: 同梱スキルのマスター
agents/                     # 各エージェント（.gitignore 対象。運用側のデータ）
  observer/
    agent.json
  planner/
    agent.json
  evaluator/
    agent.json
    skills/                 # 任意: このエージェント専用のスキルフォルダ
```

`agent.json`（`null` / 省略は env・既定へフォールバック）:

```json
{
  "name": "observer",
  "description": "観測結果の解釈だけを行うエージェント",
  "model": null,
  "skills": ["datetime", "file-manager"],
  "core_tools": ["read_file", "list_dir"],
  "max_tool_steps": null,
  "require_confirmation": null,
  "mcp_servers": ["filesystem"]
}
```

- `skills` … ライブラリ（上の探索順序で決まる場所）から名前で選ぶ。省略した場合は `agents/<name>/skills/` に置いたフォルダがそのまま有効になります。両方ある場合はエージェント自身のフォルダが優先されます。
- `core_tools` … Core Toolの許可リスト。省略で全Core Tool有効。`delegate_task` / `delegate_tasks` もこのリストで制御できるので、**委譲機能はRuntimeに常に存在し、使わせるかはProfileが決める**構成になります。
- `mcp_servers` … `mcp.json` で定義したMCPサーバのうち、このエージェントが使ってよいものの名前。**省略すると使いません**（MCPサーバはワークスペースの外に手が届くので、プロファイルごとに明示的にopt-inします）。
- `description` と有効なSkillは、A2A Agent Card の説明・`skills` としてそのまま公開されます。

組み込みの `default` エージェント（予約名。`library` / `none` / `-` も同義）はライブラリ全体＋全Core Tool＋`mcp.json` の全MCPサーバを意味します。何も指定しなければこれで動きます。

エージェント名は `LITTLE_AGENT_AGENT` または `--agent <name>` で選びます。

## A2A（Agent2Agent）プロトコル

Little Agent の外部インタフェースは A2A です。Agent Card による発見と JSON-RPC 2.0 によるタスク実行という標準の手順に載っているため、**Little Agent 以外のA2A対応エージェントとも相互運用**できます。

| 対応範囲 | 内容 |
| --- | --- |
| プロトコル版 | `0.3.0`（`protocolVersion`） |
| Agent Card | `/.well-known/agent-card.json`（旧 `/.well-known/agent.json` も配信） |
| トランスポート | JSON-RPC 2.0 over HTTP（`preferredTransport: JSONRPC`） |
| メソッド | `message/send`、`tasks/get`、`tasks/cancel` |
| Part | `TextPart` と `DataPart`（入力・出力とも） |
| タスク状態 | `submitted` / `working` / `completed` / `canceled` / `failed` |
| 作業場所 | metadata で `workspace`（読み書き）/ `allowed_paths`（読み取りのみ）を要求可。サーバ側で権限種別ごとに検証 |
| 認可 | `LITTLE_AGENT_A2A_TOKEN` による Bearer トークン |
| 未対応 | `message/stream`（SSE）は `-32004 UnsupportedOperation`、`tasks/pushNotificationConfig/*` は `-32003 PushNotificationNotSupported` |

A2Aタスク1件が Agent 実行1回に対応します。タスクごとに新しいエージェントを構築し、履歴を渡さず、`NullMemoryStore` を与えるので、**タスク間でコンテキストも記憶も共有されません**。

SSEとpush通知はどちらも仕様上optionalで、Agent Cardで `false` と宣言しています。現在の設計では委譲が1回のブロッキングTool呼び出しなので、逐次出力の消費者がおらず、クライアントはタスクの間ずっと生きている前提です。非同期委譲を入れる段階になったら再検討します。

### サーバとして公開する

```powershell
little-agent serve-a2a --agent observer --port 8801
# または
python -m little_agent.a2a.serve --agent observer --port 8801
```

- サーバは既定で `127.0.0.1` のみにバインドします。`LITTLE_AGENT_A2A_TOKEN` を設定すると Bearer トークン必須になり、Agent Card の `securitySchemes` に公開されます（カード自体は発見のため公開のまま）。
- サーバ側には確認プロンプトを出せる人間がいないため、**確認が必要なToolは既定で拒否**します。許可するには `--auto-approve` または `LITTLE_AGENT_A2A_AUTO_APPROVE=true`。
- `tasks/cancel` はそのタスクの停止フラグを立て、Tool実行の合間でエージェントを中断させます（緊急停止ホットキーと同じ仕組み）。

### 構造化データの受け渡し

`message/send` の parts には TextPart と DataPart のどちらも渡せます。

**テキストだけ**:

```json
{"parts": [{"kind": "text", "text": "READMEを要約して"}]}
```

**構造化入力**: DataPart で `instruction` / `context` / `output_schema` を明示できます。

```json
{
  "parts": [
    {
      "kind": "data",
      "data": {
        "instruction": "このObservationを解釈してください",
        "context": {"observation": {}, "world_state": {}},
        "output_schema": {
          "type": "object",
          "properties": {
            "state_deltas": {"type": "array"},
            "confidence": {"type": "number"}
          },
          "required": ["state_deltas", "confidence"]
        }
      }
    }
  ]
}
```

- これらのキーを持たない DataPart は、そのまま `context` にマージされます（TextPart で指示、DataPart でデータ、という組み合わせが可能）。
- TextPart と DataPart の `instruction` が両方ある場合は連結されます。
- 指示が1つも無い場合は `-32005 ContentTypeNotSupported` になります。

**結果**: `output_schema` を指定した実行は、artifact が DataPart で返ります。指定しない実行は TextPart です。

```json
{"parts": [{"kind": "data", "data": {"state_deltas": [], "confidence": 0.91}}]}
```

Skill名を直接指定する独自パラメータはありません。能力の境界は Agent Profile で表現し、Profile内でのSkill自動選択に任せます。

### ワークの受け渡し先を指定する（workspace / allowed_paths）

「この案件フォルダで作業して」「共有ドライブのこの資料も見ていい」という**作業場所ごと**の受け渡しができます。`workspace` と `allowed_paths` はA2Aメッセージの metadata（`littleAgent/workspace` / `littleAgent/allowedPaths`）に載り、受け取った側は**自分の設定に照らして検査**してから、そのタスク専用の作業範囲として採用します。理解しない相手はmetadataを無視して自分のワークスペースで動くだけなので、相互運用性は壊れません。

**2つは権限が違います。**

| 項目 | 相手に与える権限 | 意味 |
| --- | --- | --- |
| `workspace` | **読み書き** | 相手がそのタスクで作業する場所。成果物はここに置く |
| `allowed_paths` | **読み取りのみ** | その外にある参照資料。渡した資料を相手に書き換えられることはない |

```jsonc
// delegate_task の引数
{
  "task": "reports/q3 の下書きを仕上げて 売上.xlsx の数字と突き合わせて",
  "agent": "office",
  "workspace": "reports/q3",                 // 作業場所（読み書き）。相対パスでよい
  "allowed_paths": ["D:\\shared\\売上.xlsx"]  // 参照だけ（読み取り専用）
}
```

- `workspace` を渡すと、相手はそのディレクトリを**そのタスクのワークスペース**として動きます（相対パスのTool引数もそこ基準）。ディレクトリが無ければ作成されます。省略すると相手自身のワークスペースのままです。
- `allowed_paths` はワークスペース**外**を読むための例外リストです。書き込みは通らないので、成果物は `workspace` の側に作らせます。渡さなければ相手側の設定のままで、上書きしません。
- 相手自身の `LITTLE_AGENT_WRITABLE_PATHS` は grant では変わりません。渡せるのは「どこで作業するか」と「何を参照してよいか」だけです。
- `delegate_tasks` でもサブタスクごとに同じ2つを指定できます（案件フォルダ別に並列で走らせられます）。
- タスクが終われば効力も終わります。1タスク＝1エージェントなので、他のタスクには漏れません。

**渡せる範囲**（同じ検査を、権限種別ごとに2回行います）:

| 渡すもの | 渡す側・受け取る側とも、この範囲に収まっていること |
| --- | --- |
| `workspace` | 自分の**書き込み可能**な範囲（ワークスペース＋`LITTLE_AGENT_WRITABLE_PATHS`） |
| `allowed_paths` | 自分の**読み取り可能**な範囲（上記＋`LITTLE_AGENT_READABLE_PATHS`） |

1. **渡す側** … 上の表に収まらないものは渡せません。委譲を経由して自分の権限を広げることはできません。読むだけのパスは参照用としては渡せますが、作業場所にはできません。
2. **受け取る側** … 受け取ったパスを、サーバ自身の設定（起動時の `--writable-path` / `--readable-path` を含む）に照らして同じように検査し、外れていれば `-32602 InvalidParams` で断ります。grantを一切受け付けない設定のサーバも同じくエラーを返します。

渡す側の検査に落ちたサブタスクは**送信前に**失敗し、受け取る側に断られたサブタスクも**相手が作業を始める前に**失敗します。どちらも理由（どのパスが、読み取り／書き込みどちらの許可範囲から外れたか）がそのままToolの結果に返ります。

ローカルプロファイルへの委譲では、自動起動する子サーバに親と同じワークスペースと許可パスを引き継ぐので、親が到達できる場所はそのまま渡せます。手動で起動するサーバは次のように許可範囲を決めます。

```powershell
# 案件フォルダを作業場所として渡せるようにする（読み書き）
little-agent serve-a2a --agent office --port 8801 --writable-path D:\projects
# 共有ドライブは参照だけ渡せるようにする（読み取り専用）
little-agent serve-a2a --agent office --port 8801 --readable-path D:\shared
# 信頼できる自分専用サーバなら、検査自体を外すこともできる（既定はオフ）
python -m little_agent.a2a.serve --port 8801 --allow-any-path
```

### クライアントとして委譲する（delegate_task / delegate_tasks）

Core Tool の `delegate_task`（1件）と `delegate_tasks`（複数を並列）がA2Aクライアントです。Agent Cardを取得し、`message/send` を送り、`tasks/get` で終了状態までポーリングして、artifact を親に返します。

`delegate_task` の引数:

| 引数 | 内容 |
| --- | --- |
| `task` | 相手エージェントへの完結した指示（コンテキストは共有されないので必要な情報を全部入れる） |
| `agent` | 任意。ローカルのプロファイル名、または設定済みリモートピア名。省略すると `default` |
| `agent_url` | 任意。A2Aエージェントのベースを直接指定（例 `http://127.0.0.1:8801/`）。`agent` より優先 |
| `background` | 任意。作業前に渡す前提・素材 |
| `workspace` | 任意。相手がこのタスクで作業する（読み書きする）ディレクトリ |
| `allowed_paths` | 任意。そのワークスペース外で相手が**読んで**よい参照ファイル・ディレクトリの配列 |

`delegate_tasks` は同じ形のオブジェクトの配列を取り、それぞれを独立したA2Aタスクとして同時に走らせます。

- 同時実行数の上限は `LITTLE_AGENT_MAX_PARALLEL_DELEGATIONS`（既定 `4`）。
- 結果は**依頼した順**に `[1/3] peer '...' — completed` の形で並び、先頭に完了/失敗の件数サマリが付きます。
- **部分失敗を許容**します。全滅したときだけTool全体がエラーになります。
- 依存関係のあるサブタスクを1回の `delegate_tasks` に入れてはいけません。その場合は `delegate_task` を順に呼びます。

委譲先の解決:

- **リモートピア** … `LITTLE_AGENT_A2A_PEERS`（`name=url,name2=url2` 形式）に登録したエージェント、または `agent_url` で直接指定したエージェント。相手はA2A準拠であれば実装は問いません。
- **ローカルプロファイル** … `agents/` のプロファイル名を渡すと、初回の委譲時にローカルのA2Aサーバを空きポートで自動起動し、以降のセッション中は再利用します（終了時に自動停止）。

委譲の深さは message の metadata（`littleAgent/delegationDepth`）で相手に伝わり、無限の連鎖を防ぎます。`LITTLE_AGENT_MAX_DELEGATION_DEPTH`（既定 `2`、`0` で委譲を無効化）で調整します。委譲待ちの間に緊急停止ホットキーを押すと、待機を打ち切って相手のタスクを `tasks/cancel` で取り消します。

## MCP（Model Context Protocol）

Little Agent は MCP の**クライアント**と**サーバ**の両方になれます。A2A と同じく標準ライブラリだけで実装しています（MCP SDK 不使用）。

| 対応範囲 | 内容 |
| --- | --- |
| プロトコル版 | `2025-06-18`（`2025-03-26` / `2024-11-05` のサーバとも接続可） |
| トランスポート | stdio（子プロセス）、Streamable HTTP（JSON応答 / SSE応答、`Mcp-Session-Id`） |
| クライアント機能 | Tools（ページング、text / image / resource / structuredContent / isError）、Resources（list / templates / read）、Prompts（list / get）、Roots（workspace と許可パスを返す）、ping、`notifications/tools/list_changed`、タイムアウト時の `notifications/cancelled` |
| 未対応 | Sampling、Elicitation（capabilityを宣言しないので、準拠サーバは要求してきません）、HTTPのGETによるサーバ発ストリーム、OAuth認可フロー（`headers` で静的トークンを渡す形のみ） |

### MCPサーバを使う（mcp.json）

`mcp.json` に MCP サーバを書くと、起動時に接続してそのToolをエージェントに渡します。書式は Claude Desktop / Claude Code / Cursor などと共通の `mcpServers` 形式なので、定義をそのまま持ってこられます。

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:\\projects"]
    },
    "issues": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${ISSUES_TOKEN}"},
      "require_confirmation": false,
      "tools": ["search_issues", "get_issue"]
    }
  }
}
```

| キー | 内容 |
| --- | --- |
| `command` / `args` / `env` / `cwd` | stdioサーバ。子プロセスとして起動（`cwd` 既定はワークスペース）。`npx` などは PATH から解決（Windowsの `npx.cmd` も可） |
| `url` / `headers` | Streamable HTTPサーバ |
| `require_confirmation` | Tool実行前に確認するか（既定 `true`） |
| `tools` | 公開するTool名の許可リスト（省略で全部） |
| `disabled` | 定義を残したまま起動しない |
| `timeout` | 1リクエストの待ち時間（秒、既定 `60`） |

- `args` / `env` / `headers` / `url` / `cwd` の `${NAME}` は環境変数で置き換えます。秘密情報をファイルに書かずに済みます。
- 読む場所は `~/.little_agent/mcp.json`（`LITTLE_AGENT_GLOBAL_MCP_CONFIG`）→ `<workspace>/mcp.json`（`LITTLE_AGENT_MCP_CONFIG`）の順で、同じ名前はワークスペース側が勝ちます。
- 接続はプロセス内で共有されます。chat の各ターン、`/agent` の切り替え、A2Aの各Taskが同じ接続を使い、終了時に閉じます（stdioサーバは停止）。
- 起動に失敗したサーバは起動時に警告して除外し、エージェント自体はそのまま動きます。

エージェントから見ると:

- 各Toolは `mcp__<server>__<tool>` という名前のToolになります（説明の先頭に `[MCP <server>]`）。Tool名が衝突することはありません。
- Resourceを持つサーバがあると `list_mcp_resources`（一覧）と `read_mcp_resource`（URI指定で読む）が加わります。画像は Vision 対応モデルにそのまま渡ります。
- サーバが `initialize` で返した `instructions` は system prompt に入ります。
- サーバが `tools/list_changed` を通知すると、次の実行の前にTool一覧を取り直します。
- サーバから `roots/list` を聞かれると、ワークスペースと許可パスを `file://` URI で答えます。

**MCPサーバはワークスペースの外にあります。** サーバが何に触れるかはサーバ自身とその起動方法が決めることで、Little Agent のパス制限は及びません。そのため:

- MCP Tool は既定で**実行前に確認**します（`require_confirmation: false` で外せます）。A2A / `serve-mcp` では、確認が必要なToolは `--auto-approve` が無い限り拒否されます。
- 名前付きの Agent Profile は `mcp_servers` に書いたサーバしか使えません（`default` エージェントは全サーバ）。

chat では `/mcp` で状態を確認できます。

| コマンド | 動作 |
| --- | --- |
| `/mcp` | サーバ一覧（状態・トランスポート・Tool数・エラー） |
| `/mcp tools [server]` | MCP Tool の一覧 |
| `/mcp resources [server]` | Resource / Resource Template の一覧 |
| `/mcp prompts [server]` | Prompt の一覧と引数 |
| `/mcp prompt <server> <name> [key=value ...]` | MCP Prompt を取得して、その内容をエージェントに送る |
| `/mcp reconnect <server>` | サーバに接続し直す（失敗したサーバの再試行にも） |

### MCPサーバとして公開する（serve-mcp）

```powershell
little-agent serve-mcp --agent office
```

エージェント自身を stdio の MCP サーバとして公開します。Claude Desktop / Claude Code / IDE など任意の MCP ホストから、ほかのToolと同じように仕事を渡せます。

```json
{
  "mcpServers": {
    "office": {
      "command": "little-agent",
      "args": ["serve-mcp", "--agent", "office"],
      "env": {"LITTLE_AGENT_WORKSPACE": "D:\\work"}
    }
  }
}
```

公開されるToolは `run_task` の1つです。

| 引数 | 内容 |
| --- | --- |
| `instruction` | 必須。完結した指示 |
| `context` | 任意。この呼び出し用の構造化データ |
| `output_schema` | 任意。JSON Schema。指定すると検証済みJSONが `structuredContent` で返ります |
| `workspace` | 任意。この呼び出しで作業する（読み書きする）ディレクトリ |
| `allowed_paths` | 任意。ワークスペース外で**読んで**よいパスの配列 |

- A2Aと同じく、呼び出しごとに新しいエージェントを作り、履歴を渡さず、`NullMemoryStore` を与えます。呼び出し間でコンテキストも記憶も共有しません。
- `workspace` / `allowed_paths` は A2A の [workspace / allowed_paths](#ワークの受け渡し先を指定するworkspace--allowed_paths) と同じ検査（書き込み可能範囲／読み取り可能範囲）を通ります。外れていればTool エラーになります。
- 確認が必要なToolは既定で拒否します（`--auto-approve` / `LITTLE_AGENT_MCP_AUTO_APPROVE=true` で許可）。検査を外す `--allow-any-path` / `LITTLE_AGENT_MCP_ALLOW_ANY_PATH` もあります。
- `notifications/cancelled` を受けると、その呼び出しのエージェントを Tool 実行の合間で止めます。
- stdout はプロトコル専用です。Runtime が出すそのほかの表示はすべて stderr に回ります。

## セットアップ

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
Copy-Item .env.example .env
```

設定は `.env`（または環境変数）で行います。項目の一覧と説明は `.env.example` を参照してください。

## Chat

```powershell
little-agent chat
little-agent chat --no-memory
```

`>` プロンプトで入力します。**会話は続きます**（前のターンの内容は次のターンでも参照できます）。一方で、Tool実行の途中経過は次のターンには持ち越しません — 残るのはユーザー発言とAgentの最終回答だけです。

### スラッシュコマンド

`>` プロンプトで `/` から始まる入力は **スラッシュコマンド** として扱われます。

- `/` 始まり → コマンドとして解釈
- `//` 始まり → 先頭の `/` を1つ外した本文をそのままLLMへ送る（エスケープ）
- それ以外 → 通常どおり実行

ディスパッチ順序は「built-in → custom → どちらも無ければ `Unknown command`」です。未知コマンドはLLMに送られません。

| コマンド | エイリアス | 動作 |
| --- | --- | --- |
| `/help` | `/?` | 全コマンドを説明付きで一覧 |
| `/exit` | `/quit` | セッション終了（memory ONならここで auto-learning が走る） |
| `/skills` | | 読み込み済みSkillを一覧（`/skills <name>` で詳細と本文、`/skills check` で読み込みの問題） |
| `/<skill-name>` | | ユーザーがSkillを選んで依頼を送る。本文とそのSkillのToolが最初から有効になる（例: `/excel-file 売上.xlsx を集計して`） |
| `/tools` | | 登録済みToolと、Skillを有効にすると読み込まれるToolを一覧 |
| `/usage` | | 当セッションのtoken累計を表示 |
| `/config` | | model・workspace・memory状態などの設定を表示 |
| `/reload` | | `commands/*.md` を再読込 |
| `/clear` | | 会話をリセット（永続memoryは消しません） |
| `/memory` | | 永続memoryの内容を表示（OFFならその旨を表示） |
| `/remember` | | いまの会話から恒久memoryを抽出して保存 |
| `/agents` | | エージェントプロファイルを一覧（アクティブは `*`） |
| `/agent` | | アクティブなエージェントを表示 / `/agent <name>` で切替 |
| `/mcp` | | MCPサーバ・Tool・Resource・Promptの確認、Promptの実行（[詳細](#mcpサーバを使うmcpjson)） |

`/agent` で切り替えても**会話は続きます**。切り替わるのは能力（Skill/Tool）であって、話している相手の記憶ではありません。

`commands/<name>.md` を置くと `/name` で使えるユーザー定義コマンドになります。探索先はプロジェクト（`LITTLE_AGENT_COMMANDS_DIR`）とグローバル（`~/.little_agent/commands/`）で、名前が衝突した場合はプロジェクトが優先されます。

```markdown
---
description: 指定ファイルをレビューして改善点を優先度付きで挙げる
---
次のファイルを読んでコードレビューして。
対象: $ARGUMENTS
```

引数展開は `$ARGUMENTS`（全文字列）と `$1` `$2`（位置引数）。プレースホルダが1つも無く引数がある場合は本文末尾に追記されます。同梱の例: `commands/review.md`、`commands/plan.md`、`commands/commit.md`。

### 承認と緊急停止

- **実行内の一括承認**: 確認が必要なToolを最初に実行するとき一度だけ確認し、承認するとその実行の残りは再確認しません（PC操作中にカーソルを奪い合わないため）。
- **緊急停止ホットキー**: エージェントが動作している間だけ有効なグローバルホットキー（既定 `Ctrl+Alt+Q`、`LITTLE_AGENT_STOP_HOTKEY`）。Tool実行の合間で中断します。`pynput` を使うため標準の依存に含まれます。
- `computer-use` 使用時は、マウスを画面の隅へ動かすと pyautogui の failsafe が即アボートします。

## ログとToken集計

`LITTLE_AGENT_ENABLE_LOGGING=true`（既定）でセッション単位のJSONLログを保存します。

```text
logs/
  conversations/<session_id>.jsonl   ユーザー入力、LLM応答、最終回答、LLMエラー
  tools/<session_id>.jsonl           Tool名、引数、実行結果、キャンセル、エラー
  usage/<session_id>.jsonl           LLM呼び出しごとのtoken使用量と累計
```

互換APIが `usage` を返さない場合は、文字数からの概算値を `estimated: true` として記録します。

## テスト

```powershell
python -m pytest -q
```

| ファイル | 対象 |
| --- | --- |
| `tests/test_core.py` | Agent loop、multi-step tool loop、実行の独立性、Structured Output |
| `tests/test_memory.py` | MemoryStore（File/Null）、ChatSessionの会話継続、auto-learning |
| `tests/test_launch.py` | 起動モードの引数解釈と、各モードが選ぶMemoryStore |
| `tests/test_skills.py` | 同梱Skillの読み込み、Skill Tool実行、Skill追加、ライブラリ探索順序、0件時の警告、core非依存の検証 |
| `tests/test_skill_format.py` | Agent Skills 形式: フロントマターの解釈、寛容な検証、旧形式の読み込み、カタログと `activate_skill`、Skillディレクトリの読み取り専用許可、`/<skill-name>`、`.agents/skills` の探索、`skill-creator` の出力 |
| `tests/test_grant.py` | workspace / allowed_paths の表現と、読み書き権限ごとの2段階認可 |
| `tests/test_a2a.py` | A2Aサーバ/クライアント実HTTP、TextPart/DataPart、cancel、Task非共有、workspace受け渡し、委譲・並列委譲 |
| `tests/test_schema.py` | 内蔵JSON Schemaバリデータ |
| `tests/test_agents.py` | Agent Profile（Skill/Tool/委譲の制限、profile解決） |
| `tests/test_commands.py` | スラッシュコマンド |
| `tests/test_workspace_harness.py` | `workspace-harness` Skillのタスクフォルダ操作 |
| `tests/test_mcp.py` | mcp.json、MCPクライアント（stdio実プロセス / Streamable HTTPのJSON・SSE）、Tool/Resource/Prompt、Profile選択、`/mcp`、`serve-mcp`（実プロセスでのクライアント↔サーバ往復を含む） |

## 拡張方法

**Toolを追加する**: `little_agent/tools` にToolクラスを追加し、`name` / `description` / `parameters` / `requires_confirmation` / `run()` を実装して、`little_agent/tools/__init__.py` の `default_tools()` に登録します。

**Skillを追加する**: Skillライブラリ（または `<workspace>/.agents/skills/`）に `<new-skill>/SKILL.md` を作り、フロントマターに `name`（フォルダ名と同じ）と `description` を書きます。必要なら `scripts/` / `references/` / `assets/` と `tools.json` を足します。coreの変更は不要です。ほかの Agent Skills 対応クライアント向けのSkillもそのままコピーして使えます。

**外部のToolをつなぐ**: MCPサーバなら `mcp.json` に書くだけです。coreもSkillも変更不要です。

**Memoryの保存先を変える**: `little_agent/memory/store.py` の `MemoryStore` プロトコル（`sections` / `tools` / `learn` / `describe`）を満たすクラスを作り、`build_agent(..., memory=...)` に渡します。Runtime側の変更は要りません。

## 現在の制限

- A2Aはブロッキング＋ポーリングのみ（`message/stream` のSSEとpush通知は意図的に未対応）
- タスクストアはインメモリ（プロセス再起動で消えます）
- PowerShell安全ガードは簡易的です
- Skillのフロントマターは仕様で使う範囲のYAMLだけを解釈します（アンカーや複雑なネストは非対応）
- Web検索Toolはありません（`fetch_url` によるURL取得のみ）
- Structured Output に失敗したときの自動リトライは行いません（そのまま失敗として返します）
