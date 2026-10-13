"""docs/current-initiatives.md との突合・評価対象判定。

docs/current-initiatives.md はユーザー管理の正本。ここでは記載内容を
パースして評価タイミングを判定するのみで、実装日・評価予定日を
コード側から推測・確定することはしない（unknownはunknownのまま扱う）。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path

from src_v2 import normalize
from src_v2.analysis.common import (
    SHORTFALL_COMPETITOR_SERP,
    SHORTFALL_IMPLEMENTED_AT,
    SHORTFALL_TARGET_PAGES,
    make_finding,
    sum_metrics,
)
from src_v2.analysis.confidence import assess_confidence

_KEY_MAP = {
    "対象": "target",
    "対象URL": "target_url",
    "状態": "status",
    "実装日": "implemented_at",
    "評価予定日": "evaluation_due",
    "メモ": "note",
    "仮説": "hypothesis",
    "成功シグナル": "success_signal",
}


def parse_initiatives_md(path: Path | str) -> list[dict]:
    """"## 施策名" 見出し + "- key: value" 行のフォーマットをパースする。"""
    text = Path(path).read_text(encoding="utf-8")
    initiatives: list[dict] = []
    current: dict | None = None

    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+)$", line.strip())
        if heading:
            if current is not None:
                initiatives.append(current)
            current = {"name": heading.group(1).strip()}
            continue

        item = re.match(r"^-\s*([^:：]+)[:：]\s*(.+)$", line.strip())
        if item and current is not None:
            key_ja = item.group(1).strip()
            value = item.group(2).strip()
            key = _KEY_MAP.get(key_ja)
            if key:
                current[key] = value

    if current is not None:
        initiatives.append(current)

    return initiatives


def _resolve_target_page_ids(initiative: dict, pages_cfg: list[dict]) -> list[str]:
    if initiative.get("target_url"):
        normalized = normalize.normalize_url(initiative["target_url"])["normalized_url"]
        return [p["id"] for p in pages_cfg if normalize.normalize_url(p["url"])["normalized_url"] == normalized]

    target_text = initiative.get("target", "")
    if "/branding/" in target_text and "Definition" in target_text:
        return [p["id"] for p in pages_cfg if p["group"] == "branding" and p["category"] == "definition"]
    if "/service/" in target_text and "Definition" in target_text:
        return [p["id"] for p in pages_cfg if p["group"] == "service" and p["category"] == "definition"]
    return []


def _slug(name: str) -> str:
    """英数字が全く含まれない施策名（日本語のみ等）でもidが衝突しないよう、
    ASCIIスラッグが空になる場合は名前のハッシュを使う（決定的・一意）。"""
    ascii_slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    if ascii_slug:
        return ascii_slug
    return "initiative-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def analyze_initiatives(
    initiatives_md_path: Path | str,
    bundle: dict,
    pages_cfg: list[dict],
    thresholds_cfg: dict,
    weeks_available: int = 1,
) -> list[dict]:
    initiatives = parse_initiatives_md(initiatives_md_path)
    findings: list[dict] = []

    review_cfg = thresholds_cfg["initiative_review"]
    day14 = review_cfg["day14_after_implementation"]
    day28 = review_cfg["day28_after_implementation"]
    reference_date = datetime.strptime(bundle["period"]["cur_end"], "%Y-%m-%d")

    page_daily_cur = bundle["gsc"]["current"]["gsc_page_daily"]
    page_daily_prv = bundle["gsc"]["comparison"]["gsc_page_daily"]

    for initiative in initiatives:
        name = initiative["name"]
        slug = _slug(name)
        implemented_at_raw = (initiative.get("implemented_at") or "").strip()
        target_ids = _resolve_target_page_ids(initiative, pages_cfg)

        if not implemented_at_raw or implemented_at_raw.lower() == "unknown":
            findings.append(make_finding(
                id=f"initiative-{slug}",
                category="initiative",
                severity="info",
                confidence="Not assessable",
                message=f"「{name}」は実装日が未確認のため評価対象外（14日/28日評価をスキップ）",
                recommendation="実装日が判明次第 docs/current-initiatives.md に追記してください",
                judgment="判断保留",
                shortfall=SHORTFALL_IMPLEMENTED_AT,
                evidence={
                    "name": name,
                    "status": initiative.get("status"),
                    "target": initiative.get("target") or initiative.get("target_url"),
                    "hypothesis": initiative.get("hypothesis"),
                    "success_signal": initiative.get("success_signal"),
                },
                source={"initiative": name},
            ))
            continue

        try:
            implemented_at = datetime.strptime(implemented_at_raw, "%Y-%m-%d")
        except ValueError:
            findings.append(make_finding(
                id=f"initiative-{slug}",
                category="initiative",
                severity="info",
                confidence="Not assessable",
                message=f"「{name}」の実装日「{implemented_at_raw}」を日付として解釈できない",
                recommendation="docs/current-initiatives.md の実装日をYYYY-MM-DD形式で記載してください",
                judgment="判断保留",
                shortfall=SHORTFALL_IMPLEMENTED_AT,
                source={"initiative": name},
            ))
            continue

        day14_date = implemented_at + timedelta(days=day14)
        day28_date = implemented_at + timedelta(days=day28)

        if reference_date < day14_date:
            findings.append(make_finding(
                id=f"initiative-{slug}",
                category="initiative",
                severity="info",
                confidence="Not assessable",
                message=f"「{name}」は一次評価（14日, {day14_date:%Y-%m-%d}予定）にまだ達していない",
                recommendation="観測を継続する",
                judgment="効果確認待ち",
                evidence={"implemented_at": implemented_at_raw, "day14": f"{day14_date:%Y-%m-%d}", "day28": f"{day28_date:%Y-%m-%d}"},
                source={"initiative": name},
            ))
            continue

        if reference_date < day28_date:
            findings.append(make_finding(
                id=f"initiative-{slug}",
                category="initiative",
                severity="info",
                confidence="Not assessable",
                message=f"「{name}」は一次評価時期（14日, {day14_date:%Y-%m-%d}）を通過。二次評価（28日, {day28_date:%Y-%m-%d}）待ち",
                recommendation="追加変更ではなく観測を継続する",
                judgment="効果確認待ち",
                evidence={"implemented_at": implemented_at_raw, "day14": f"{day14_date:%Y-%m-%d}", "day28": f"{day28_date:%Y-%m-%d}"},
                source={"initiative": name},
            ))
            continue

        # day28到達。対象ページのシグナルを確認する。
        if not target_ids:
            findings.append(make_finding(
                id=f"initiative-{slug}",
                category="initiative",
                severity="info",
                confidence="Not assessable",
                message=f"「{name}」は28日評価時期を過ぎているが、対象ページを自動解決できないためシグナルを評価できない",
                recommendation="docs/current-initiatives.md の対象記載を見直すか、手動で効果を確認してください",
                judgment="判断保留",
                shortfall=SHORTFALL_TARGET_PAGES,
                evidence={"implemented_at": implemented_at_raw, "target_raw": initiative.get("target") or initiative.get("target_url")},
                source={"initiative": name},
            ))
            continue

        cur = sum_metrics([r for r in page_daily_cur if r["page_id"] in target_ids])
        prv = sum_metrics([r for r in page_daily_prv if r["page_id"] in target_ids])

        clicks_improved = cur["clicks"] >= prv["clicks"]
        impressions_improved = cur["impressions"] >= prv["impressions"]

        confidence = assess_confidence({
            "impressions": cur["impressions"], "clicks": cur["clicks"],
            "weeks_available": weeks_available, "seo_aio_agree": None, "page_query_agree": None,
        }, thresholds_cfg)

        if clicks_improved and impressions_improved:
            findings.append(make_finding(
                id=f"initiative-{slug}",
                category="initiative",
                severity="info",
                confidence=confidence["level"],
                message=f"「{name}」は28日経過。対象ページ群のクリック・表示回数は横ばい〜改善（clicks {prv['clicks']}→{cur['clicks']}, impressions {prv['impressions']}→{cur['impressions']}）",
                recommendation="追加変更ではなく観測を継続する",
                judgment="効果確認待ち",
                evidence={"target_page_ids": target_ids, "current": cur, "comparison": prv},
                source={"initiative": name},
            ))
        else:
            findings.append(make_finding(
                id=f"initiative-{slug}",
                category="initiative",
                severity="warning",
                confidence=confidence["level"],
                message=f"「{name}」は28日経過。対象ページ群のクリックまたは表示回数が悪化（clicks {prv['clicks']}→{cur['clicks']}, impressions {prv['impressions']}→{cur['impressions']}）",
                recommendation="効果が確認できないため、施策内容の再調査を検討する",
                judgment="再調査",
                shortfall=SHORTFALL_COMPETITOR_SERP,
                evidence={"target_page_ids": target_ids, "current": cur, "comparison": prv},
                source={"initiative": name},
            ))

    return findings
