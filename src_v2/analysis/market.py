"""重点市場・競合・AIO分析。

config_v2/queries.yaml の markets について、SEO状態・AIO状態・
AIO競合引用の変化をFindingとして生成する。

【2026-09-30 2階層化】市場は tier で扱いを分ける。
  deal（重点商談市場）/ theme（テーマ市場）… 検索Vol.を考慮して判定する。
    個別ページ用の「Imp 50未満は判断保留」は一律適用せず、市場ごとの最低Imp
    （min(low_impressions, 検索Vol.÷weeks_per_month×min_impressions_ratio)）以上の
    場合のみ順位の上昇・下降を判定する。Impが0の場合は「表示なし」（TCD側の課題）。
  reference（参考クエリ）… 検索Vol.が小さいため判定しない（数値の参考表示のみ）。
テーマ市場は exact（正規化後の完全一致）で集計し、異なる検索意図の複合語を混ぜない。

【2026-07-27 方針転換】完全一致（exact match）による集計は廃止した。
「東京 インナーブランディング会社」「インナーブランディング会社 東京」等の
語順違いをすべて別クエリとして扱うと実際の検索需要を取りこぼすため、
markets.*.all に列挙したトークンをすべて（順序不問で）含むクエリを対象とする
AND条件マッチング（normalize.matches_all_tokens）を唯一の正とする。
Looker Studio側の集計（RE2正規表現ベース）は比較参照専用とし、本モジュールの
出力を正本として扱う。

一致した実クエリの内訳は evidence.matched_query_variants に必ず保持し、
「集計結果がどの生クエリ由来か分からない」状態を作らない（根拠追跡性の担保）。
"""
from __future__ import annotations

from src_v2 import normalize
from src_v2.analysis.common import (
    INVESTIGATE_SERP,
    SHORTFALL_COMPETITOR_SERP,
    expected_ctr,
    group_metrics,
    make_finding,
    position_delta,
    sum_metrics,
    top_n_by,
)
from src_v2.analysis.confidence import assess_confidence

_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def _escalate(current: str, at_least: str) -> str:
    return current if _SEVERITY_ORDER[current] >= _SEVERITY_ORDER[at_least] else at_least


def _query_matcher(market_cfg: dict):
    """テーマ市場は exact（完全一致）、それ以外は all（トークンAND一致）。"""
    if market_cfg.get("exact"):
        return lambda q: normalize.matches_exact(q, market_cfg["exact"])
    return lambda q: normalize.matches_all_tokens(q, market_cfg["all"])


def _matched_rows(rows: list[dict], matcher) -> list[dict]:
    return [r for r in rows if matcher(r["query"])]


def market_min_impressions(market_cfg: dict, thresholds: dict) -> int:
    """順位の上昇・下降を判定するのに必要な市場ごとの最低Imp。
    検索Vol.300の市場に一律50impを要求しないための実務的な閾値（thresholds.yaml market）。"""
    low_impressions = thresholds["low_data"]["low_impressions"]
    volume = market_cfg.get("search_volume_ref")
    if not volume:
        return low_impressions
    market_cfg_t = thresholds["market"]
    weekly = volume / market_cfg_t["weeks_per_month"] * market_cfg_t["min_impressions_ratio"]
    return min(low_impressions, round(weekly))


def _matched_query_variants(rows: list[dict]) -> list[dict]:
    """AND条件に一致した生クエリの内訳（監査用）。どのクエリが集計に
    寄与したかを常に追跡できるようにする。"""
    grouped = group_metrics(rows, key_fn=lambda r: r["query"])
    return [
        {"query": query, **metrics}
        for query, metrics in sorted(grouped.items(), key=lambda kv: -kv[1]["impressions"])
    ]


def _top_url_for_market(page_query_rows: list[dict], matcher) -> dict | None:
    matched = _matched_rows(page_query_rows, matcher)
    if not matched:
        return None
    grouped = group_metrics(matched, key_fn=lambda r: r.get("page_id") or r["normalized_url"])
    ranked = top_n_by(grouped, n=1, sort_key="clicks")
    if not ranked:
        return None
    key, metrics = ranked[0]
    return {"page_id_or_url": key, **metrics}


