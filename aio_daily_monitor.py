#!/usr/bin/env python3
"""TCD AIO 日次モニター。

config/aio_queries.yml に登録したクエリについて、SerpAPI経由で
Google AI Overview の有無・TCD引用状況・引用元ドメインを毎日取得し、

  - data/aio/daily/YYYY-MM-DD.json  … 生データ（AIO全文含む）
  - data/aio/looker/aio_daily.csv         … Looker Studio用（クエリ粒度）
  - data/aio/looker/aio_domains_daily.csv … Looker Studio用（引用ドメイン粒度）

として保存する。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml

DEFAULT_CONFIG_PATH = "config/aio_queries.yml"
DEFAULT_DAILY_JSON_DIR = "data/aio/daily"
DEFAULT_LOOKER_DIR = "data/aio/looker"
AIO_DAILY_CSV_NAME = "aio_daily.csv"
AIO_DOMAINS_CSV_NAME = "aio_domains_daily.csv"
DEFAULT_LOG_PATH = "logs/aio_daily.log"

TCD_DOMAIN_DEFAULT = "tcd.jp"

# 新しい列は既存列の後ろに追加する（Looker Studio・generate_report.py の既存列を崩さない）
DAILY_CSV_COLUMNS = [
    "date", "query", "aio_exists", "tcd_cited", "tcd_mentioned",
    "aio_text_hash", "aio_text_changed", "observation_type", "previous_hash_exists", "status",
    "citation_status", "tcd_recommended", "recommendation_status", "recommendation_reason",
]

# citation_status: AIOの本文・引用元を取得できたか（週次 src_v2/normalize.py と同じ区分）
CITATION_EXTRACTED = "extracted"      # 引用元を1件以上取得
CITATION_NONE = "none"                # 本文は取得できたが引用元は0件
CITATION_UNAVAILABLE = "unavailable"  # AIOはあるが本文・引用元とも未取得（page_tokenのみ等）。TCD引用は判定不可

# recommendation_status: AIO本文の会社一覧でTCDが推薦されているか（引用の有無とは独立に判定する）
RECOMMENDED = "recommended"            # 本文にTCDが登場
NOT_RECOMMENDED = "not_recommended"    # 会社一覧は取得できたがTCDは登場しない
UNDETERMINABLE = "undeterminable"      # 会社一覧を取得できていない（推薦判定不可。「推薦なし」とは扱わない）
NOT_APPLICABLE = "not_applicable"      # 会社を推薦する検索意図のクエリではない
NO_AIO = "no_aio"                      # AIO自体が生成されていない

DEFAULT_RECOMMENDATION_QUERY_KEYWORDS = ["会社"]

# 「TCD」はWordPressテーマ「TCD」（株式会社デザインプラス）と同じ表記のため、
# 法人格つきの社名・ドメインは単独で判定し、「TCD」単独の表記は同じ行の文脈で判定する。
_TCD_STRONG_RE = re.compile(
    r"株式会社\s*TCD(?![A-Za-z0-9])|[（(]株[）)]\s*TCD(?![A-Za-z0-9])|(?<![A-Za-z0-9])TCD\s*株式会社"
    r"|(?<![A-Za-z0-9])TCD\s+Corporation|(?<![A-Za-z0-9-])tcd\.jp(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
_TCD_BARE_RE = re.compile(r"(?<![A-Za-z0-9])TCD(?![A-Za-z0-9])")
DEFAULT_TCD_CONTEXT_TERMS = ["ブランディング", "ブランド", "銀座", "芦屋", "50年", "1971"]
DEFAULT_TCD_EXCLUDE_TERMS = ["WordPress", "ワードプレス", "テーマ", "デザインプラス", "Design Plus", "DesignPlus", "tcd-theme"]

# 会社一覧が本文に含まれているかの判定に使う法人格表記
_LEGAL_FORM_RE = re.compile(r"株式会社|有限会社|合同会社|[（(]株[）)]")
_COMPANY_TOKEN_RE = re.compile(
    r"(?:株式会社|有限会社|合同会社|[（(]株[）)])\s*[^\s、。，,：:（）()「」・/／]+"
    r"|[^\s、。，,：:（）()「」・/／]+?(?:株式会社|有限会社|合同会社)"
)

# 会社一覧の完全性の判定に使う行パターン。
# 一覧の見出し・予告（この後に会社が並ぶはずの行）
_LIST_ANNOUNCE_RE = re.compile(
    r"以下の(?:通り|とおり|ような|会社|企業)|(?:ご)?紹介します|(?:会社|企業)(?:と|の)特徴"
    r"|代表的な.{0,20}(?:会社|企業)|おすすめ.{0,20}(?:会社|企業)(?:一覧)?[:：]?$"
)
# 一覧の見出しの後、ここまでに一覧項目がなければ「見出しだけ残って項目が欠けた」とみなす段落の区切り
_SECTION_BREAK_RE = re.compile(r"選び方|ポイント|費用|相場|よろしければ|教えて|よくある|注意点|依頼の流れ|類型|種類")
# 一覧の後に続く別の段落（追加質問・会社選び・費用相場など）。項目の説明文に出にくい語に限る。
# 一覧項目の後にこれがあれば、本文は一覧の途中で切れず先まで取得できているとみなす
_LIST_CLOSING_RE = re.compile(r"よろしければ|教えて|お聞かせ|お知らせください|選び|選定|費用相場|費用の目安|相場|よくある質問|まとめ")
_LIST_BULLET_RE = re.compile(r"^[\s・\-*●■◆▶︎>]*(?:\d+[.)．、）]\s*)?")
DEFAULT_MIN_LISTED_COMPANIES = 2

# recommendation_reason: 推薦判定の根拠（日次JSONに記録し、判定不可の内訳を確認できるようにする）
REASON_TCD_IN_BODY = "tcd_in_body"
REASON_LIST_OBSERVED = "company_list_observed"
REASON_NO_LIST = "no_company_list"
REASON_TOO_FEW_ITEMS = "too_few_list_items"
REASON_HEADING_WITHOUT_ITEMS = "list_heading_without_items"
REASON_LIST_NOT_CLOSED = "list_not_followed_by_other_section"
REASON_CONTENT_UNAVAILABLE = "aio_content_unavailable"
DOMAINS_CSV_COLUMNS = ["date", "query", "cited_domain", "cited_domain_count"]


def load_env(env_path: str = ".env") -> None:
    """.env を読み込み os.environ にセットする（python-dotenv 不要の簡易実装）。
    既に環境変数が設定されている場合は上書きしない。"""
    p = Path(env_path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _extract_domain(url: str) -> str:
    m = re.search(r'https?://([^/]+)', url)
    return m.group(1) if m else url


def _root_domain(hostname: str) -> str:
    """www. サブドメインをルートドメインへ正規化する。"""
    h = (hostname or "").lower().strip()
    return h[4:] if h.startswith("www.") else h


_DOMAIN_LIKE_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)


def _looks_like_domain(s: str) -> bool:
    """'www.example.com' のようなホスト名らしい文字列か判定する（会社名の誤正規化を防ぐ）。"""
    return bool(_DOMAIN_LIKE_RE.match((s or "").strip()))


def is_google_domain(domain: str) -> bool:
    """Google検索・Google自身のURL（support.google.comなど）かどうかを判定する。"""
    d = (domain or "").lower()
    return d == "google" or "google." in d


def _flatten_text_block(block: dict) -> list[str]:
    parts = []
    snippet = block.get("snippet")
    if snippet:
        parts.append(snippet)
    for item in block.get("list", []) or []:
        parts.extend(_flatten_text_block(item))
    for sub in block.get("text_blocks", []) or []:
        parts.extend(_flatten_text_block(sub))
    return parts


def extract_aio_text(ai_ov: dict) -> str:
    """ai_overview.text が空の場合、text_blocks（段落・リスト）から本文を再構成する。"""
    text = (ai_ov.get("text") or "").strip()
    if text:
        return text
    parts: list[str] = []
    for block in ai_ov.get("text_blocks", []) or []:
        parts.extend(_flatten_text_block(block))
    return "\n".join(parts).strip()


def _normalize_key(s: str) -> str:
    return re.sub(r'\s+', '', (s or '')).lower()


def build_domain_alias_lookup(domain_aliases: dict | None) -> dict[str, str]:
    """config の domain_aliases から 正規化キー→正規ドメイン の逆引き辞書を作る。"""
    lookup: dict[str, str] = {}
    for canonical, aliases in (domain_aliases or {}).items():
        lookup[_normalize_key(canonical)] = canonical
        for alias in aliases:
            lookup[_normalize_key(alias)] = canonical
    return lookup


def normalize_domain(raw: str, alias_lookup: dict[str, str]) -> str:
    """引用元表記（ドメイン／社名／英語表記）を正規化後のドメインに寄せる。"""
    if not raw:
        return raw
    return alias_lookup.get(_normalize_key(raw), raw)


def build_judgement_rules(config: dict) -> dict:
    """TCD社名判定・推薦判定の条件を config から組み立てる（未設定は既定値）。"""
    entity = config.get("tcd_entity") or {}
    recommendation = config.get("recommendation") or {}
    return {
        "tcd_domain": config.get("tcd_domain", TCD_DOMAIN_DEFAULT),
        "context_terms": entity.get("context_terms", DEFAULT_TCD_CONTEXT_TERMS),
        "exclude_terms": entity.get("exclude_terms", DEFAULT_TCD_EXCLUDE_TERMS),
        "query_keywords": recommendation.get("query_keywords", DEFAULT_RECOMMENDATION_QUERY_KEYWORDS),
        "known_company_names": recommendation.get("known_company_names", []),
        "min_listed_companies": recommendation.get("min_listed_companies", DEFAULT_MIN_LISTED_COMPANIES),
    }


def parse_references(ai_ov: dict, alias_lookup: dict[str, str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """ai_overview.references から (引用URL, 引用ドメイン, タイトル, 引用元表示名) を取り出す。

    SerpAPIの引用元URLは "link" キーに入る（"url" は旧仕様互換として参照するのみ）。
    Googleのドメインのリンク（google.com/searchviewer、google.co.jp/goto等）は遷移先が分からないため
    引用URLには含めず、ドメインは表示名から求める。
    """
    cited_urls: list[str] = []
    cited_domains: list[str] = []
    cited_titles: list[str] = []
    cited_sources: list[str] = []

    for ref in ai_ov.get("references", []) or []:
        if isinstance(ref, str):
            url, src_name, title = ref, "", ""
        else:
            url = ref.get("link") or ref.get("url") or ""
            src = ref.get("source", "")
            src_name = (src.get("name", "") if isinstance(src, dict) else str(src or ""))
            title = ref.get("title", "")
        # ドメイン（正規化キー）は必ずURLのホスト名から求め、
        # 表示名（会社名など）と混同しない。www.はルートドメインへ正規化する。
        url_domain = _root_domain(_extract_domain(url)) if url else ""
        if is_google_domain(url_domain):
            url, url_domain = "", ""
        fallback = _root_domain(src_name) if _looks_like_domain(src_name) else src_name
        domain = normalize_domain(url_domain, alias_lookup) if url_domain else normalize_domain(fallback, alias_lookup)
        if url:
            cited_urls.append(url)
        if domain:
            cited_domains.append(domain)
        if title:
            cited_titles.append(title)
        cited_sources.append(src_name)

    return cited_urls, cited_domains, cited_titles, cited_sources


def is_tcd_url(url: str, tcd_domain: str = TCD_DOMAIN_DEFAULT) -> bool:
    """URLのホスト名がTCDのドメイン（またはそのサブドメイン）か。部分一致では判定しない。"""
    host = _root_domain(_extract_domain(url)) if url else ""
    return host == tcd_domain or host.endswith("." + tcd_domain)


def is_tcd_source(name: str, tcd_domain: str = TCD_DOMAIN_DEFAULT) -> bool:
    """引用元の表示名・ドメインがTCDか（「株式会社TCD」等の法人名表記、またはTCDのドメイン）。"""
    n = (name or "").strip()
    if not n:
        return False
    if _looks_like_domain(n):
        host = _root_domain(n)
        return host == tcd_domain or host.endswith("." + tcd_domain)
    return bool(_TCD_STRONG_RE.search(n))


def find_tcd_mentions(text: str, rules: dict) -> list[str]:
    """AIO本文のうち、株式会社TCDへの言及と判定した行を返す。

    「株式会社TCD」「tcd.jp」等は単独で言及とみなす。「TCD」単独の表記は、同じ行に
    ブランディング会社としての文脈語があり、WordPressテーマ等の除外語がない場合のみ言及とみなす。
    """
    context_terms = [t.lower() for t in rules.get("context_terms", DEFAULT_TCD_CONTEXT_TERMS)]
    exclude_terms = [t.lower() for t in rules.get("exclude_terms", DEFAULT_TCD_EXCLUDE_TERMS)]
    hits: list[str] = []
    for line in (text or "").splitlines():
        if _TCD_STRONG_RE.search(line):
            hits.append(line.strip())
            continue
        if not _TCD_BARE_RE.search(line):
            continue
        low = line.lower()
        if any(t in low for t in exclude_terms):
            continue
        if any(t in low for t in context_terms):
            hits.append(line.strip())
    return hits


def find_listed_companies(text: str, known_company_names: list[str]) -> list[str]:
    """AIO本文に含まれる会社名（法人格つきの表記、または既知の会社名）を出現順・重複なしで返す。"""
    text = text or ""
    found = [m.group(0) for m in _COMPANY_TOKEN_RE.finditer(text)]
    found += [name for name in known_company_names if name and name in text]
    return list(dict.fromkeys(found))


_ITEM_LABEL_RE = re.compile(r"^[^：:。]{1,20}[：:]\s*")  # 「採用・企業ブランディング：株式会社○○は…」の分類ラベル
_ITEM_ATTRIBUTE_RE = re.compile(r"^(?:特徴|強み|得意分野|得意|実績|所在地|サービス)\s*[：:]")


_ITEM_NAME_END_RE = re.compile(r"$|[\s：:（(,，]")


def _company_at_start(body: str, known_company_names: list[str]) -> str | None:
    """行頭の会社名を返す。直後が区切り（「：」「（」・空白・行末等）の場合に限る。
    「株式会社○○などの専門会社をはじめ…」「実績が豊富な株式会社○○や…」のような文章の行は一覧項目にしない。"""
    candidates = []
    m = _COMPANY_TOKEN_RE.match(body)
    if m:
        candidates.append(m.group(0))
    candidates += [name for name in known_company_names if name and body.startswith(name)]
    m = _TCD_STRONG_RE.match(body)
    if m:
        candidates.append(m.group(0))
    for name in candidates:
        if _ITEM_NAME_END_RE.match(body, len(name)):
            return name
    return None


def _list_item_company(line: str, known_company_names: list[str], next_line: str = "") -> str | None:
    """会社一覧の1項目の行なら、その会社名を返す。

    次のいずれかを一覧項目とみなす。
      - 行頭が会社名（法人格つき・既知の会社名・TCD）で始まる
      - 「分類ラベル：会社名…」の形で、ラベルの直後が会社名
      - 句点のない短い行で、次の行が「特徴:」「強み:」等の属性行（法人格のない社名だけの見出し）
    """
    body = _LIST_BULLET_RE.sub("", line, count=1).strip()
    if not body:
        return None
    company = _company_at_start(body, known_company_names)
    if company:
        return company
    label = _ITEM_LABEL_RE.match(body)
    if label and not _ITEM_ATTRIBUTE_RE.match(body):  # 「実績：株式会社○○」は取引先であって一覧項目ではない
        company = _company_at_start(body[label.end():], known_company_names)
        if company:
            return company
    if len(body) <= 40 and "。" not in body and not _ITEM_ATTRIBUTE_RE.match(body) \
            and _ITEM_ATTRIBUTE_RE.match(_LIST_BULLET_RE.sub("", next_line, count=1).strip()):
        return body
    return None


def _is_list_closing(line: str) -> bool:
    return bool(_LIST_CLOSING_RE.search(line)) or (len(line.strip()) <= 25 and "ポイント" in line)


def assess_company_list(text: str, known_company_names: list[str], min_items: int = DEFAULT_MIN_LISTED_COMPANIES) -> tuple[bool, str, list[str]]:
    """AIO本文の会社一覧を、TCDが「載っていない」と言える程度に取得できているか判定する。

    会社名が1社出てくるだけでは、一覧全体を取得できた証拠にならない（冒頭の要約文だけが取れて
    一覧部分が欠けることがある）。次をすべて満たす場合だけ、一覧を取得できたとみなす。
      1. 行頭が会社名で始まる一覧項目が min_items 社以上ある
      2. 一覧の見出し・予告の行の後に、次の段落までに一覧項目が1つ以上ある（見出しだけ残って項目が欠けていない）
      3. 一覧項目の後に別の段落（会社選び・追加質問など）が続く（一覧の途中で本文が切れていない）。
         別段落の後に引用元カード由来の会社名が並ぶことがあるため、最初の一覧項目より後を見る
    返り値: (取得できたか, 根拠, 一覧項目の会社名)
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    item_flags = [
        _list_item_company(ln, known_company_names, lines[i + 1] if i + 1 < len(lines) else "")
        for i, ln in enumerate(lines)
    ]
    items = list(dict.fromkeys(c for c in item_flags if c))

    for i, line in enumerate(lines):
        # 社名を含む行は要約文（「代表的な会社には株式会社○○や…」）であり、一覧の見出しではない
        if item_flags[i] or not _LIST_ANNOUNCE_RE.search(line) or find_listed_companies(line, known_company_names):
            continue
        has_item = False
        for j in range(i + 1, len(lines)):
            if item_flags[j]:
                has_item = True
                break
            if _SECTION_BREAK_RE.search(lines[j]):
                break
        if not has_item:
            return False, REASON_HEADING_WITHOUT_ITEMS, items

    if not items:
        return False, REASON_NO_LIST, items
    if len(items) < min_items:
        return False, REASON_TOO_FEW_ITEMS, items
    first_item = next(i for i, c in enumerate(item_flags) if c)
    if not any(_is_list_closing(ln) for ln, c in zip(lines[first_item + 1:], item_flags[first_item + 1:]) if not c):
        return False, REASON_LIST_NOT_CLOSED, items
    return True, REASON_LIST_OBSERVED, items


