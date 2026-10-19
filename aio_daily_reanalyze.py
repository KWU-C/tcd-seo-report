#!/usr/bin/env python3
"""保存済みの日次AIOデータ（data/aio/daily/*.json）を現行の判定ロジックで再解析する。

SerpAPIへの追加リクエストは行わない。

  - 日次JSONには解析後の値しか残っていないため、引用URLは週次パイプラインが保存した
    生レスポンス（data_v2/raw/aio/*.json）のうち、同じ観測（同じ日付・クエリで、本文と
    引用元の並びが完全に一致するもの）からのみ復元する。一致しない記録のURLは空のまま残す。
  - TCD引用・TCD推薦・引用状況は、保存済みの本文と引用元から判定し直す。
  - 書き換え前の日次JSONとLooker用CSVは data/aio/backup/<実行時刻>/ にコピーして保持する。
  - Looker用CSVは再解析後の日次JSONから日付順に作り直す。

使い方:
  python3 aio_daily_reanalyze.py --dry-run   # 変更内容の集計だけ表示（書き込みなし）
  python3 aio_daily_reanalyze.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import aio_daily_monitor as monitor

DEFAULT_RAW_AIO_DIR = "data_v2/raw/aio"
DEFAULT_BACKUP_ROOT = "data/aio/backup"
LOGIC_VERSION = "2026-10-19.2"  # 推薦判定に会社一覧の完全性チェックを追加
JST = timedelta(hours=9)


def _raw_ai_overview(record: dict) -> dict | None:
    """週次rawレコードから、本文・引用元が入っている ai_overview を取り出す（fetch_aio.py の保存形式）。"""
    raw = record.get("raw_response") or {}
    candidates = [
        (raw.get("ai_overview_result") or {}).get("ai_overview"),
        (raw.get("search_result") or {}).get("ai_overview"),
    ]
    return next((c for c in candidates if c and (c.get("references") or c.get("text_blocks") or c.get("text"))), None)


def load_raw_index(raw_dir: Path) -> dict[tuple[str, str], list[tuple[str, dict]]]:
    """(JST日付, クエリ) → [(rawファイルパス, ai_overview)] の索引を作る。"""
    index: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for path in sorted(raw_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("acquisition_status") != "success":
            continue
        ai_ov = _raw_ai_overview(record)
        if not ai_ov:
            continue
        observed = datetime.strptime(record["observed_at"], "%Y-%m-%dT%H:%M:%SZ") + JST
        index.setdefault((observed.strftime("%Y-%m-%d"), record["query"]), []).append((str(path), ai_ov))
    return index


def is_same_observation(daily: dict, ai_ov: dict, alias_lookup: dict[str, str]) -> bool:
    """日次記録とraw ai_overview が同じ観測結果か（本文・引用元の件数・各引用元・タイトルの並びが一致）。

    日次記録の引用ドメインは旧ロジックで表示名から作られているため、両者を同じ別名辞書で
    正規化してから比べる。
    """
    if monitor.extract_aio_text(ai_ov) != (daily.get("aio_text") or ""):
        return False
    refs = [r for r in ai_ov.get("references", []) or [] if isinstance(r, dict)]
    stored_domains = daily.get("cited_domains") or []
    if len(refs) != len(stored_domains):
        return False
    titles = [r.get("title") for r in refs if r.get("title")]
    if titles != (daily.get("cited_titles") or []):
        return False
    for ref, stored in zip(refs, stored_domains):
        src = str(ref.get("source") or "")
        legacy = monitor._root_domain(src) if monitor._looks_like_domain(src) else src
        if monitor.normalize_domain(legacy, alias_lookup) != monitor.normalize_domain(stored, alias_lookup):
            return False
    return True


def reanalyze_record(
    record: dict,
    raw_candidates: list[tuple[str, dict]],
    alias_lookup: dict[str, str],
    rules: dict,
    reanalyzed_at: str,
) -> dict:
    """1件の日次記録を再解析した新しい記録を返す（引数は変更しない）。"""
    new = dict(record)
    raw_source = None
    for path, ai_ov in raw_candidates:
        if is_same_observation(record, ai_ov, alias_lookup):
            raw_source = path
            cited_urls, cited_domains, cited_titles, cited_sources = monitor.parse_references(ai_ov, alias_lookup)
            new.update({
                "cited_urls": cited_urls, "cited_domains": cited_domains,
                "cited_titles": cited_titles, "cited_sources": cited_sources,
            })
            break
    else:
        # 生レスポンスがない記録：旧ロジックの引用ドメインは表示名由来のため、表示名として残しつつ別名で正規化する
        stored = record.get("cited_domains") or []
        new["cited_sources"] = record.get("cited_sources") or list(stored)
        new["cited_domains"] = [monitor.normalize_domain(d, alias_lookup) for d in stored]
        new["cited_urls"] = record.get("cited_urls") or []

    tcd_domain = rules.get("tcd_domain", monitor.TCD_DOMAIN_DEFAULT)
    new["competitor_domains"] = monitor.competitor_domains(new["cited_domains"], tcd_domain)
    new.update(monitor.evaluate_observation(
        new["query"], new["status"], new.get("aio_exists"), new.get("aio_text"),
        new["cited_urls"], new["cited_domains"], new["cited_sources"], rules,
    ))
    new["reanalysis"] = {
        "reanalyzed_at": reanalyzed_at,
        "logic_version": LOGIC_VERSION,
        "cited_urls_source": raw_source,  # Noneは生レスポンスと照合できず、引用URLを復元できなかった記録
    }
    return new


_COMPARED_FIELDS = ("tcd_cited", "tcd_mentioned", "tcd_recommended", "recommendation_status", "citation_status")


def backup(daily_dir: Path, looker_dir: Path, backup_root: Path, stamp: str) -> Path:
    dest = backup_root / stamp
    if dest.exists():
        raise FileExistsError(f"バックアップ先が既に存在します: {dest}")
    shutil.copytree(daily_dir, dest / "daily")
    if looker_dir.exists():
        shutil.copytree(looker_dir, dest / "looker")
    return dest


def rebuild_looker_csvs(daily_dir: Path, looker_dir: Path) -> None:
    """日次JSONから日付順にLooker用CSVを作り直す（一時ディレクトリで作成してから置き換える）。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for path in sorted(daily_dir.glob("*.json")):
            results = json.loads(path.read_text(encoding="utf-8"))
            monitor.update_looker_csvs(results, tmp_dir, path.stem)
        looker_dir.mkdir(parents=True, exist_ok=True)
        for name in (monitor.AIO_DAILY_CSV_NAME, monitor.AIO_DOMAINS_CSV_NAME):
            shutil.copyfile(tmp_dir / name, looker_dir / name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="日次AIOデータの再解析（APIは使用しない）")
    parser.add_argument("--config", default=monitor.DEFAULT_CONFIG_PATH)
    parser.add_argument("--daily-json-dir", default=monitor.DEFAULT_DAILY_JSON_DIR)
    parser.add_argument("--looker-dir", default=monitor.DEFAULT_LOOKER_DIR)
    parser.add_argument("--raw-aio-dir", default=DEFAULT_RAW_AIO_DIR)
    parser.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--dry-run", action="store_true", help="書き込みせず変更内容の集計だけ表示する")
    args = parser.parse_args(argv)

    config = monitor.load_config(args.config)
    rules = monitor.build_judgement_rules(config)
    alias_lookup = monitor.build_domain_alias_lookup(config.get("domain_aliases"))
    daily_dir, looker_dir = Path(args.daily_json_dir), Path(args.looker_dir)
    raw_index = load_raw_index(Path(args.raw_aio_dir))

    now = datetime.now()
    reanalyzed_at = now.isoformat(timespec="seconds")
    outputs: dict[Path, list[dict]] = {}
    n_records = n_recovered = 0
    changes: dict[str, int] = {f: 0 for f in _COMPARED_FIELDS}
    recovered: list[str] = []

    for path in sorted(daily_dir.glob("*.json")):
        records = json.loads(path.read_text(encoding="utf-8"))
        new_records = []
        for r in records:
            new = reanalyze_record(r, raw_index.get((path.stem, r["query"]), []), alias_lookup, rules, reanalyzed_at)
            n_records += 1
            if new["reanalysis"]["cited_urls_source"]:
                n_recovered += 1
                recovered.append(f"{path.stem} {r['query']} ({len(new['cited_urls'])} URL)")
            for f in _COMPARED_FIELDS:
                if r.get(f) != new.get(f):
                    changes[f] += 1
            new_records.append(new)
        outputs[path] = new_records

    print(f"再解析対象: {n_records}件（{len(outputs)}日分）")
    print(f"引用URLを生レスポンスから復元: {n_recovered}件")
    for line in recovered:
        print(f"  - {line}")
    print("判定が変わった件数: " + ", ".join(f"{k}={v}" for k, v in changes.items()))

    if args.dry_run:
        print("--dry-run のため書き込みは行いません。")
        return 0

    dest = backup(daily_dir, looker_dir, Path(args.backup_root), now.strftime("%Y%m%dT%H%M%S"))
    print(f"元データをバックアップ: {dest}")
    for path, new_records in outputs.items():
        path.write_text(json.dumps(new_records, ensure_ascii=False, indent=2), encoding="utf-8")
    rebuild_looker_csvs(daily_dir, looker_dir)
    print(f"日次JSONとLooker用CSVを更新: {daily_dir} / {looker_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