def _judge_seo(current: dict, prv: dict, thresholds: dict, min_impressions: int) -> tuple[str, str, str, str, str | None]:
    """(severity, judgment, message, recommendation, shortfall) を返す。
    min_impressions: 市場ごとの最低Imp（market_min_impressions）。これ未満では順位の上昇・下降を判定しない。
    Impが少ないこと自体は課題として扱わない（検索Vol.が小さい市場ではImpも小さいため）。"""
    meaningful = thresholds["low_data"]["position_change_meaningful"]
    flat = thresholds["display"]["position_delta_flat"]

    position = current["position"]
    if not current["impressions"] or position is None:
        # 検索需要がある市場（deal/theme）でTCDが表示されていない＝TCD側の課題。
        # 「不足：インデックス確認」とはしない（ページ自体はインデックスされていても、
        # そのクエリで上位に出ていないだけのことが多い）。表示は「表示なし｜要調査：SERP確認」
        # GSC上に表示データがない（0imp）ことと、インデックス未登録は別の事象。混同しない表記にする
        return ("warning", "表示なし", "GSCで表示データなし（該当クエリの表示回数0。インデックス未登録を意味しない）",
                "実際の検索結果（上位ページ・検索意図）を確認し、TCDの対象ページとの差を把握する", None)

    # 順位の上昇・下降はExecutive Summaryと同じ閾値（display.position_delta_flat）で判定し、
    # カードとサマリーで「改善」「横ばい」の表記が食い違わないようにする。
    # 下落のseverity=warningは、Confidence判定用のposition_change_meaningful以上の下落に限る。
    delta = position_delta(position, prv.get("position"))
    moved = delta is not None and abs(delta) >= flat and current["impressions"] >= min_impressions
    move_str = f"{prv.get('position'):.1f}→{position:.1f}位" if delta is not None else f"{position:.1f}位"

    if position <= 5:
        exp_ctr = expected_ctr(position)
        if exp_ctr is not None and current["impressions"] >= min_impressions and current["ctr"] < exp_ctr * 0.7:
            return ("info", "再調査", f"上位表示（{position:.1f}位）だがCTRが期待値（目安{exp_ctr:.1%}）より低い",
                    "表示URLを確認／タイトル・descriptionを競合と比較", SHORTFALL_COMPETITOR_SERP)
        if moved and delta > 0:
            return ("info", "継続観測", f"順位改善・上位表示（{move_str}）", "現状の掲載内容を継続観測する", None)
        return ("info", "継続観測", f"上位表示を維持（{position:.1f}位）", "現状の掲載内容を継続観測する", None)

    # 市場ごとの最低Imp未満は、順位そのものが1週間のブレを含む参考値。
    # 10位前後の上下で「再調査」「順位下落」を出さず、継続観測に留める（低Impの大阪等）
    if current["impressions"] < min_impressions:
        return ("info", "継続観測", f"{position:.1f}位（表示{current['impressions']}件のため順位・前週比は参考値）", "継続観測する", None)

    if position > 10:
        if moved:
            prefix = f"10位以下・順位{'改善' if delta > 0 else '下落'}（{move_str}）"
        else:
            prefix = f"10位以下（{position:.1f}位）"
        return ("info", "再調査", prefix,
                "ページ×クエリで表示URLを確認／検索結果のタイトル・descriptionを競合と比較／内部リンクを確認",
                SHORTFALL_COMPETITOR_SERP)

    if moved and delta < 0:
        severity = "warning" if delta <= -meaningful else "info"
        return (severity, "再調査", f"順位下落（{move_str}）",
                "ページ×クエリで下落クエリを特定／検索結果のタイトル・descriptionを競合と比較／内部リンクを確認",
                SHORTFALL_COMPETITOR_SERP)
    if moved and delta > 0:
        return ("info", "継続観測", f"順位改善（{move_str}）", "改善傾向を継続観測する", None)

    return ("info", "継続観測", f"順位はおおむね横ばい（{position:.1f}位）", "継続観測する", None)