def is_recommendation_query(query: str, rules: dict) -> bool:
    """会社を推薦する検索意図のクエリか（既定は「会社」を含むクエリ）。"""
    return any(k in (query or "") for k in rules.get("query_keywords", DEFAULT_RECOMMENDATION_QUERY_KEYWORDS))


def evaluate_observation(
    query: str,
    status: str,
    aio_exists: bool | None,
    aio_text: str | None,
    cited_urls: list[str],
    cited_domains: list[str],
    cited_sources: list[str],
    rules: dict,
) -> dict:
    """AIO生成・TCD引用・TCD推薦をそれぞれ独立に判定する。

    tcd_cited / tcd_recommended は True / False / None（判定不可）の3値。
    AIOはあっても本文・引用元が取得できていない場合や、本文に会社一覧がない場合は
    False（なし）と混同せず None とする。
    """
    tcd_domain = rules.get("tcd_domain", TCD_DOMAIN_DEFAULT)
    if status not in ("OK_WITH_AIO", "OK_NO_AIO"):
        return {
            "citation_status": None, "tcd_cited": None, "tcd_recommended": None,
            "recommendation_status": None, "recommendation_reason": None, "tcd_mentioned": False,
            "tcd_mention_evidence": [], "listed_companies": [],
        }
    if not aio_exists:
        return {
            "citation_status": None, "tcd_cited": False, "tcd_recommended": False,
            "recommendation_status": NO_AIO, "recommendation_reason": None, "tcd_mentioned": False,
            "tcd_mention_evidence": [], "listed_companies": [],
        }

    text = aio_text or ""
    if not (text.strip() or cited_domains or cited_urls):
        citation_status = CITATION_UNAVAILABLE
        tcd_cited = None
    else:
        citation_status = CITATION_EXTRACTED if (cited_domains or cited_urls) else CITATION_NONE
        tcd_cited = any(is_tcd_url(u, tcd_domain) for u in cited_urls) or \
            any(is_tcd_source(s, tcd_domain) for s in list(cited_sources) + list(cited_domains))

    evidence = find_tcd_mentions(text, rules)
    known = rules.get("known_company_names", [])
    companies = find_listed_companies(text, known)
    reason = None
    if not is_recommendation_query(query, rules):
        recommendation_status, tcd_recommended = NOT_APPLICABLE, None
    elif evidence:
        # TCDの登場は一覧の一部しか取れていなくても確認できる
        recommendation_status, tcd_recommended, reason = RECOMMENDED, True, REASON_TCD_IN_BODY
    elif citation_status == CITATION_UNAVAILABLE:
        recommendation_status, tcd_recommended, reason = UNDETERMINABLE, None, REASON_CONTENT_UNAVAILABLE
    else:
        # 「載っていない」と言えるのは、会社一覧を最後まで取得できたと判断できる場合だけ
        observed, reason, _ = assess_company_list(text, known, rules.get("min_listed_companies", DEFAULT_MIN_LISTED_COMPANIES))
        recommendation_status, tcd_recommended = (NOT_RECOMMENDED, False) if observed else (UNDETERMINABLE, None)

    return {
        "citation_status": citation_status,
        "tcd_cited": tcd_cited,
        "tcd_recommended": tcd_recommended,
        "recommendation_status": recommendation_status,
        "recommendation_reason": reason,
        "tcd_mentioned": bool(tcd_cited) or bool(evidence),
        "tcd_mention_evidence": evidence,
        "listed_companies": companies,
    }


