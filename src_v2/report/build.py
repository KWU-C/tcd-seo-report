#!/usr/bin/env python3
"""Phase 5: analysis.json のみを入力に report.json / report.md / report.html を生成する。

GSC raw、AIO raw、normalized.json、config_v2/pages.yaml、
docs/current-initiatives.md はここでは一切読まない
（read対象は analysis.json 1ファイルのみ）。

Executive Summary（①今週の結論）の「AI要約」について:
このスクリプト自身はLLM APIを呼び出さない（追加課金・新規APIキーを避けるため）。
--narrative-file を指定しない場合は数値ベースの下書き箇条書きが入る。
実運用フロー:
  1. python3 -m src_v2.report.build            … 下書き入りでreport.*を生成
  2. Claude Codeが同じ analysis.json を読み、経営者向けの文章を執筆
  3. その文章をテキストファイルに保存し、
     python3 -m src_v2.report.build --narrative-file <path> で再生成
     （report.*の①だけが文章に差し替わる。②以降のデータは変わらない）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src_v2.report.render_html import render_html
from src_v2.report.render_json import render_json
from src_v2.report.render_md import render_markdown
from src_v2.report.view_model import build_view_model

PROCESSED_DIR = Path("data_v2/processed")
REPORTS_DIR = Path("reports_v2")


def _latest_analysis_path(processed_root: Path = PROCESSED_DIR) -> Path:
    candidates = sorted(
        p for p in processed_root.glob("*/analysis.json") if p.is_file()
    )
    if not candidates:
        raise FileNotFoundError(
            f"{processed_root} 配下にanalysis.jsonが見つかりません。先に src_v2.analysis.summary を実行してください。"
        )
    return candidates[-1]


def build_reports(
    analysis_path: Path,
    output_root: Path = REPORTS_DIR,
    narrative_path: Path | None = None,
) -> dict[str, Path]:
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    vm = build_view_model(analysis)

    if narrative_path is not None:
        vm["executive_summary"]["narrative"] = narrative_path.read_text(encoding="utf-8")

    period = vm["header"]["period"]
    out_dir = output_root / f"{period['cur_start']}_{period['cur_end']}"
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = {
        "json": out_dir / "report.json",
        "md": out_dir / "report.md",
        "html": out_dir / "report.html",
    }
    paths["json"].write_text(render_json(vm), encoding="utf-8")
    paths["md"].write_text(render_markdown(vm), encoding="utf-8")
    paths["html"].write_text(render_html(vm), encoding="utf-8")

    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="TCD週次SEO/AIOレポート v2 Phase 5 レポート生成")
    parser.add_argument("--analysis-path", help="analysis.jsonのパス。省略時はdata_v2/processed/配下の最新を使う")
    parser.add_argument("--narrative-file", help="①今週の結論に差し込む、Claude Code執筆済みの文章ファイル")
    args = parser.parse_args()

    analysis_path = Path(args.analysis_path) if args.analysis_path else _latest_analysis_path()
    narrative_path = Path(args.narrative_file) if args.narrative_file else None
    paths = build_reports(analysis_path, narrative_path=narrative_path)

    print(f"入力: {analysis_path}")
    for fmt, path in paths.items():
        print(f"出力({fmt}): {path}")


if __name__ == "__main__":
    main()
