"""信頼度（Confidence）判定。

thresholds.yaml の confidence.factors に列挙された判断材料のうち、
評価対象のFindingに適用できるものだけを見て判定する
（存在しない材料は「対象外」として無視し、Not assessableへ寄せない）。

判断材料（該当するもののみ評価）:
  - データ量（low_impressions/low_clicksを下回っていないか）
  - 複数週の継続性（minimum_weeks_for_trend以上のデータがあるか）
  - SEOとAIOの一致
  - ページ単位とクエリ単位の一致

すべての材料がNoneで評価不能な場合は Not assessable を返す。
"""
from __future__ import annotations

LEVEL_HIGH = "High"
LEVEL_MEDIUM = "Medium"
LEVEL_LOW = "Low"
LEVEL_NOT_ASSESSABLE = "Not assessable"


def assess_confidence(evidence: dict, thresholds: dict) -> dict:
    """evidence:
      impressions: int | None
      clicks: int | None
      weeks_available: int | None       # 何週分のデータで判断しているか
      seo_aio_agree: bool | None        # SEOとAIOが同じ方向を示しているか（該当しないならNone）
      page_query_agree: bool | None     # ページ単位とクエリ単位の傾向が一致するか（該当しないならNone）

    戻り値: {"level": str, "reasons": list[str]}
    """
    low_data_cfg = thresholds["low_data"]
    low_impressions = low_data_cfg["low_impressions"]
    low_clicks = low_data_cfg["low_clicks"]
    min_weeks = low_data_cfg["minimum_weeks_for_trend"]

    reasons: list[str] = []
    factors: list[bool] = []

    impressions = evidence.get("impressions")
    clicks = evidence.get("clicks")
    if impressions is None and clicks is None:
        return {"level": LEVEL_NOT_ASSESSABLE, "reasons": ["Impression/Clickデータがない"]}

    data_ok = (impressions or 0) >= low_impressions and (clicks or 0) >= low_clicks
    factors.append(data_ok)
    if not data_ok:
        reasons.append(f"データ量が少ない（impressions={impressions}, clicks={clicks}）")

    weeks_available = evidence.get("weeks_available")
    if weeks_available is not None:
        trend_ok = weeks_available >= min_weeks
        factors.append(trend_ok)
        if not trend_ok:
            reasons.append(f"継続週数が{min_weeks}週未満（{weeks_available}週分のデータ）")

    seo_aio_agree = evidence.get("seo_aio_agree")
    if seo_aio_agree is not None:
        factors.append(seo_aio_agree)
        if not seo_aio_agree:
            reasons.append("SEOとAIOで示す方向が一致しない")

    page_query_agree = evidence.get("page_query_agree")
    if page_query_agree is not None:
        factors.append(page_query_agree)
        if not page_query_agree:
            reasons.append("ページ単位とクエリ単位で傾向が一致しない")

    if not factors:
        return {"level": LEVEL_NOT_ASSESSABLE, "reasons": ["判断材料が不足している"]}

    ratio = sum(1 for f in factors if f) / len(factors)
    if ratio >= 0.75:
        level = LEVEL_HIGH
    elif ratio >= 0.5:
        level = LEVEL_MEDIUM
    else:
        level = LEVEL_LOW

    return {"level": level, "reasons": reasons}