def competitor_domains(cited_domains: list[str], tcd_domain: str = TCD_DOMAIN_DEFAULT) -> list[str]:
    return [d for d in cited_domains if not is_tcd_source(d, tcd_domain) and not is_google_domain(d)]


def _empty_result(query: str, observed_at: str, status: str, error: str | None) -> dict:
    return {
        "observed_at": observed_at,
        "query": query,
        "status": status,
        "aio_exists": None,
        "citation_status": None,
        "tcd_cited": None,
        "tcd_recommended": None,
        "recommendation_status": None,
        "recommendation_reason": None,
        "tcd_mentioned": False,
        "tcd_mention_evidence": [],
        "listed_companies": [],
        "aio_text": None,
        "cited_urls": [],
        "cited_domains": [],
        "cited_titles": [],
        "cited_sources": [],
        "competitor_domains": [],
        "error": error,
    }


def fetch_aio_for_query(
    query: str,
    observed_at: str,
    serpapi_cfg: dict,
    api_key: str,
    alias_lookup: dict[str, str],
    rules: dict,
) -> dict:
    """SerpAPI で1クエリ分の Google AI Overview を観測する。"""
    from serpapi import GoogleSearch

    result = _empty_result(query, observed_at, "OK_NO_AIO", None)
    result["aio_exists"] = False

    try:
        params = {
            "engine": serpapi_cfg.get("engine", "google"),
            "q": query,
            "google_domain": serpapi_cfg.get("google_domain", "google.co.jp"),
            "hl": serpapi_cfg.get("hl", "ja"),
            "gl": serpapi_cfg.get("gl", "jp"),
            "location": serpapi_cfg.get("location", "Japan"),
            "api_key": api_key,
        }
        res = GoogleSearch(params).get_dict()
        ai_ov = res.get("ai_overview")

        # フォールバック: page_token がある場合は google_ai_overview エンジンで再取得
        if not ai_ov and res.get("ai_overview_page_token"):
            aio_res = GoogleSearch({
                "engine": "google_ai_overview",
                "page_token": res["ai_overview_page_token"],
                "api_key": api_key,
            }).get_dict()
            ai_ov = aio_res.get("ai_overview")

        if ai_ov:
            result["aio_exists"] = True
            result["status"] = "OK_WITH_AIO"
            # page_tokenのみ（本文が別リクエスト扱い）の応答は本文・引用元が空のまま残り、
            # evaluate_observation で「判定不可」になる。追加取得はAPI消費が増えるため行わない。
            result["aio_text"] = extract_aio_text(ai_ov)
            cited_urls, cited_domains, cited_titles, cited_sources = parse_references(ai_ov, alias_lookup)
            result["cited_urls"] = cited_urls
            result["cited_domains"] = cited_domains
            result["cited_titles"] = cited_titles
            result["cited_sources"] = cited_sources
            result["competitor_domains"] = competitor_domains(cited_domains, rules.get("tcd_domain", TCD_DOMAIN_DEFAULT))

    except Exception as e:
        result["status"] = "FETCH_ERROR"
        result["error"] = str(e)

    result.update(evaluate_observation(
        query, result["status"], result["aio_exists"], result["aio_text"],
        result["cited_urls"], result["cited_domains"], result["cited_sources"], rules,
    ))
    return result


