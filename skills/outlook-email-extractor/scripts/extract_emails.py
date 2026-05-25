"""
Outlook メール抽出ツール
========================
Outlook COM を使って、メールを件名・送信元でフィルタし抽出します。

対応ソース:
  1. ライブ Outlookストア（デフォルト）
  2. .pst ファイル

出力形式: CSV / JSON

使用例:
    python extract_emails.py --source-type pst --pst-path "C:\\backup\\archive.pst" \
        --subject "お知らせ" --sender "info@company.com" --output "result.csv" --format csv

    python extract_emails.py --source-type live \
        --subject "見積" --sender "tanaka" --output "estimate.json" --format json

要件:
  - Python 3.7+
  - pywin32 (pip install pywin32)
  - 64-bit Python（64-bit Outlook との整合）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import win32com.client
    import pythoncom
except ImportError:
    print("エラー: pywin32 が必要です。pip install pywin32 を実行してください。")
    sys.exit(1)

# ============================================================================
# データクラス
# ============================================================================

@dataclass
class EmailItem:
    """抽出されたメールの基本情報"""
    sender_name: str
    sender_email: str
    recipients: str
    subject: str
    sent_on: str
    body_preview: str
    has_attachments: bool
    attachment_count: int
    item_class: str  # IPM.Note など


# ============================================================================
# ヘルパー関数
# ============================================================================

def _safe_str(val: any, default: str = "") -> str:
    """None を安全に文字列に変換"""
    if val is None:
        return default
    return str(val)


def _format_date(dt) -> str:
    """Outlook の日付オブジェクトを文字列にフォーマット"""
    try:
        if hasattr(dt, "strftime"):
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        # pythoncom.MakeTime からの datetime ではない場合
        return str(dt)
    except Exception:
        return str(dt)


def _extract_mail_props(item) -> Optional[EmailItem]:
    """
    Outlook.MailItem からプロパティを抽出
    """
    try:
        # MessageClass が IPM.Note（通常メール）のみに限定
        item_class = _safe_str(item.MessageClass)
        if not item_class.startswith("IPM.Note"):
            return None

        # 送信元情報
        sender_name = _safe_str(item.SenderName)

        sender_email = _safe_str("")
        try:
            sender_email = _safe_str(item.SenderEmailAddress)
        except Exception:
            # Exchange マップド名前空間の場合は取得できないことがある
            try:
                sender_email = _safe_str(item.Sender.GetExchangeUser().PrimarySmtpAddress)
            except Exception:
                sender_email = _safe_str(item.Sender.GetAddress())

        # 受信者（To + CC をコンマ区切り）
        to_list = []
        try:
            for recip in item.Recipients:
                to_list.append(_safe_str(recip.Name))
        except Exception:
            pass
        recipients = ", ".join(to_list)

        # 件名
        subject = _safe_str(item.Subject)

        # 送信日時
        sent_on = _format_date(item.SentOn)

        #本文プレビュー（最初 500 文字）
        body = _safe_str(item.Body)
        body_preview = body[:500] if body else ""

        # 添付ファイル
        has_attachments = item.Attachments.Count > 0
        attachment_count = item.Attachments.Count

        return EmailItem(
            sender_name=sender_name,
            sender_email=sender_email,
            recipients=recipients,
            subject=subject,
            sent_on=sent_on,
            body_preview=body_preview,
            has_attachments=has_attachments,
            attachment_count=attachment_count,
            item_class=item_class,
        )
    except Exception as e:
        print(f"  [警告] メール項目の抽出に失敗: {e}")
        return None


# ============================================================================
# フィルターロジック
# ============================================================================

def _matches_filter(item: EmailItem, subject_filter: Optional[str], sender_filter: Optional[str]) -> bool:
    """
    フィルタ条件に一致するかチェック

    subject_filter: 件名に含まれる部分一致文字列（大文字小文字区別なし）
    sender_filter:  送信元（名前またはメールアドレス）に含まれる部分一致文字列（大文字小文字区別なし）
    """
    if subject_filter:
        if subject_filter.lower() not in item.subject.lower():
            return False

    if sender_filter:
        sender_lower = sender_filter.lower()
        if (sender_lower not in item.sender_name.lower() and
                sender_lower not in item.sender_email.lower()):
            return False

    return True


# ============================================================================
# フォルダ探索
# ============================================================================

def _walk_folders(folder, emails: list, subject_filter: Optional[str], sender_filter: Optional[str]):
    """
    フォルダを再帰的に探索してメールを抽出
    """
    for subfolder in folder.Folders:
        try:
            _walk_folders(subfolder, emails, subject_filter, sender_filter)
        except Exception as e:
            print(f"  [警告] サブフォルダ '{subfolder.Name}' のアクセス失敗: {e}")

    # 現在のフォルダ内のアイテムを処理
    try:
        items = folder.Items
        # 件名のソート順序（最新が先頭にくるよう）
        items.Sort("[ReceivedTime]", True)

        for item in items:
            try:
                mail = _extract_mail_props(item)
                if mail is not None and _matches_filter(mail, subject_filter, sender_filter):
                    emails.append(mail)
            except Exception as e:
                print(f"  [警告] アイテム処理中にエラー: {e}")
    except Exception as e:
        print(f"  [警告] フォルダ '{folder.Name}' のアイテム取得に失敗: {e}")


# ============================================================================
# メイン抽出処理
# ============================================================================

def extract_emails(
    source_type: str = "live",
    pst_path: Optional[str] = None,
    subject_filter: Optional[str] = None,
    sender_filter: Optional[str] = None,
    output_path: str = "output.csv",
    output_format: str = "csv",
    include_body: bool = False,
) -> list:
    """
    Outlook からメールを抽出するメイン関数

    Parameters
    ----------
    source_type : str
        "live" = ライブ Outlook, "pst" = .pst ファイル
    pst_path : str | None
        .pst ファイルのフルパス（source_type="pst" の場合必須）
    subject_filter : str | None
        件名に含まれる文字列（部分一致、大文字小文字区別なし）
    sender_filter : str | None
        送信元に含まれる文字列（名前またはメールアドレス、部分一致）
    output_path : str
        結果の出力ファイルパス
    output_format : str
        "csv" または "json"
    include_body : bool
        True の場合、body_preview に 500 文字のプレビューを含む

    Returns
    -------
    list[EmailItem]
        抽出されたメールのリスト
    """
    print("=" * 60)
    print("  Outlook メール抽出ツール")
    print("=" * 60)

    # Outlook アプリケーションを取得
    try:
        outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    except Exception as e:
        print(f"エラー: Outlook に接続できません。Outlook を起動して再度試してください。\n  詳細: {e}")
        return []

    target_folder = None

    if source_type == "pst":
        if not pst_path:
            print("エラー: source_type=pst の場合は --pst-path を指定してください。")
            return []

        pst_path = os.path.expandvars(pst_path)
        if not os.path.exists(pst_path):
            print(f"エラー: ファイルが見つかりません: {pst_path}")
            return []

        print(f"\n.pst ファイルを開きます: {pst_path}")
        try:
            # 既にマウント済みか確認
            store_found = False
            for store in outlook.Stores:
                if pst_path.lower() in store.FilePath.lower():
                    store_found = True
                    break

            if not store_found:
                outlook.AddStore(pst_path)
                print("  (.pst をマウントしました)")

            # 対象 Store を探してフォルダを取得
            target_folder = None
            for store in outlook.Stores:
                if pst_path.lower() in store.FilePath.lower():
                    target_folder = store.GetRootFolder()
                    break

            if target_folder is None:
                print("エラー: .pst ファイルのルートフォルダを取得できませんでした。")
                return []

        except Exception as e:
            print(f"エラー: .pst ファイルの読み込みに失敗しました。詳細: {e}")
            return []

    else:  # live
        print("\nライブ Outlook ストアからメールを抽出します。")
        # 既定のフォルダ（通常はデフォルトメールボックス）
        target_folder = outlook.GetDefaultFolder(6)  # olFolderInbox

        # 全てのフォルダを対象にしたいので、Session.Folders ルートから
        # しかし、Outlook は Folders から直接アクセスできないため、
        # GetNamespace("MAPI").Folders で全ストアを取得
        # 全てのユーザーフォルダを対象にする
        all_folders = outlook.Folders
        print(f"  対象フォルダ数: {all_folders.Count}")

        # 全てのフォルダを対象にする場合はループで処理
        # 簡易実装として、受信ボックスとそのサブフォルダを対象に
        # より広範囲に探索する場合、all_folders をループ

    # フィルタ条件の表示
    print(f"  件名フィルタ: {_safe_str(subject_filter, '(なし)')}")
    print(f"  送信元フィルタ: {_safe_str(sender_filter, '(なし)')}")
    print(f"  出力ファイル: {output_path}")
    print()

    emails: list[EmailItem] = []

    if source_type == "pst":
        # pst: target_folder 以下を探索
        print(f"  フォルダ探索開始 (pst: {target_folder.Name})...")
        _walk_folders(target_folder, emails, subject_filter, sender_filter)
    else:
        # live: 全てのフォルダを探索
        print("  フォルダ探索開始 (live)...")
        try:
            for folder in outlook.Folders:
                print(f"    ストア: {folder.Name}")
                _walk_folders(folder, emails, subject_filter, sender_filter)
        except Exception as e:
            print(f"  [警告] フォルダ探索中にエラー: {e}")

    print(f"\n  抽出完了: {len(emails)} 件")

    # 結果が 0 件の場合はそのまま返す
    if not emails:
        print("  ヒント: フィルタ条件を変更するか、フォルダ内にメールがあるか確認してください。")
        return emails

    # 出力ファイルの書き出し
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    if output_format == "json":
        data = [asdict(e) for e in emails]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  JSON 出力: {output_path}")
    else:
        # CSV 出力
        # BOM 付き UTF-8 で Excel 互換
        bom = "\ufeff"
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "送信元名前", "送信元メール", "受信者", "件名",
                "送信日時", "本文プレビュー", "添付あり", "添付数",
            ])
            for e in emails:
                writer.writerow([
                    e.sender_name,
                    e.sender_email,
                    e.recipients,
                    e.subject,
                    e.sent_on,
                    e.body_preview if include_body else "",
                    e.has_attachments,
                    e.attachment_count,
                ])
        print(f"  CSV 出力: {output_path}")

    return emails


# ============================================================================
# CLI エントリポイント
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Outlook メールを件名・送信元でフィルタして抽出",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # ライブ Outlook から件名に「見積」を含むメールを抽出
  python extract_emails.py --source-type live --subject 見積 --output estimate.csv

  # .pst から送信元に「tanaka」を含むメールを JSON で抽出
  python extract_emails.py --source-type pst --pst-path "C:\\backup\\data.pst" --sender tanaka --output tanaka.json --format json

  # 両方のフィルタを組み合わせて 500 文字の本文プレビューを含む
  python extract_emails.py --subject 会議 --sender sales --include-body --output meetings.csv
        """,
    )

    parser.add_argument(
        "--source-type",
        choices=["live", "pst"],
        default="live",
        help="メールのソース: live (アクティブな Outlook), pst (.pst ファイル)",
    )
    parser.add_argument(
        "--pst-path",
        type=str,
        default=None,
        help=".pst ファイルのフルパス (source-type=pst のみ必須)",
    )
    parser.add_argument(
        "--subject",
        type=str,
        default=None,
        help="件名に含まれる文字列フィルタ (大文字小文字区別なし、部分一致)",
    )
    parser.add_argument(
        "--sender",
        type=str,
        default=None,
        help="送信元に含まれる文字列フィルタ (名前またはメールアドレス、部分一致)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="output_emails.csv",
        help="出力ファイルパス (既定: output_emails.csv)",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="出力フォーマット (既定: csv)",
    )
    parser.add_argument(
        "--include-body",
        action="store_true",
        help="出力に本文プレビュー (500 文字) を含む",
    )

    args = parser.parse_args()

    extract_emails(
        source_type=args.source_type,
        pst_path=args.pst_path,
        subject_filter=args.subject,
        sender_filter=args.sender,
        output_path=args.output,
        output_format=args.format,
        include_body=args.include_body,
    )


if __name__ == "__main__":
    main()
