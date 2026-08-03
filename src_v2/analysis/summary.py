#!/usr/bin/env python3
"""分析結果の統合。

market.py / pages.py / initiatives.py の出力（Findingのリスト）を集約し、
analysis.json として書き出す。HTML・Markdownなどのレポート生成は、
以後この analysis.json のみを参照する（rawやnormalizedを直接参照しない）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src_v2 import config_loader
from src_v2.analysis.initiatives import analyze_initiatives
from src_v2.analysis.landing_pages import analyze_landing_pages
from src_v2.analysis.market import analyze_market
from src_v2.analysis.pages import analyze_pages
from src_v2.schema import SCHEMA_VERSION

PROCESSED_DIR = Path("data_v2/processed")
INITIATIVES_MD = Path("docs/current-initiatives.md")


def _period_dirs(processed_root: Path) -> list[Path]:
    return sorted(
        d for d in processed_root.iterdir()
        if d.is_dir() and (d / "normalized.json").exists()
    )


def _load_bundle(period_dir: Path) -> dict:
    return json.loads((period_dir / "normalized.json").read_text(encoding="utf-8"))


def _find_previous_bundle(processed_root: Path, period: dict) -> dict | None:
    prv_dir = processed_root / f"{period['prv_start']}_{period['prv_end']}"
    if (prv_dir / "normalized.json").exists():
        return _load_bundle(prv_dir)
    return None


def _recent_bundles(processed_root: Path, up_to_cur_end: str, max_periods: int = 4) -> list[tuple[str, dict]]:
    """up_to_cur_end以前（含む）の直近max_periods件を(cur_end, bundle)の古い順で返す。
    AIO競合ドメインの複数週継続性判定（market.py）に使う。"""
    dirs = _period_dirs(processed_root)
    result: list[tuple[str, dict]] = []
    for d in dirs:
        bundle = _load_bundle(d)
        if bundle["period"]["cur_end"] <= up_to_cur_end:
            result.append((bundle["period"]["cur_end"], bundle))
    result.sort(key=lambda item: item[0])
    return result[-max_periods:]


def _count_weeks_available(processed_root: Path, up_to_cur_end: str) -> int:
    """up_to_cur_end以前に完了しているレポート週の数（今回含む）。
    「複数週の継続性」Confidence判定に使う。過大評価しないよう、
    normalized.jsonが実在する週のみ数える。
    """
    dirs = _period_dirs(processed_root)
    count = 0
    for d in dirs:
        bundle = _load_bundle(d)
        if bundle["period"]["cur_end"] <= up_to_cur_end:
            count += 1
    return max(count, 1)


def _data_summary(bundle: dict, excluded_cfg: dict | None) -> dict:
    """記述的メタデータのみ（取得件数・比較週有無・除外リスト）。判定ロジックは含まない。
    Phase 5のレポート層がanalysis.json以外（normalized.json・config_v2/等）を
    参照せずにヘッダー情報を出せるようにするために付与する。
    """
    gsc_cur = bundle["gsc"]["current"]
    gsc_prv = bundle["gsc"]["comparison"]
    return {
        "gsc": {
            "query_daily_rows": len(gsc_cur["gsc_query_daily"]),
            "page_daily_rows": len(gsc_cur["gsc_page_daily"]),
            "page_query_daily_rows": len(gsc_cur["gsc_page_query_daily"]),
            "comparison_available": len(gsc_prv["gsc_query_daily"]) > 0,
        },
        "aio": bundle["data_quality"]["aio_counts"],
        "magazine_landing_pages": {
            "excluded_urls": (excluded_cfg or {}).get("excluded_urls", []),
        },
    }


def _counts(findings: list[dict]) -> dict:
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for f in findings:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        by_category[f["category"]] = by_category.get(f["category"], 0) + 1
    return {"total": len(findings), "by_severity": by_severity, "by_category": by_category}


def run_analysis(
    period_dir: Path,
    processed_root: Path = PROCESSED_DIR,
    initiatives_md_path: Path = INITIATIVES_MD,
) -> Path:
    bundle = _load_bundle(period_dir)
    period = bundle["period"]

    pages_cfg = config_loader.load_pages()
    queries_cfg = config_loader.load_queries()
    thresholds_cfg = config_loader.load_thresholds()
    excluded_cfg = config_loader.load_excluded_landing_pages()

    previous_bundle = _find_previous_bundle(processed_root, period)
    weeks_available = _count_weeks_available(processed_root, period["cur_end"])
    recent_bundles = _recent_bundles(processed_root, period["cur_end"], max_periods=4)

    findings: list[dict] = []
    findings += analyze_market(bundle, previous_bundle, queries_cfg, thresholds_cfg, weeks_available, recent_bundles)
    findings += analyze_pages(bundle, config_loader.enabled_pages(pages_cfg), thresholds_cfg, weeks_available)
    findings += analyze_landing_pages(bundle, excluded_cfg, thresholds_cfg, weeks_available, top_n=10)

    if initiatives_md_path.exists():
        findings += analyze_initiatives(initiatives_md_path, bundle, pages_cfg, thresholds_cfg, weeks_available)
    else:
        print(f"警告: {initiatives_md_path} が見つからないため施策評価をスキップします", file=sys.stderr)

    analysis = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": period,
        "data_quality": bundle["data_quality"],
        "data_summary": _data_summary(bundle, excluded_cfg),
        "weeks_available": weeks_available,
        "ga4_summary": bundle.get("ga4", {}),
        "findings": findings,
        "counts": _counts(findings),
    }

    out_path = period_dir / "analysis.json"
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="TCD週次SEO/AIOレポート v2 Phase 4 分析")
    parser.add_argument("--period-dir", help="data_v2/processed/配下の期間ディレクトリ。省略時は最新を使う")
    args = parser.parse_args()

    if args.period_dir:
        period_dir = Path(args.period_dir)
    else:
        dirs = _period_dirs(PROCESSED_DIR)
        if not dirs:
            print("normalized.jsonが見つかりません。先に src_v2.pipeline を実行してください。", file=sys.stderr)
            sys.exit(1)
        period_dir = dirs[-1]

    out_path = run_analysis(period_dir)
    analysis = json.loads(out_path.read_text(encoding="utf-8"))
    print(f"分析結果出力: {out_path}")
    print(f"Finding数: {analysis['counts']['total']} {analysis['counts']['by_severity']}")


if __name__ == "__main__":
    main()
