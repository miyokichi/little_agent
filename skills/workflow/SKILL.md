# workflow

## Description
ゴールをAIタスクと人間タスクの依存関係付きワークフロー(DAG)に分解して data/workflows.json に保存し、進捗の記録とブラウザでの可視化を支援する。計画、段取り、工程管理、承認フロー、タスク分解、進捗確認に対応する。

## When to use
- 複数ステップの計画、プロジェクトの段取り、工程を立てたいとき
- AIの作業と人間の承認、レビュー、作業が混ざる進行を管理したいとき
- ワークフロー、依存関係、進捗、可視化、ビューア、ダッシュボードが話題のとき
- タスクの状態(着手、完了、失敗、スキップ)を記録または確認したいとき
- 単発のTODO追加やメモは task_manager を使う(workflow は複数タスクと依存関係の管理用)

## Allowed tools
- create_workflow
- add_workflow_task
- update_task_status
- show_workflow
- list_workflows
- delete_workflow
- open_workflow_viewer

## Instructions
- ゴールは3〜10個のタスクに分解する。1タスクは1回の作業で完了できる粒度にする。
- assignee の判断基準: エージェントのツールで完結する作業(調査、文書作成、コード、ファイル操作)は "ai"。実世界の作業、外部システムの操作、意思決定、承認、レビューは "human"。承認が必要な箇所は独立した human タスク(承認ゲート)として間に挟む。
- create_workflow では各タスクに一時キー key を付け、depends_on は key で参照する。登録後は応答に含まれる実IDを使って操作する。
- depends_on は順序が本当に必要な場合だけ張る。並列にできるタスクへ不要な依存を張らない。
- 作成後はタスク一覧(ID付き)を要約して見せ、open_workflow_viewer でブラウザから確認できることを案内する。
- AIタスクを実行するときのプロトコル: 着手直前に update_task_status で running にする → 作業する → 完了直後に done と result(成果の1〜2文)を記録する。失敗したら failed と理由を記録する。
- human タスクは通常ユーザー自身がビューアの「完了にする」ボタンで完了する。会話でユーザーが完了を明言した場合のみ update_task_status で done にする。
- 対象の workflow_id / task_id が曖昧なときは、先に list_workflows や show_workflow で確認する。
- delete_workflow は元に戻せないため、確認プロンプトを尊重する。
