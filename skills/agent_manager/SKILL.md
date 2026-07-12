# agent_manager

## Description
エージェントのプロファイル（使うスキルとツールの構成）を作成・管理する。全スキルを保管するライブラリ（skills/）から、選んだスキルフォルダを各エージェント（agents/<name>/）にコピーして構成する。エージェントの一覧、詳細表示、スキルの追加・削除、コアツールの許可リスト設定、削除に対応する。

## When to use
- 用途特化のエージェントを作りたいとき（例: Office作業用、PC操作用、調査用）
- 「エージェントを作って」「このエージェントにスキルを追加して」「エージェント一覧を見せて」などの依頼
- 各エージェントが使えるスキルやツールを設定・変更したいとき
- 実行するエージェントの選択・切替そのものは、CLIの `/agents` `/agent <name>` コマンドや起動時の `--agent` で行う（このスキルは構成の管理担当）

## Allowed tools
- create_agent
- list_agents
- show_agent
- add_agent_skill
- remove_agent_skill
- set_agent_core_tools
- delete_agent

## Instructions
- エージェントは agents/<name>/ に作られる。skills/ 配下のライブラリからスキルフォルダを **コピー** して構成する（スナップショット。ライブラリを後で編集してもコピー済みには反映されない）。
- create_agent では skills にライブラリのフォルダ名を渡す。利用可能なスキル名が不明なときは、まずライブラリ（skills/）の一覧を確認するか、存在しない名前を渡すとエラーに候補が表示される。
- コアツール（read_file, write_file, list_dir, run_powershell など本体組み込みのツール）は core_tools の許可リストで絞れる。未指定なら全コアツールが有効。スキル由来のツールはコピーしたスキルに応じて自動で有効になる。
- 破壊的な操作（create_agent の overwrite、remove_agent_skill、delete_agent）は実行前確認が出る。delete_agent は元に戻せないので確認を尊重する。
- ディスク上の変更が実行中のエージェントに反映されるのは、次回起動時か、CLIで `/agent <name>` で切り替えたとき。作成・変更後はその旨を案内する。
- 作成したら「little-agent --agent <name> で起動、または会話中に /agent <name> で切替」と伝える。
