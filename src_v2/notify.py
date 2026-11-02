#!/usr/bin/env python3
"""週次自動運用の通知（担当者宛て。配信先 unei@tcd.jp には送らない）。

- failure: 取得失敗・データ品質fail・生成失敗・送信失敗。失敗した工程とログ末尾を送る。
  同じ週・同じ工程の失敗通知は1回だけ（再試行のたびに届かないようにする）。
- review: 配信後、確認が必要な項目がある場合だけ送る（問い合わせ未集計・不整合・取得失敗、
  AIO判定不可、GSC期間のずれ、GA4取得失敗、データ品質warning）。
通知メールを送れない場合（認証切れ・通信断）は、macOS の通知センターとログに残す。
本文に問い合わせの個人情報・本文は含まない（レポートと同じ件数・状態のみ）。
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

NOTIFIED_DIR = Path("state/notified")
SUBJECT_PREFIX = "[reports_v2 自動運用]"


def _tail(path: Path | None, lines: int = 40) -> str:
    if not path or not path.exists():
        return "（ログなし）"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def collect_review_items(report_dir: Path) -> list[str]:
    """report.json から、配信後に担当者が確認すべき項目を集める。"""
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    items: list[str] = []
    header = report.get("header", {})
    period = header.get("period", {})

    inq = report.get("inquiries", {})
    if inq.get("status_label") and not inq.get("entered"):
        items.append(f"問い合わせ実績: {inq['status_label']}")
    items += [f"問い合わせ実績: {w}" for w in inq.get("warnings", [])]

    if period.get("gsc_shift_days"):
        items.append(f"検索順位: Search Consoleの確定遅れのため、末日を{period['gsc_shift_days']}日前にずらした"
                     f"（{period.get('cur_start')}〜{period.get('cur_end')}）")

    for row in (report.get("priority_markets") or {}).get("table", []):
        if row.get("citation_unavailable"):
            items.append(f"AIO: {row.get('short_label') or row.get('label')} は本文を取得できず判定不可")

    for seg in (report.get("site_usage") or {}).get("segments", []):
        if seg.get("error"):
            items.append(f"GA4: {seg.get('label')} の取得に失敗（{seg['error'][:120]}）")

    if header.get("data_quality_status") not in (None, "pass"):
        items.append(f"データ品質: {header.get('data_quality_label')}")
    return items


def _mac_notify(title: str, message: str) -> None:
    script = f'display notification {json.dumps(message[:200])} with title {json.dumps(title)}'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def send_text_email(to: str, subject: str, body: str, gmail_service: Any = None,
                    credentials_file: str = "credentials.json") -> bool:
    """送れたら True。失敗しても例外にせず、通知センターに出して False を返す。"""
    try:
        if gmail_service is None:
            from src_v2.auth import get_gmail_service
            gmail_service = get_gmail_service(credentials_file)
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["To"] = to
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except Exception as e:  # noqa: BLE001 - 通知の失敗で処理全体を止めない
        print(f"通知メールを送れなかった: {type(e).__name__}: {e}", file=sys.stderr)
        _mac_notify(SUBJECT_PREFIX, subject)
        return False


def failure_message(week: str, step: str, log_path: Path | None, next_retry: str | None) -> tuple[str, str]:
    subject = f"{SUBJECT_PREFIX} 失敗: {week} 週（{step}）"
    lines = [
        f"週次レポート（{week} 週）の自動実行が「{step}」で失敗しました。レポートは配信されていません。",
        "",
        f"次の自動再試行: {next_retry or 'なし（手動で再実行してください）'}",
        f"手動での再実行: scripts/weekly_report.sh --week {week}",
        "（送信済みの週は送信済み台帳により再送されません）",
        "",
        f"ログ: {log_path or '-'}",
        "---- ログ末尾 ----",
        _tail(log_path),
    ]
    return subject, "\n".join(lines)


def review_message(week: str, items: list[str], to: str) -> tuple[str, str]:
    subject = f"{SUBJECT_PREFIX} 配信済み・要確認 {len(items)}件: {week} 週"
    lines = [f"週次レポート（{week} 週）を {to} へ配信しました。次の項目を確認してください。", ""]
    lines += [f"- {item}" for item in items]
    lines += ["", "問い合わせ実績はスプレッドシートを更新後、次週のレポートの累計に反映されます（配信済みの週は自動で再送しません）。"]
    return subject, "\n".join(lines)


def _already_notified(week: str, step: str) -> bool:
    return (NOTIFIED_DIR / f"{week}_{step}.flag").exists()


def _mark_notified(week: str, step: str) -> None:
    NOTIFIED_DIR.mkdir(parents=True, exist_ok=True)
    (NOTIFIED_DIR / f"{week}_{step}.flag").write_text("notified\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="reports_v2 週次自動運用の通知")
    parser.add_argument("--kind", choices=["failure", "review"], required=True)
    parser.add_argument("--week", required=True, help="レポート週の月曜日（YYYY-MM-DD）")
    parser.add_argument("--to", required=True, help="通知先（担当者）")
    parser.add_argument("--step", help="failure: 失敗した工程")
    parser.add_argument("--log", help="failure: ログファイル")
    parser.add_argument("--next-retry", help="failure: 次の自動再試行の予定")
    parser.add_argument("--report-dir", help="review: レポートのフォルダ")
    parser.add_argument("--delivered-to", default="unei@tcd.jp", help="review: 配信先（本文の表記用）")
    parser.add_argument("--dry-run", action="store_true", help="送らずに内容を表示する")
    args = parser.parse_args()

    if args.kind == "failure":
        step = args.step or "不明"
        if not args.dry_run and _already_notified(args.week, step):
            print(f"通知済みのため送らない（{args.week} / {step}）")
            _mac_notify(SUBJECT_PREFIX, f"{args.week} 週の再試行も「{step}」で失敗")
            return
        subject, body = failure_message(args.week, step, Path(args.log) if args.log else None, args.next_retry)
    else:
        items = collect_review_items(Path(args.report_dir))
        if not items:
            print("要確認の項目なし（通知しない）")
            return
        subject, body = review_message(args.week, items, args.delivered_to)

    if args.dry_run:
        print(f"[dry-run] To: {args.to}\nSubject: {subject}\n\n{body}")
        return
    if send_text_email(args.to, subject, body) and args.kind == "failure":
        _mark_notified(args.week, args.step or "不明")


if __name__ == "__main__":
    main()
