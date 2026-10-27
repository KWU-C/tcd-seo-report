"""正規化層。

URLの正規化は本モジュールでのみ行う（fetch層・validate層では行わない）。
AIO引用ドメインの正規化（alias解決・エンティティ分類）も、raw観測結果の
解釈にあたるためここで行う（raw層には解釈済みの値を置かない）。

入力: raw（GSC rows / AIO observation record）
出力: data-contract.md 4節に定義したprocessedテーブル（プレーンなlist[dict]）
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote

# ────────────────────────────────────────────────────────────────────
# URL正規化（このモジュールが唯一の実装場所）
# ────────────────────────────────────────────────────────────────────

_NOISE_QUERY_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "ref",
}
_EXCLUDE_PATH_MARKERS = ("/wp-admin", "/wp-login")
_EXCLUDE_QUERY_MARKERS = ("customize_changeset_uuid", "preview")


def normalize_url(raw_url: str) -> dict:
    """URLを正規化する。data-contract.md 3節の契約に従う。

    - http -> https
    - hostの大文字小文字統一
    - URLエンコード表現の統一（一度decodeしてから再構成）
    - 末尾の index.html を "/" に統一（ディレクトリ形式のみ。.htmlファイルURLはそのまま）
    - 不要クエリパラメータ（utm_*等）の除去
    - preview/customize/wp-admin系はis_excluded=Trueにする（正規化はするが監視対象外として扱う）

    canonical_url はHTTP応答の確認が必要なため、本関数では判定しない
    （未確認はnull。推測でcanonical_urlを埋めない）。
    ハイフン/アンダースコアの統一はしない（別URLとして扱う。要件通り）。
    """
    parts = urlsplit(raw_url.strip())
    scheme = "https"
    netloc = parts.netloc.lower()

    path = unquote(parts.path)
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]

    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _NOISE_QUERY_PARAMS
    ]

    is_excluded = False
    exclusion_reason = None
    if any(marker in path for marker in _EXCLUDE_PATH_MARKERS):
        is_excluded = True
        exclusion_reason = "wp-admin/wp-login系パス"
    elif any(k for k, _ in parse_qsl(parts.query, keep_blank_values=True) if any(m in k for m in _EXCLUDE_QUERY_MARKERS)):
        is_excluded = True
        exclusion_reason = "preview/customize系クエリパラメータ"

    normalized_url = urlunsplit((scheme, netloc, path, urlencode(query_pairs), ""))

    return {
        "normalized_url": normalized_url,
        "canonical_url": None,
        "is_excluded": is_excluded,
        "exclusion_reason": exclusion_reason,
    }


# ────────────────────────────────────────────────────────────────────
# クエリ正規化・重点クエリ照合（config_v2/queries.yaml の query_matching に対応）
# ────────────────────────────────────────────────────────────────────

def _strip_all_spaces(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


def normalize_query_text(raw_query: str) -> str:
    """半角/全角スペース・スペースなしのバリアントを比較できるよう正規化する。"""
    return _strip_all_spaces(raw_query)


def matches_all_tokens(gsc_query: str, tokens: list[str]) -> bool:
    """gsc_query が tokens をすべて（順序不問で）含むかを判定する。

    完全一致ではなくAND条件を正とする（2026-07-27の方針転換。queries.yaml
    markets.*.all を参照）。「東京 インナーブランディング会社」
    「インナーブランディング会社 東京」「東京のインナーブランディング会社」等、
    Google上で語順・表記が揺れる自然な検索をすべて拾うため。
    一部のtokenしか含まないクエリ（例:「インナーブランディング 支援」）は
    対象外になる。
    """
    normalized_gsc = normalize_query_text(gsc_query)
    return bool(tokens) and all(normalize_query_text(tok) in normalized_gsc for tok in tokens)


def matches_exact(gsc_query: str, candidates: list[str]) -> bool:
    """正規化後のgsc_queryがcandidatesのいずれかと完全一致するか。
    テーマ市場（「ブランディング」単体等）で、異なる検索意図を含む複合語を
    集計に混ぜないために使う（queries.yaml markets.*.exact）。"""
    normalized_gsc = normalize_query_text(gsc_query)
    return any(normalize_query_text(c) == normalized_gsc for c in candidates)


# ────────────────────────────────────────────────────────────────────
# GSC processedテーブル生成（data-contract.md 4.1）
# ────────────────────────────────────────────────────────────────────

def _row_values(row: dict, dimensions: list[str]) -> dict:
    keys = row.get("keys", [])
    return dict(zip(dimensions, keys))


def build_page_lookup(pages: list[dict]) -> dict[str, str]:
    """pages.yaml由来のURLを正規化した上で normalized_url -> page_id の辞書を作る。"""
    lookup: dict[str, str] = {}
    for page in pages:
        normalized = normalize_url(page["url"])["normalized_url"]
        lookup[normalized] = page["id"]
    return lookup


def build_query_daily(raw_date_query: dict, source_fetched_at: str) -> list[dict]:
    dimensions = raw_date_query["dimensions"]
    rows = raw_date_query["response"]["rows"]
    out = []
    for row in rows:
        values = _row_values(row, dimensions)
        out.append({
            "date": values["date"],
            "query": values["query"],
            "normalized_query": normalize_query_text(values["query"]),
            "clicks": int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
            "ctr": float(row.get("ctr", 0.0)),
            "position": float(row.get("position", 0.0)),
            "source_fetched_at": source_fetched_at,
        })
    return out


def build_page_daily(raw_date_page: dict, page_lookup: dict[str, str], source_fetched_at: str) -> list[dict]:
    dimensions = raw_date_page["dimensions"]
    rows = raw_date_page["response"]["rows"]
    out = []
    for row in rows:
        values = _row_values(row, dimensions)
        normalized = normalize_url(values["page"])["normalized_url"]
        out.append({
            "date": values["date"],
            "page_url": values["page"],
            "normalized_url": normalized,
            "page_id": page_lookup.get(normalized),
            "clicks": int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
            "ctr": float(row.get("ctr", 0.0)),
            "position": float(row.get("position", 0.0)),
            "source_fetched_at": source_fetched_at,
        })
    return out


def build_page_query_daily(raw_date_page_query: dict, page_lookup: dict[str, str], source_fetched_at: str) -> list[dict]:
    dimensions = raw_date_page_query["dimensions"]
    rows = raw_date_page_query["response"]["rows"]
    out = []
    for row in rows:
        values = _row_values(row, dimensions)
        normalized = normalize_url(values["page"])["normalized_url"]
        out.append({
            "date": values["date"],
            "page_url": values["page"],
            "normalized_url": normalized,
            "page_id": page_lookup.get(normalized),
            "query": values["query"],
            "clicks": int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
            "ctr": float(row.get("ctr", 0.0)),
            "position": float(row.get("position", 0.0)),
            "source_fetched_at": source_fetched_at,
        })
    return out


def build_gsc_processed_tables(raw_by_dimension: dict[str, dict], pages: list[dict]) -> dict[str, list[dict]]:
    page_lookup = build_page_lookup(pages)
    return {
        "gsc_query_daily": build_query_daily(
            raw_by_dimension["date_query"], raw_by_dimension["date_query"]["fetched_at"]
        ),
        "gsc_page_daily": build_page_daily(
            raw_by_dimension["date_page"], page_lookup, raw_by_dimension["date_page"]["fetched_at"]
        ),
        "gsc_page_query_daily": build_page_query_daily(
            raw_by_dimension["date_page_query"], page_lookup, raw_by_dimension["date_page_query"]["fetched_at"]
        ),
    }


# ────────────────────────────────────────────────────────────────────
# AIO processedテーブル生成（data-contract.md 4.2）
# ────────────────────────────────────────────────────────────────────

_TCD_PATTERNS = ["tcd.jp", "株式会社tcd", " tcd ", "（tcd）"]
_DOMAIN_RE = re.compile(r"https?://([^/]+)")


def _extract_root_domain(url: str) -> str:
    m = _DOMAIN_RE.search(url or "")
    host = (m.group(1) if m else "").lower()
    return host[4:] if host.startswith("www.") else host


def _build_alias_lookup(competitor_domains_cfg: dict) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in (competitor_domains_cfg or {}).get("aliases", {}).items():
        lookup[_strip_all_spaces(canonical.lower())] = canonical
        for alias in aliases:
            lookup[_strip_all_spaces(alias.lower())] = canonical
    return lookup


def _resolve_domain(raw_domain_or_name: str, alias_lookup: dict[str, str]) -> str:
    if not raw_domain_or_name:
        return ""
    key = _strip_all_spaces(raw_domain_or_name.lower())
    return alias_lookup.get(key, raw_domain_or_name)


_GOOGLE_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*google\.[a-z.]+$")
_DOMAIN_LIKE_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.IGNORECASE)


def _is_google_link(url: str) -> bool:
    """Google経由のリンク（google.com/searchviewer、google.co.jp/goto 等）か。遷移先はURLから分からない。"""
    return bool(_GOOGLE_HOST_RE.match(_extract_root_domain(url)))


def _source_domain(src_name: str, alias_lookup: dict[str, str]) -> str | None:
    """引用元URLから遷移先が分からない場合に、SerpAPIの source 欄からドメインを求める。

    source がドメイン表記ならそのドメイン、表示名が alias に登録済みならその正規ドメイン、
    source が「Google」ならGoogle自身の引用（競合集計からは除外）。それ以外は不明として None を返す
    （リダイレクト先や表示名からドメインを推測しない）。
    """
    name = (src_name or "").strip()
    if not name:
        return None
    if _DOMAIN_LIKE_RE.match(name):
        host = name.lower()
        return _resolve_domain(host[4:] if host.startswith("www.") else host, alias_lookup)
    resolved = _resolve_domain(name, alias_lookup)
    if resolved != name or name.lower() in _NON_COMPETITOR_SOURCES:
        return resolved
    return None


# citation_status: AIO生成時に引用元を取得できたかどうか（「引用元なし」と「引用元取得不可」を区別する）
CITATION_EXTRACTED = "extracted"      # 引用元を1件以上取得
CITATION_NONE = "none"                # AIO本文は取得できたが引用元は0件
CITATION_UNAVAILABLE = "unavailable"  # AIO本文・引用元とも取得できていない（page_tokenのみ返却、追加取得で
                                      # 「Google hasn't returned any results」等）。生成有無・TCD引用とも判定不可

# Google内部リンク（google.com/searchviewer等。url抽出時に除外され、source名「Google」だけが残る）。
# 競合ドメインの集計・表示には含めない（cited_domains自体は元データとして保持する）。
_NON_COMPETITOR_SOURCES = {"google", "google.com", "google.co.jp"}


def has_aio_content(ai_overview: dict | None) -> bool:
    """SerpAPIのai_overviewに本文・引用元が実際に入っているか。
    {"page_token": ..., "serpapi_link": ...} だけの応答は「AIOあり・内容は別リクエスト」で、中身は未取得。"""
    ao = ai_overview or {}
    return bool(ao.get("references") or ao.get("text_blocks") or ao.get("text"))


def citation_status(obs: dict | None) -> str | None:
    """aio_observationの引用元取得状況。AIO未生成・未取得の場合はNone。
    citation_status導入前のnormalized.jsonは、引用元が1件もなければ取得不可とみなす
    （当時の該当ケースはすべてpage_tokenのみの応答だったため）。"""
    if not obs or obs.get("acquisition_status") != "success" or not obs.get("aio_generated"):
        return None
    if obs.get("citation_status"):
        return obs["citation_status"]
    return CITATION_EXTRACTED if (obs.get("cited_domains") or obs.get("cited_urls")) else CITATION_UNAVAILABLE


def competitor_domains(obs: dict | None) -> list[str]:
    """AIO引用ドメインのうち競合集計・表示に使うもの（引用順・重複除去、TCDとGoogle内部リンクを除外）。"""
    return list(dict.fromkeys(
        d for d in ((obs or {}).get("cited_domains") or [])
        if d and d != "tcd.jp" and d.lower() not in _NON_COMPETITOR_SOURCES
    ))


# TCD推薦の判定（AIO本文の会社一覧にTCDが紹介されているか）は、日次AIO監視と同じ判定関数・設定を使う
# （日次と週次で判定基準がずれないようにするため。判定条件の正本は aio_daily_monitor.py と config/aio_queries.yml）。
AIO_RECOMMENDATION_CONFIG = "config/aio_queries.yml"
_recommendation_rules: dict | None = None


def _load_recommendation_rules() -> dict:
    global _recommendation_rules
    if _recommendation_rules is None:
        import aio_daily_monitor
        _recommendation_rules = aio_daily_monitor.build_judgement_rules(
            aio_daily_monitor.load_config(AIO_RECOMMENDATION_CONFIG))
    return _recommendation_rules


def assess_recommendation(query: str, ai_overview: dict | None, cited_urls: list[str], cited_domains: list[str],
                          rules: dict | None = None) -> dict:
    """AIO本文からTCD推薦を判定する。tcd_recommended は True（あり）/ False（なし）/ None（判定不可・対象外）。
    「なし」は会社一覧を最後まで取得でき、その中にTCDがない場合だけ。本文を取得できない・一覧が途中で
    切れている場合は判定不可（None）とし、「なし」と混同しない。"""
    import aio_daily_monitor
    rules = rules or _load_recommendation_rules()
    text = aio_daily_monitor.extract_aio_text(ai_overview) if ai_overview else ""
    result = aio_daily_monitor.evaluate_observation(
        query, "OK_WITH_AIO", True, text, cited_urls, cited_domains, [], rules)
    return {
        "tcd_recommended": result["tcd_recommended"],
        "recommendation_status": result["recommendation_status"],
        "recommendation_reason": result["recommendation_reason"],
    }


def build_aio_observation(record: dict, competitor_domains_cfg: dict | None = None,
                          recommendation_rules: dict | None = None) -> dict:
    """AIO raw record（fetch_aio.pyの出力）を data-contract.md 4.2 の
    aio_observation スキーマへ変換する。ドメイン抽出・alias正規化・TCD引用判定は
    ここで初めて行う（rawには含めない）。
    """
    status = record["acquisition_status"]
    alias_lookup = _build_alias_lookup(competitor_domains_cfg or {})

    aio_generated: bool | None
    tcd_cited: bool | None
    tcd_urls: list[str] = []
    cited_domains: list[str] = []
    cited_urls: list[str] = []
    unknown_sources: list[str] = []  # 引用元ドメインを特定できなかった引用（source欄の表記をそのまま保持）
    answer_text_hash: str | None = None
    cite_status: str | None = None
    content_error: str | None = None

    if status == "success":
        aio_generated = True
        raw_response = record.get("raw_response") or {}
        search_result = raw_response.get("search_result") or {}
        ai_overview_result = raw_response.get("ai_overview_result")
        # 本文・引用元が入っている方を使う（検索結果側がpage_tokenのみの場合は追加取得結果）
        candidates = [search_result.get("ai_overview"), (ai_overview_result or {}).get("ai_overview")]
        ai_overview = next((c for c in candidates if has_aio_content(c)), None) or {}

        text = ai_overview.get("text", "") or ""
        if text:
            answer_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # 中身を取得できていない場合、TCD引用の有無は判定できない（False＝引用なしと混同しない）
        tcd_cited = False if ai_overview else None
        for ref in ai_overview.get("references", []):
            if isinstance(ref, str):
                url, src_name = ref, ""
            else:
                url = ref.get("link", "") or ref.get("url", "")
                src = ref.get("source", {})
                src_name = src.get("name", "") if isinstance(src, dict) else str(src)
            # Google経由のリンクは遷移先が分からないため引用URLにせず、ドメインは source 欄から求める
            if _is_google_link(url):
                url = ""

            domain = _resolve_domain(_extract_root_domain(url), alias_lookup) if url else _source_domain(src_name, alias_lookup)
            if url:
                cited_urls.append(url)
            if domain:
                cited_domains.append(domain)
            else:
                unknown_sources.append(src_name)

            haystack = f"{url} {domain} {src_name}".lower()
            if any(p in haystack for p in _TCD_PATTERNS):
                tcd_cited = True
                if url:
                    tcd_urls.append(url)

        if not ai_overview:
            cite_status = CITATION_UNAVAILABLE
            followup = (ai_overview_result or {})
            content_error = followup.get("error") or (followup.get("ai_overview") or {}).get("error")
        else:
            cite_status = CITATION_EXTRACTED if (cited_domains or cited_urls or unknown_sources) else CITATION_NONE
        recommendation = assess_recommendation(
            record["query"], ai_overview, cited_urls, cited_domains, recommendation_rules)

    elif status == "not_generated":
        aio_generated = False
        tcd_cited = None
        recommendation = {"tcd_recommended": None, "recommendation_status": "no_aio", "recommendation_reason": None}
    else:
        aio_generated = None
        tcd_cited = None
        # 取得エラー・未実行。推薦を判定するクエリなら「判定不可」（「なし」とは扱わない）
        recommendation = assess_recommendation(record["query"], None, [], [], recommendation_rules)
        if recommendation["recommendation_status"] == "undeterminable":
            recommendation["recommendation_reason"] = "acquisition_failed"

    return {
        "observed_at": record["observed_at"],
        "query": record["query"],
        "acquisition_status": status,
        "aio_generated": aio_generated,
        "tcd_cited": tcd_cited,
        "tcd_urls": tcd_urls,
        "cited_domains": cited_domains,
        "cited_urls": cited_urls,
        "unknown_sources": unknown_sources,
        "citation_status": cite_status,
        **recommendation,
        "answer_text_hash": answer_text_hash,
        "error_message": record.get("error_message") or content_error,
    }


def build_aio_processed_table(records: list[dict], competitor_domains_cfg: dict | None = None) -> list[dict]:
    return [build_aio_observation(r, competitor_domains_cfg) for r in records]