def analyze_market(
    bundle: dict,
    previous_bundle: dict | None,
    queries_cfg: dict,
    thresholds_cfg: dict,
    weeks_available: int = 1,
    recent_bundles: list[tuple[str, dict]] | None = None,
) -> list[dict]:
    """recent_bundles: (cur_end, bundle) を古い順に並べた直近最大4週分（当週含む）。
    AIO競合ドメインの複数週継続性判定に使う。省略時は当週のみで判定する
    （1回だけの新規引用は継続性なしとして扱われる＝安全側に倒れる）。
    """
    findings: list[dict] = []

    query_daily_cur = bundle["gsc"]["current"]["gsc_query_daily"]
    query_daily_prv = bundle["gsc"]["comparison"]["gsc_query_daily"]
    page_query_cur = bundle["gsc"]["current"]["gsc_page_query_daily"]
    aio_by_query = {o["query"]: o for o in bundle.get("aio_observations", [])}
    prev_aio_by_query = {o["query"]: o for o in (previous_bundle or {}).get("aio_observations", [])}

    domain_history = _build_domain_history(recent_bundles or [(bundle["period"]["cur_end"], bundle)])
    deal_queries = {m["query"] for m in queries_cfg["markets"].values() if m.get("tier", "deal") == "deal"}
    cross_market_counts = _cross_market_domain_counts(bundle, deal_queries)
    cur_end = bundle["period"]["cur_end"]

    for query_id, m in queries_cfg["markets"].items():
        representative_query = m["query"]
        tier = m.get("tier", "deal")
        matcher = _query_matcher(m)
        min_impressions = market_min_impressions(m, thresholds_cfg)

        matched_cur = _matched_rows(query_daily_cur, matcher)
        matched_prv = _matched_rows(query_daily_prv, matcher)
        current = sum_metrics(matched_cur)
        comparison = sum_metrics(matched_prv)
        matched_query_variants = _matched_query_variants(matched_cur)

        top_url = _top_url_for_market(page_query_cur, matcher)
        # aio: false（参考クエリ）はAIO観測対象外。過去週の観測が残っていても使わない
        aio_obs = aio_by_query.get(representative_query) if m.get("aio", True) else None

        if tier == "reference":
            severity, judgment, message, recommendation, shortfall = (
                "info", "参考", "検索Vol.が小さい参考クエリ（市場評価の対象外）", "判定しない（数値のみ参考表示）", None)
        else:
            severity, judgment, message, recommendation, shortfall = _judge_seo(
                current, comparison, thresholds_cfg, min_impressions)

        aio_note = "AIO観測データなし" if m.get("aio", True) else "AIO観測対象外"
        seo_aio_agree = None
        cite_status = normalize.citation_status(aio_obs)
        if aio_obs is not None:
            status = aio_obs["acquisition_status"]
            if status == "success":
                if cite_status == normalize.CITATION_UNAVAILABLE:
                    # 本文・引用元を取得できていない＝TCD引用の有無は不明。「引用されていない」とは書かない
                    aio_note = "AIOの本文・引用元を取得できず、TCD引用は判定不可"
                elif aio_obs["aio_generated"] and not aio_obs["tcd_cited"]:
                    aio_note = "AIO検索結果に、TCDは引用されていない"
                    severity = _escalate(severity, "warning")
                    seo_direction = "positive" if (position_delta(current["position"], comparison.get("position")) or 0) > 0 else "negative"
                    seo_aio_agree = seo_direction == "negative"
                elif aio_obs["aio_generated"] and aio_obs["tcd_cited"]:
                    aio_note = "AIOでTCD引用あり"
                    seo_direction = "positive" if (position_delta(current["position"], comparison.get("position")) or 0) >= 0 else "negative"
                    seo_aio_agree = seo_direction == "positive"
                else:
                    aio_note = "AIOは生成されていない（observed）"
            elif status == "not_generated":
                aio_note = "AIOは生成されていない（observed）"
            else:
                aio_note = f"AIO観測が未完了（{status}）"

        competitor_summary = _competitor_summary(aio_obs, prev_aio_by_query.get(representative_query))

        evidence = {
            "query": representative_query, "market": m["market"], "region": m["region"],
            "tier": tier,
            "search_volume_ref": m.get("search_volume_ref"),
            "search_volume_as_of": queries_cfg.get("search_volume_as_of"),
            "min_impressions": min_impressions,
            "match_tokens": m.get("all") or [],
            "match_exact": m.get("exact") or [],
            "current": current, "comparison": comparison,
            "matched_query_variants": matched_query_variants,
            "top_url": top_url,
            "aio": aio_obs,
            # 表示・集計用の引用元（引用順・重複除去・Google内部リンク除外）。元データは aio.cited_domains に保持
            "aio_citations": {"status": cite_status, "domains": normalize.competitor_domains(aio_obs)},
            "competitor_summary": competitor_summary,
        }

        # データ量の判定も市場ごとの最低Impを基準にする（一律50impを要求しない）
        market_thresholds = {**thresholds_cfg, "low_data": {**thresholds_cfg["low_data"], "low_impressions": min_impressions}}
        confidence = assess_confidence({
            "impressions": current["impressions"],
            "clicks": current["clicks"],
            "weeks_available": weeks_available,
            "seo_aio_agree": seo_aio_agree,
            "page_query_agree": None,
        }, market_thresholds)

        findings.append(make_finding(
            id=f"market-{query_id}",
            category="market",
            severity=severity,
            confidence=confidence["level"],
            message=f"{message}／{aio_note}",
            recommendation=recommendation,
            judgment=judgment,
            evidence=evidence,
            source={"query_id": query_id, "query": representative_query},
            shortfall=shortfall,
            investigate=INVESTIGATE_SERP if judgment == "表示なし" else None,
        ))

        # 競合動向は重点商談市場のみ（テーマ市場のAIOは一般情報サイトの引用が多くノイズになるため）
        if tier == "deal":
            findings.extend(_competitor_findings(
                query_id, representative_query, competitor_summary,
                domain_history.get(representative_query, {}), cross_market_counts, cur_end,
            ))

    return findings


