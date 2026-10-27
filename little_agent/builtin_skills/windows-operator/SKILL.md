---
name: windows-operator
description: "WindowsとPowerShellを前提にしたローカル操作を支援する。 Use when: ユーザーがWindows上の操作やPowerShellコマンドを依頼したとき; ローカル環境の状態確認が必要なとき."
allowed-tools: run_powershell get_datetime list_dir
metadata:
  author: little-agent
  version: "1.0"
---
# Windows Operator

## When to use
- ユーザーがWindows上の操作やPowerShellコマンドを依頼したとき
- ローカル環境の状態確認が必要なとき

## Instructions
- PowerShell構文を優先する。
- 破壊的な操作や環境変更は避け、必要な場合は確認を取る。
- コマンドの作業ディレクトリはワークスペース内に限定する。
