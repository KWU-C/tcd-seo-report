#!/usr/bin/env python3
"""生成済みレポート（reports_v2/{period}/report.html）をGmail APIで送信する。

build.py（レポート生成）とは責務を分離する。ここでは既存のreport.htmlを
読み込んで送信するだけで、レポート内容の生成・加工は行わない。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from src_v2.auth import get_gmail_service

REPORTS_DIR = Path("reports_v2")


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
        period = report.get("header", {}).get("period", {})
        cur_start, cur_end = period.get("cur_start"), period.get("cur_end")
        if cur_start and cur_end:
            return f"TCD.jp 週次SEO/AIOレポート（v2） {cur_start} 〜 {cur_end}"
    return f"TCD.jp 週次SEO/AIOレポート（v2） {report_dir.name}"


def send_html_email(gmail_service: Any, to: str, subject: str, html_content: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_report(report_dir: Path, to: str, credentials_file: str = "credentials.json") -> None:
    html_path = report_dir / "report.html"
    if not html_path.exists():
        raise FileNotFoundError(f"{html_path} が見つかりません。先に src_v2.report.build を実行してください。")

    html_content = html_path.read_text(encoding="utf-8")
    subject = _subject_for(report_dir)

    gmail_service = get_gmail_service(credentials_file)
    send_html_email(gmail_service, to, subject, html_content)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCD週次SEO/AIOレポート v2 送信")
    parser.add_argument("--report-dir", help="reports_v2/配下のレポートディレクトリ。省略時は最新を使う")
    parser.add_argument("--to", required=True, help="送信先メールアドレス")
    parser.add_argument("--credentials-file", default="credentials.json")
    args = parser.parse_args()

    report_dir = Path(args.report_dir) if args.report_dir else _latest_report_dir()

    print(f"入力: {report_dir / 'report.html'}")
    print(f"送信先: {args.to}")
    try:
        send_report(report_dir, args.to, args.credentials_file)
    except FileNotFoundError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    print("送信完了")


if __name__ == "__main__":
    main()