def _build_domain_history(recent_bundles: list[tuple[str, dict]]) -> dict[str, dict[str, set[str]]]:
    """query -> domain -> その domain が引用された週(cur_end)の集合。直近最大4週分（当週含む）。"""
    history: dict[str, dict[str, set[str]]] = {}
    for cur_end, b in recent_bundles:
        for obs in b.get("aio_observations", []):
            if obs.get("acquisition_status") != "success":
                continue
            query = obs.get("query")
            for d in normalize.competitor_domains(obs):
                history.setdefault(query, {}).setdefault(d, set()).add(cur_end)
    return history


def _cross_market_domain_counts(bundle: dict, deal_queries: set[str]) -> dict[str, int]:
    """当週、同一ドメインが何件の重点商談市場クエリで引用されているか。"""
    counts: dict[str, int] = {}
    for obs in bundle.get("aio_observations", []):
        if obs.get("acquisition_status") != "success" or obs.get("query") not in deal_queries:
            continue
        for d in normalize.competitor_domains(obs):
            counts[d] = counts.get(d, 0) + 1
    return counts


def _citations_available(obs: dict | None) -> bool:
    """引用元を比較に使える観測か。取得不可（page_tokenのみ等）の週を「引用0件」として
    扱うと、翌週に全ドメインが「新規」、当週に全ドメインが「消失」と誤判定されるため除外する。"""
    return bool(
        obs and obs.get("acquisition_status") == "success"
        and normalize.citation_status(obs) != normalize.CITATION_UNAVAILABLE
    )


