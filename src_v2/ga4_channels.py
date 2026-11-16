"""GA4 の流入元（sessionSourceMedium）を、レポートの区分に分類する（2026-11-23週〜）。

区分は上から順に判定し、1つのセッションは1つの区分にだけ入る（セッション数の合計がサイト全体と一致する）。
  生成AI → 広告 → 自然検索 → 直接 → 参照元 → その他・不明
規則は config_v2/pipeline.yaml の ga4.channels。
"""
from __future__ import annotations

import re

AI = "ai"
AD = "ad"
ORGANIC = "organic"
DIRECT = "direct"
REFERRAL = "referral"
OTHER = "other"

# 表示順
CHANNEL_KEYS = (AD, ORGANIC, DIRECT, REFERRAL, AI, OTHER)
CHANNEL_LABELS = {
    AD: "広告",
    ORGANIC: "自然検索",
    DIRECT: "直接",
    REFERRAL: "参照元（他サイトのリンク）",
    AI: "生成AI",
    OTHER: "その他・不明",
}
OTHER_AI_LABEL = "その他の生成AI"


def classify_ai_source(source: str, medium: str, ai_cfg: dict) -> str | None:
    """生成AIのサービス名。対象外は None。

    参照元が services[].sources のいずれかと全体一致すればそのサービス。
    一致しなくてもメディアが include_mediums（GA4 の ai-assistant 分類）なら「その他の生成AI」。
    """
    for service in ai_cfg.get("services", []):
        if any(re.fullmatch(p, source, flags=re.IGNORECASE) for p in service.get("sources", [])):
            return service["label"]
    if medium.lower() in {m.lower() for m in ai_cfg.get("include_mediums", [])}:
        return OTHER_AI_LABEL
    return None


def classify_channel(source_medium: str, channels_cfg: dict) -> tuple[str, str | None]:
    """「参照元 / メディア」→ (区分, 生成AIのサービス名 or None)。"""
    source, _, medium = source_medium.partition(" / ")
    source, medium = source.strip(), medium.strip()
    service = classify_ai_source(source, medium, channels_cfg.get("ai") or {})
    if service:
        return AI, service
    if re.fullmatch(channels_cfg["ad_medium_regex"], medium, flags=re.IGNORECASE):
        return AD, None
    if medium.lower() == "organic":
        return ORGANIC, None
    if source == "(direct)" and medium in ("(none)", "(not set)"):
        return DIRECT, None
    if medium.lower() == "referral":
        return REFERRAL, None
    return OTHER, None
