#!/usr/bin/env python3
"""生成済みレポート（reports_v2/{週}/report.html）をGmail APIで送信する。

build.py（レポート生成）とは責務を分離する。ここでは既存のreport.htmlを
読み込んで送信するだけで、レポート内容の生成・加工は行わない。

二重配信の防止（2026-11-02〜）:
- 送る週は --report-dir で必ず明示する（「最新のフォルダ」を自動で選ばない。送る週の取り違えを防ぐ）。
- 送信に成功したら送信済み台帳 state/sent/<週フォルダ名>.json に宛先・日時・GmailメッセージIDを記録する。
- 台帳に同じ宛先の記録がある週は送らない。再送は --force-resend を明示したときだけ。
  （2026-09-07 に手動実行と自動実行が同時に動いて二重送信した事故への対策）
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

REPORTS_DIR = Path("reports_v2")
SENT_LEDGER_DIR = Path("state/sent")
JST = timezone(timedelta(hours=9))


class AlreadySentError(RuntimeError):
    """送信済み台帳に同じ週・同じ宛先の記録がある。"""


def _latest_report_dir(reports_root: Path = REPORTS_DIR) -> Path:
    candidates = sorted(p for p in reports_root.glob("*/report.html") if p.is_file())
    if not candidates:
        raise FileNotFoundError(
            f"{reports_root} 配下にreport.htmlが見つかりません。先に src_v2.report.build を実行してください。"
        )
    return candidates[-1].parent


def _subject_for(report_dir: Path) -> str:
    report_json_path = report_dir / "report.json"
    if report_json_path.exists():
        report = json.loads(report_json_path.read_text(encoding="utf-8"))
        header = report.get("header", {})
        title = (header.get("periods") or {}).get("title")
        if title:
            return f"TCD.jp 週次SEO/AIOレポート（v2） {title}"
        period = header.get("period", {})
        cur_start, cur_end = period.get("cur_start"), period.get("cur_end")
        if cur_start and cur_end:
            return f"TCD.jp 週次SEO/AIOレポート（v2） {cur_start} 〜 {cur_end}"
    return f"TCD.jp 週次SEO/AIOレポート（v2） {report_dir.name}"


def ledger_path(report_dir: Path, ledger_dir: Path = SENT_LEDGER_DIR) -> Path:
    return ledger_dir / f"{report_dir.name}.json"


def read_ledger(report_dir: Path, ledger_dir: Path = SENT_LEDGER_DIR) -> dict:
    path = ledger_path(report_dir, ledger_dir)
    if not path.exists():
        return {"report_dir": report_dir.name, "sends": []}
    return json.loads(path.read_text(encoding="utf-8"))


def already_sent(report_dir: Path, to: str, ledger_dir: Path = SENT_LEDGER_DIR) -> dict | None:
    """同じ週・同じ宛先の送信記録（無ければ None）。"""
    for record in read_ledger(report_dir, ledger_dir)["sends"]:
        if record.get("to", "").lower() == to.lower():
            return record
    return None


def record_send(report_dir: Path, to: str, message_id: str | None, subject: str,
                ledger_dir: Path = SENT_LEDGER_DIR) -> Path:
    ledger = read_ledger(report_dir, ledger_dir)
    ledger["sends"].append({
        "to": to, "subject": subject, "message_id": message_id,
        "sent_at": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
    })
    path = ledger_path(report_dir, ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def send_html_email(gmail_service: Any, to: str, subject: str, html_content: str) -> str | None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    resp = gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return (resp or {}).get("id")


def send_report(report_dir: Path, to: str, credentials_file: str = "credentials.json",
                force_resend: bool = False, ledger_dir: Path = SENT_LEDGER_DIR,
                gmail_service: Any = None) -> str | None:
    html_path = report_dir / "report.html"
    if not html_path.exists():
        raise FileNotFoundError(f"{html_path} が見つかりません。先に src_v2.report.build を実行してください。")

    previous = already_sent(report_dir, to, ledger_dir)
    if previous and not force_resend:
        raise AlreadySentError(
            f"{report_dir.name} は {to} へ送信済み（{previous.get('sent_at')}）。再送する場合は --force-resend を指定する")

    html_content = html_path.read_text(encoding="utf-8")
    subject = _subject_for(report_dir)

    if gmail_service is None:
        from src_v2.auth import get_gmail_service
        gmail_service = get_gmail_service(credentials_file)
    message_id = send_html_email(gmail_service, to, subject, html_content)
    record_send(report_dir, to, message_id, subject, ledger_dir)
    return message_id


def main() -> None:
    parser = argparse.ArgumentParser(description="TCD週次SEO/AIOレポート v2 送信")
    parser.add_argument("--report-dir", required=True, help="reports_v2/配下のレポートディレクトリ（送る週を明示する）")
    parser.add_argument("--to", required=True, help="送信先メールアドレス")
    parser.add_argument("--force-resend", action="store_true", help="送信済み台帳に記録がある週を再送する")
    parser.add_argument("--credentials-file", default="credentials.json")
    args = parser.parse_args()

    report_dir = Path(args.report_dir)

    print(f"入力: {report_dir / 'report.html'}")
    print(f"送信先: {args.to}")
    try:
        message_id = send_report(report_dir, args.to, args.credentials_file, force_resend=args.force_resend)
    except AlreadySentError as e:
        print(f"送信しません: {e}", file=sys.stderr)
        sys.exit(3)
    except FileNotFoundError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"送信完了（message_id={message_id}、台帳: {ledger_path(report_dir)}）")


if __name__ == "__main__":
    main()
