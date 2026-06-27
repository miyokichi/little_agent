# screen_capture

## Description
PC画面のスクリーンショット取得と、Vision API を使った画面内容の説明を支援する。

## When to use
- ユーザーが「スクリーンショットを撮って」「画面を保存して」と依頼したとき
- 「今の画面を説明して」「画面に何が映っているか教えて」と聞かれたとき
- 画面上の表示内容を確認・要約したいとき

## Allowed tools
- take_screenshot
- describe_screen

## Setup
このスキルにはスクリーンショット用のライブラリが必要です。

    pip install mss Pillow

`describe_screen` は Vision 対応モデルへの API 呼び出しを行うため、以下の環境変数を使用します（`.env` でも可）。

- `OPENAI_API_KEY`（必須）
- `OPENAI_BASE_URL`（任意, 既定 `https://api.openai.com/v1`）
- `LITTLE_AGENT_VISION_MODEL`（任意, 既定は `LITTLE_AGENT_MODEL` → `gpt-4.1-mini`）
- `LITTLE_AGENT_TIMEOUT_SECONDS`（任意, 既定 60）

## Instructions
- 画面を保存するだけなら `take_screenshot` を使う。保存先は省略可（既定 `screenshots/screen.png`）。
- 画面の内容を理解・要約したいときは `describe_screen` を使う。具体的に知りたいことがあれば `prompt` に指定する。
- 一部の領域だけ対象にしたい場合は `region` に `[x, y, width, height]` を渡す。
- このスキルは画面の「取得」専用。マウス/キーボードによる操作は行わない。
- GUI セッションが無い環境（ヘッドレス）ではキャプチャに失敗する。その場合はエラー内容をユーザーに伝える。
