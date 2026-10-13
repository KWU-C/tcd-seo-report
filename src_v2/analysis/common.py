"""Analysisレイヤー共通ヘルパー。

Finding生成、週次集計など、market.py / pages.py / initiatives.py が
共通して使う処理のみを置く。業務判断（severity/judgmentの決定）は
各モジュール側に置き、ここには置かない。
"""
from __future__ import annotations

# 掲載順位別の期待CTR（Backlinko 2023調査ベース。generate_report.pyのEXPECTED_CTR_BY_POSを踏襲）
EXPECTED_CTR_BY_POSITION: dict[int, float] = {
    1: 0.284, 2: 0.152, 3: 0.107, 4: 0.079, 5: 0.060,
    6: 0.047, 7: 0.038, 8: 0.031, 9: 0.026, 10: 0.022,
}

VALID_SEVERITIES = {"critical", "warning", "info"}
VALID_JUDGMENTS = {"実施", "継続観測", "効果確認待ち", "再調査", "判断保留", "表示なし", "参考"}

# 「判断保留」「再調査」「表示なし」で、判断のために何が不足しているか（レポートに「判断｜不足：X」で表示）
SHORTFALL_SAMPLE = "サンプル数"
SHORTFALL_COMPETITOR_SERP = "競合SERP確認"
SHORTFALL_INDEX = "インデックス確認"
SHORTFALL_IMPLEMENTED_AT = "実装日"
SHORTFALL_TARGET_PAGES = "対象ページ"

# 「不足」ではなく次に調べるべきこと（レポートに「判断｜要調査：X」で表示）。
# 市場でImp=0の場合に使う。インデックス未確認（SHORTFALL_INDEX）は、個別ページで
# GSCデータが存在しない等、実際にインデックス状況の確認が必要なケースに限定する。
INVESTIGATE_SERP = "SERP確認"


def make_finding(
    id: str,
    category: str,
    severity: str,
    confidence: str,
    message: str,
    recommendation: str,
    *,
    judgment: str | None = None,
    evidence: dict | None = None,
    source: dict | None = None,
    shortfall: str | None = None,
    investigate: str | None = None,
) -> dict:
    """Finding単位の必須フィールド（id/category/severity/confidence/message/recommendation）を
    保証しつつ生成する。judgment/evidence/sourceは追跡可能性のための追加フィールド。
    """
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"不正なseverity: {severity}")
    if judgment is not None and judgment not in VALID_JUDGMENTS:
        raise ValueError(f"不正なjudgment: {judgment}")

    finding = {
        "id": id,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "message": message,
        "recommendation": recommendation,
    }
    if judgment is not None:
        finding["judgment"] = judgment
    if evidence is not None:
        finding["evidence"] = evidence
    if source is not None:
        finding["source"] = source
    if shortfall is not None:
        finding["shortfall"] = shortfall
    if investigate is not None:
        finding["investigate"] = investigate
    return finding


def sum_metrics(rows: list[dict]) -> dict:
    """rows（clicks/impressions/positionを持つ行のリスト）を単純合計する。
    positionはimpressions加重平均。空リストなら全て0/Noneを返す。
    """
    clicks = sum(r.get("clicks", 0) for r in rows)
    impressions = sum(r.get("impressions", 0) for r in rows)
    weighted_position = sum(r.get("position", 0.0) * r.get("impressions", 0) for r in rows)
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": (clicks / impressions) if impressions else 0.0,
        "position": (weighted_position / impressions) if impressions else None,
    }


def group_metrics(rows: list[dict], key_fn) -> dict:
    """rowsを key_fn(row) でグルーピングし、それぞれ sum_metrics する。
    key_fn が None を返した行は除外する。
    """
    groups: dict = {}
    for row in rows:
        key = key_fn(row)
        if key is None:
            continue
        groups.setdefault(key, []).append(row)
    return {key: sum_metrics(group_rows) for key, group_rows in groups.items()}


def top_n_by(grouped: dict, n: int = 5, sort_key: str = "clicks") -> list[tuple]:
    """group_metricsの出力から (key, metrics) を sort_key 降順で上位n件返す。"""
    return sorted(grouped.items(), key=lambda kv: kv[1].get(sort_key, 0) or 0, reverse=True)[:n]


def pct_change(cur: float, prv: float) -> float | None:
    if prv in (0, None) or cur is None:
        return None
    return (cur - prv) / prv


def position_delta(cur_position: float | None, prv_position: float | None) -> float | None:
    """順位の変化量。正の値=改善（数値が小さくなった）。position自体は小さいほど上位。"""
    if cur_position is None or prv_position is None:
        return None
    return prv_position - cur_position


def expected_ctr(position: float | None) -> float | None:
    if position is None:
        return None
    rounded = round(position)
    return EXPECTED_CTR_BY_POSITION.get(rounded)