def _competitor_summary(current_obs: dict | None, previous_obs: dict | None) -> dict:
    """AIO引用競合ドメインの状況（件数・前週比・新規/消失）。
    比較不可（初回観測・前回未成功等）の場合も comparison_available=False で
    明示する（「変化なし」と「未取得のため比較不能」を混同しない）。
    """
    current_available = _citations_available(current_obs)
    current_domains = sorted(normalize.competitor_domains(current_obs)) if current_available else []

    comparison_available = current_available and _citations_available(previous_obs)

    if not comparison_available:
        return {
            "current_count": len(current_domains) if current_available else None,
            "current_domains": current_domains,
            "previous_count": None,
            "new_domains": [],
            "dropped_domains": [],
            "comparison_available": False,
        }

    previous_domains = set(normalize.competitor_domains(previous_obs))
    current_set = set(current_domains)

    return {
        "current_count": len(current_domains),
        "current_domains": current_domains,
        "previous_count": len(previous_domains),
        "new_domains": sorted(current_set - previous_domains),
        "dropped_domains": sorted(previous_domains - current_set),
        "comparison_available": True,
    }


def _competitor_findings(
    query_id: str,
    priority_query: str,
    summary: dict,
    domain_periods: dict[str, set[str]],
    cross_market_counts: dict[str, int],
    cur_end: str,
) -> list[dict]:
    """新規ドメインは、以下のいずれかを満たす場合のみ severity=warning（Priority Action対象）とする。
    1回だけの新規引用は severity=info・judgment=継続観測 に倒す（単週変化を参入と断定しない）。
      - 直近4回の観測中2回以上（当週含む）引用 ＝ 実質「2週連続」も含む
      - 当週、複数の重点市場で引用
    """
    if not summary.get("comparison_available"):
        return []

    new_domains = summary["new_domains"]
    dropped_domains = summary["dropped_domains"]

    findings: list[dict] = []
    for domain in new_domains:
        periods_cited = domain_periods.get(domain, {cur_end})
        market_count = cross_market_counts.get(domain, 0)
        is_recurring = len(periods_cited) >= 2
        is_multi_market = market_count >= 2

        if is_recurring or is_multi_market:
            reasons = []
            if is_recurring:
                reasons.append(f"直近{len(periods_cited)}回の観測中{len(periods_cited)}回引用")
            if is_multi_market:
                reasons.append(f"{market_count}市場で引用")
            findings.append(make_finding(
                id=f"competitor-{query_id}-new-{domain}",
                category="competitor",
                severity="warning",
                confidence="Medium",
                message=f"「{priority_query}」のAIO引用に競合ドメイン「{domain}」が継続的に確認された（{'・'.join(reasons)}）",
                recommendation="新規引用ドメインの実績・コンテンツを確認し、差別化ポイントを検討する",
                judgment="再調査",
                evidence={
                    "query": priority_query, "domain": domain, "direction": "new",
                    "periods_cited": sorted(periods_cited), "cross_market_count": market_count,
                },
                source={"query_id": query_id, "query": priority_query},
                shortfall=SHORTFALL_COMPETITOR_SERP,
            ))
        else:
            findings.append(make_finding(
                id=f"competitor-{query_id}-new-{domain}",
                category="competitor",
                severity="info",
                confidence="Low",
                message=f"「{priority_query}」のAIO引用に新規ドメイン「{domain}」が確認された（今回が初回、継続性は未確認）",
                recommendation="継続観測する（1回のみの新規引用では参入と断定しない）",
                judgment="継続観測",
                evidence={
                    "query": priority_query, "domain": domain, "direction": "new",
                    "periods_cited": sorted(periods_cited), "cross_market_count": market_count,
                },
                source={"query_id": query_id, "query": priority_query},
            ))

    if dropped_domains:
        findings.append(make_finding(
            id=f"competitor-{query_id}-dropped",
            category="competitor",
            severity="info",
            confidence="Medium",
            message=f"「{priority_query}」のAIO引用から前回確認されたドメインが消失: {', '.join(dropped_domains)}",
            recommendation="継続観測する（消失は一時的な変動の可能性もあるため断定しない）",
            judgment="継続観測",
            evidence={"query": priority_query, "domains": dropped_domains, "direction": "dropped"},
            source={"query_id": query_id, "query": priority_query},
        ))

    return findings
