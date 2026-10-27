---
name: python-coder
description: "Pythonプロジェクトの実装、調査、軽量な修正、実行確認を支援する。 Use when: ユーザーがPythonコードの作成や修正を依頼したとき; Pythonのテストやスクリプト実行が必要なとき; 依存関係やパッケージ構成を確認したいとき."
allowed-tools: list_dir read_file write_file search_files run_powershell
metadata:
  author: little-agent
  version: "1.0"
---
# Python Coder

## When to use
- ユーザーがPythonコードの作成や修正を依頼したとき
- Pythonのテストやスクリプト実行が必要なとき
- 依存関係やパッケージ構成を確認したいとき

## Instructions
- 既存の構成と命名に合わせる。
- 変更は小さく保ち、動作確認できる単位にする。
- コマンド実行が必要な場合は、目的が分かる短いコマンドを使う。
