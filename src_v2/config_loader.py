"""config_v2/ 配下の設定ファイルローダー。

pages.yaml / queries.yaml / thresholds.yaml を読み込む唯一の入口。
以後のモジュールはYAMLを直接開かず、ここを経由する
（requirements.md: 「コードへのURLハードコードは禁止」を満たすため、
 URL・クエリ・閾値の実データはすべてこのローダー経由で取得する）。
"""
from __future__ import annotations

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


def enabled_pages(pages: list[dict]) -> list[dict]:
    return [p for p in pages if p.get("enabled")]
