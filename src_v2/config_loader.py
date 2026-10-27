"""config_v2/ 配下の設定ファイルローダー。

pages.yaml / queries.yaml / thresholds.yaml を読み込む唯一の入口。
以後のモジュールはYAMLを直接開かず、ここを経由する
（requirements.md: 「コードへのURLハードコードは禁止」を満たすため、
 URL・クエリ・閾値の実データはすべてこのローダー経由で取得する）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path("config_v2")

_VALID_GROUPS = {"service", "branding"}
_VALID_CATEGORIES = {"service", "definition"}


class PagesConfigError(ValueError):
    """pages.yaml の内容が不正（重複URL・重複id・不正な値）。"""


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_pages(path: Path | str = CONFIG_DIR / "pages.yaml") -> list[dict]:
    """監視対象マスタを読み込み、id/URL重複や不正なgroup/categoryがあれば例外を出す。

    「マガジン上位ランディングページ」はここに含まれない（動的抽出。
    requirements.md 4節）。
    """
    data = _load_yaml(Path(path))
    pages = data.get("pages", [])

    ids = [p["id"] for p in pages]
    urls = [p["url"] for p in pages]
    dup_ids = {i for i in ids if ids.count(i) > 1}
    dup_urls = {u for u in urls if urls.count(u) > 1}
    if dup_ids:
        raise PagesConfigError(f"pages.yaml: id重複 {dup_ids}")
    if dup_urls:
        raise PagesConfigError(f"pages.yaml: URL重複 {dup_urls}")

    for p in pages:
        if p.get("group") not in _VALID_GROUPS:
            raise PagesConfigError(f"pages.yaml: 不正なgroup id={p.get('id')} group={p.get('group')}")
        if p.get("category") not in _VALID_CATEGORIES:
            raise PagesConfigError(f"pages.yaml: 不正なcategory id={p.get('id')} category={p.get('category')}")

    return pages


def load_queries(path: Path | str = CONFIG_DIR / "queries.yaml") -> dict:
    return _load_yaml(Path(path))


def load_thresholds(path: Path | str = CONFIG_DIR / "thresholds.yaml") -> dict:
    return _load_yaml(Path(path))


def load_excluded_landing_pages(path: Path | str = CONFIG_DIR / "excluded_landing_pages.yaml") -> dict:
    """「⑧ 上位ランディングページ」抽出から除外するURL一覧。
    ファイル不在時は空リスト扱い（初期状態）。"""
    path = Path(path)
    if not path.exists():
        return {"excluded_urls": []}
    return _load_yaml(path) or {"excluded_urls": []}


def load_pipeline_config(path: Path | str = CONFIG_DIR / "pipeline.yaml") -> dict:
    """GSC対象期間の確定日カットオフ設定・GA4セグメント設定。"""
    return _load_yaml(Path(path))


def load_report_notes(path: Path | str = CONFIG_DIR / "report_notes.yaml") -> list[dict]:
    """レポートに期間限定で差し込む注記。ファイル不在時は注記なし。"""
    path = Path(path)
    if not path.exists():
        return []
    return (_load_yaml(path) or {}).get("notes") or []


class InquiriesConfigError(ValueError):
    """inquiries.yaml の内容が不正（件数の欠落・負数・内訳と総数の不一致・期間の重複）。"""


INQUIRY_COUNT_KEYS = (
    "general_total", "project_consultation", "sales_solicitation", "other", "other_needs_review", "recruiting_form",
)
_INQUIRY_BREAKDOWN_KEYS = ("project_consultation", "sales_solicitation", "other")


def _parse_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as e:
        raise InquiriesConfigError(f"inquiries.yaml: {label} の日付が不正: {value}") from e


def load_inquiries(path: Path | str = CONFIG_DIR / "inquiries.yaml") -> list[dict]:
    """確定済みの週次問い合わせ件数（件数のみ。個人情報は持たない）。ファイル不在時は空（全週「未入力」）。

    未入力と0件を区別するため、件数の項目が1つでも欠けている週はエラーにする（0件として補完しない）。
    一般問い合わせの内訳（案件相談＋売り込み＋その他）が総数と一致しない週もエラーにする。
    """
    path = Path(path)
    if not path.exists():
        return []
    weeks = (_load_yaml(path) or {}).get("weeks") or []

    parsed = []
    for i, w in enumerate(weeks):
        label = f"weeks[{i}]"
        start, end = _parse_date(w.get("start"), f"{label}.start"), _parse_date(w.get("end"), f"{label}.end")
        if (end - start).days != 6:
            raise InquiriesConfigError(f"inquiries.yaml: {label} は7日間ではない（{start}〜{end}）")
        for key in INQUIRY_COUNT_KEYS:
            value = w.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InquiriesConfigError(f"inquiries.yaml: {label}（{start}〜{end}）の {key} は0以上の整数で入力する: {value!r}")
        breakdown = sum(w[k] for k in _INQUIRY_BREAKDOWN_KEYS)
        if breakdown != w["general_total"]:
            raise InquiriesConfigError(
                f"inquiries.yaml: {label}（{start}〜{end}）の内訳合計 {breakdown}"
                f"（案件相談+売り込み・提携勧誘+その他）が一般問い合わせ {w['general_total']} と一致しない")
        if w["other_needs_review"] > w["other"]:
            raise InquiriesConfigError(f"inquiries.yaml: {label}（{start}〜{end}）の other_needs_review が other を超えている")
        parsed.append({**w, "start": start.isoformat(), "end": end.isoformat()})

    ordered = sorted(parsed, key=lambda w: w["start"])
    for prev, cur in zip(ordered, ordered[1:]):
        if cur["start"] <= prev["end"]:
            raise InquiriesConfigError(f"inquiries.yaml: 期間が重複している（{prev['start']}〜{prev['end']} と {cur['start']}〜{cur['end']}）")
    return ordered


def enabled_pages(pages: list[dict]) -> list[dict]:
    return [p for p in pages if p.get("enabled")]