def save_daily_json(results: list[dict], daily_json_dir: Path, observed_at: str) -> Path:
    daily_json_dir.mkdir(parents=True, exist_ok=True)
    path = daily_json_dir / f"{observed_at}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def get_previous_hashes(csv_path: Path, before_date: str) -> dict[str, str]:
    """aio_daily.csv から、指定日より前の各クエリの最新 aio_text_hash を取得する。"""
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, dtype=str)
    if df.empty:
        return {}
    df = df[df["date"] < before_date]
    if df.empty:
        return {}
    latest = df.sort_values("date").groupby("query").tail(1)
    return dict(zip(latest["query"], latest["aio_text_hash"].fillna("")))


def build_daily_rows(results: list[dict], previous_hashes: dict[str, str]) -> list[dict]:
    """クエリ粒度の Looker Studio 用 CSV 行を組み立てる。

    初回観測（そのクエリについて過去のハッシュが存在しない）は「本文変更」に
    カウントせず、observation_type="baseline" として扱う。
    """
    rows = []
    for r in results:
        aio_text = r.get("aio_text") or ""
        text_hash = hashlib.sha256(aio_text.encode("utf-8")).hexdigest()[:16] if aio_text else ""
        previous_hash_exists = r["query"] in previous_hashes
        prev_hash = previous_hashes.get(r["query"], "")

        if not previous_hash_exists:
            observation_type = "baseline"
            aio_text_changed = False
        elif text_hash == prev_hash:
            observation_type = "unchanged"
            aio_text_changed = False
        else:
            observation_type = "changed"
            aio_text_changed = True

        rows.append({
            "date": r["observed_at"],
            "query": r["query"],
            "aio_exists": r["aio_exists"],
            "tcd_cited": r["tcd_cited"],
            "tcd_mentioned": r["tcd_mentioned"],
            "aio_text_hash": text_hash,
            "aio_text_changed": aio_text_changed,
            "observation_type": observation_type,
            "previous_hash_exists": previous_hash_exists,
            "status": r["status"],
            "citation_status": r.get("citation_status"),
            "tcd_recommended": r.get("tcd_recommended"),
            "recommendation_status": r.get("recommendation_status"),
            "recommendation_reason": r.get("recommendation_reason"),
        })
    return rows


def build_domain_rows(results: list[dict]) -> list[dict]:
    """引用ドメイン粒度（1行1ドメイン）の Looker Studio 用 CSV 行を組み立てる。
    Google検索・Google自身のドメインは競合ドメインではないため除外する。"""
    rows = []
    for r in results:
        counts: dict[str, int] = {}
        for d in r.get("cited_domains", []):
            if is_google_domain(d):
                continue
            counts[d] = counts.get(d, 0) + 1
        for domain, count in counts.items():
            rows.append({
                "date": r["observed_at"],
                "query": r["query"],
                "cited_domain": domain,
                "cited_domain_count": count,
            })
    return rows


def write_csv_rows(
    rows: list[dict],
    csv_path: Path,
    key_cols: list[str],
    column_order: list[str],
    replace_groups: tuple[list[str], set[tuple]] | None = None,
) -> None:
    """行データを CSV に upsert する（同一キーの既存行は置き換え）。

    replace_groups=(列, キー集合) を渡すと、その列の組がキー集合に含まれる既存行を
    すべて削除してから追加する（同日再実行で引用ドメインが変わった場合に古い行を残さない）。

    将来 Google Sheets 連携に切り替える場合は、この関数と同じ
    (rows, key_cols, column_order) インターフェースで
    スプレッドシート書き込み版を用意すればよい。
    """
    new_df = pd.DataFrame(rows, columns=column_order)
    if csv_path.exists():
        old_df = pd.read_csv(csv_path, dtype=str)
        for col in column_order:
            if col not in old_df.columns:
                old_df[col] = None  # 列追加前に書き出された行は欠損値として扱う
        old_keys = old_df[key_cols].astype(str).apply(tuple, axis=1)
        new_keys = new_df[key_cols].astype(str).apply(tuple, axis=1)
        drop = old_keys.isin(set(new_keys))
        if replace_groups and not old_df.empty:
            group_cols, group_keys = replace_groups
            drop |= old_df[group_cols].astype(str).apply(tuple, axis=1).isin(group_keys)
        old_df = old_df[~drop]
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        combined = new_df
    combined = combined.sort_values(key_cols).reset_index(drop=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(csv_path, index=False)


def update_looker_csvs(results: list[dict], looker_dir: Path, observed_at: str) -> tuple[Path, Path]:
    aio_daily_csv = looker_dir / AIO_DAILY_CSV_NAME
    aio_domains_csv = looker_dir / AIO_DOMAINS_CSV_NAME

    previous_hashes = get_previous_hashes(aio_daily_csv, observed_at)
    daily_rows = build_daily_rows(results, previous_hashes)
    domain_rows = build_domain_rows(results)

    write_csv_rows(daily_rows, aio_daily_csv, key_cols=["date", "query"], column_order=DAILY_CSV_COLUMNS)
    observed_groups = {(str(r["observed_at"]), str(r["query"])) for r in results}
    write_csv_rows(domain_rows, aio_domains_csv, key_cols=["date", "query", "cited_domain"], column_order=DOMAINS_CSV_COLUMNS,
                   replace_groups=(["date", "query"], observed_groups))

    return aio_daily_csv, aio_domains_csv


def build_run_summary(results: list[dict], run_at: datetime) -> str:
    n_total = len(results)
    n_aio = sum(1 for r in results if r.get("aio_exists"))
    n_cited = sum(1 for r in results if r.get("tcd_cited"))
    n_cite_unknown = sum(1 for r in results if r.get("citation_status") == CITATION_UNAVAILABLE)
    n_rec_target = sum(1 for r in results if r.get("recommendation_status") not in (None, NOT_APPLICABLE, NO_AIO))
    n_recommended = sum(1 for r in results if r.get("tcd_recommended"))
    n_rec_unknown = sum(1 for r in results if r.get("recommendation_status") == UNDETERMINABLE)
    n_error = sum(1 for r in results if r.get("status") == "FETCH_ERROR")

    def pct(n: int, d: int) -> str:
        return f"{n / d:.0%}" if d else "-"

    lines = [
        run_at.strftime("%Y-%m-%d %H:%M"),
        f"{n_total} queries checked",
        f"AIO: {n_aio}",
        f"TCD cited: {n_cited} (undeterminable: {n_cite_unknown}/{n_aio} = {pct(n_cite_unknown, n_aio)})",
        f"TCD recommended: {n_recommended} (undeterminable: {n_rec_unknown}/{n_rec_target} = {pct(n_rec_unknown, n_rec_target)})",
    ]
    if n_error:
        lines.append(f"Errors: {n_error}")
    lines.append("CSV updated.")
    return "\n".join(lines)


def append_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(text + "\n\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="TCD AIO 日次モニター")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="クエリ設定ファイル")
    parser.add_argument("--env-file", default=".env", help=".env ファイルパス")
    parser.add_argument("--daily-json-dir", default=DEFAULT_DAILY_JSON_DIR)
    parser.add_argument("--looker-dir", default=DEFAULT_LOOKER_DIR)
    parser.add_argument("--date", default=None, help="観測日を上書き（テスト用、YYYY-MM-DD）")
    parser.add_argument("--log-file", default=DEFAULT_LOG_PATH, help="実行サマリーの追記先ログファイル")
    args = parser.parse_args()

    load_env(args.env_file)
    config = load_config(args.config)

    queries = config.get("queries", [])
    if not queries:
        print(f"{args.config} にクエリが登録されていません。", file=sys.stderr)
        sys.exit(1)

    serpapi_cfg = config.get("serpapi", {})
    rules = build_judgement_rules(config)
    alias_lookup = build_domain_alias_lookup(config.get("domain_aliases"))
    api_key = os.environ.get("SERPAPI_API_KEY")

    observed_at = args.date or date.today().strftime("%Y-%m-%d")

    if not api_key:
        print("SERPAPI_API_KEY が未設定です。.env を確認してください。", file=sys.stderr)
        results = [_empty_result(q, observed_at, "API_KEY_MISSING", "SERPAPI_API_KEY 未設定") for q in queries]
    else:
        results = []
        for query in queries:
            print(f"取得中: 「{query}」...")
            results.append(fetch_aio_for_query(query, observed_at, serpapi_cfg, api_key, alias_lookup, rules))

    json_path = save_daily_json(results, Path(args.daily_json_dir), observed_at)
    print(f"日次JSON保存: {json_path}")

    aio_daily_csv, aio_domains_csv = update_looker_csvs(results, Path(args.looker_dir), observed_at)
    print(f"Looker CSV更新: {aio_daily_csv}")
    print(f"Looker CSV更新: {aio_domains_csv}")

    summary = build_run_summary(results, datetime.now())
    print(summary)
    append_log(Path(args.log_file), summary)


if __name__ == "__main__":
    main()
