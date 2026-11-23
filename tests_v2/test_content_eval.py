"""SEO/AIO・コンテンツ評価中心の構成（2026-11-23）の評価ロジック。"""
from src_v2.report import content_eval as ce

TH = {"low_data": {"low_impressions": 50, "position_change_meaningful": 2.0}}


def _page(cur, prv, objective="knowledge", url="https://tcd.jp/a.html"):
    return {"id": "page-a", "evidence": {"page_id": "a", "title": "A｜サイト名", "url": url, "objective": [objective],
                                         "current": cur, "comparison": prv,
                                         "top_queries": [{"query": "q", "impressions": 10}]}}


def _m(imp, clicks, pos):
    return {"impressions": imp, "clicks": clicks, "ctr": clicks / imp if imp else 0, "position": pos}


def _eval(cur, prv, **kw):
    return ce._evaluate_page(_page(cur, prv, **kw), TH, [], [])


def test_page_status():
    assert _eval(_m(40, 0, 5), _m(100, 0, 5))["status"] == ce.STATUS_INSUFFICIENT
    assert _eval(_m(100, 1, 8), _m(100, 1, 11))["status"] == ce.STATUS_UP
    assert _eval(_m(100, 1, 14), _m(100, 1, 10))["status"] == ce.STATUS_DOWN
    assert _eval(_m(150, 0, 12), _m(150, 0, 12.5))["status"] == ce.STATUS_NO_CLICK
    assert _eval(_m(150, 0, 6), _m(150, 0, 6.5))["status"] == ce.STATUS_NO_CLICK
    assert _eval(_m(80, 2, 13), _m(80, 2, 12.5))["status"] == ce.STATUS_FLAT_LOW
    assert _eval(_m(80, 3, 6), _m(80, 3, 6.5))["status"] == ce.STATUS_STABLE
    assert ce.STATUS_STABLE == "順位安定" and ce.STATUS_NO_CLICK == "クリック未獲得・要確認"


def test_no_click_is_not_judged_as_content_problem():
    p = _eval(_m(150, 0, 12), _m(150, 0, 12.5))
    assert "コンテンツの問題とは判定していない" in p["evaluation"]
    # 論点は問いの形で、原因を断定しない
    assert p["points"] and all("可能性" not in x and "不足している" not in x for x in p["points"])


def test_position_change_is_labeled_week_over_week():
    p = _eval(_m(100, 1, 8), _m(100, 1, 11))
    assert "前週比" in p["status"] and "前週比・1週の順位変動" in p["evaluation"]
    assert p["short_title"] == "A" and p["points"] == []


def test_insufficient_service_page_stays_candidate():
    p = _eval(_m(10, 0, 30), _m(12, 0, 25), objective="conversion")
    assert p["status"] == ce.STATUS_INSUFFICIENT and "改善候補" in p["next_action"] and p["points"]
    lead = ce.build_lead([], [p], {"summary": "x"}, {"counts": {"評価可能": 0, "要調査": 0, "評価保留": 0}, "item_count": 0, "rows": []},
                         {"tiles": []}, {})
    assert lead["thin_candidates"] == [p]


def _ini(iid, name, urls, verdict, m=None, tests=None):
    m = m or {"pre": {"impressions": 100, "clicks": 5, "ctr": 0.05}, "post": {"impressions": 90, "clicks": 6, "ctr": 0.066}}
    return {"id": iid, "judgment": verdict, "recommendation": "r",
            "evidence": {"name": name, "urls": urls, "implemented_at": "2026-10-01", "hypothesis": f"h-{name}",
                         "metrics": m, "control": m, "primary_metric": "counts", "tests": tests or {},
                         "queries": {"decreased": [{"query": "x", "pre": 9, "post": 1}]}}}


def test_initiatives_merge_keeps_individual_ids():
    s = ce.build_initiative_summary([
        _ini("ini-a", "A", ["u1", "u2"], "指標で方向が異なる（原因未確定）"),  # 旧文言も受ける
        _ini("ini-b", "B", ["u2", "u1"], ce.VERDICT_MIXED),
        _ini("ini-c", "C", ["u3"], "評価不能"),
    ])
    assert len(s["rows"]) == 2 and s["item_count"] == 3
    merged = s["rows"][0]
    assert merged["merged"] and merged["class"] == "要調査" and "「x」" in merged["next_action"]
    assert [i["id"] for i in merged["items"]] == ["ini-a", "ini-b"]
    assert all(i["verdict"] == ce.VERDICT_MIXED for i in merged["items"])
    assert merged["hypothesis"] == "h-A／h-B"
    assert s["counts"] == {"評価可能": 0, "評価保留": 1, "要調査": 1}


def test_initiatives_not_merged_when_metrics_differ():
    other = {"pre": {"impressions": 50, "clicks": 1, "ctr": 0.02}, "post": {"impressions": 60, "clicks": 1, "ctr": 0.016}}
    s = ce.build_initiative_summary([_ini("a", "A", ["u1"], ce.VERDICT_MIXED), _ini("b", "B", ["u1"], ce.VERDICT_MIXED, m=other)])
    assert len(s["rows"]) == 2


def test_metric_lines_do_not_call_slower_growth_a_decrease():
    m = {"pre": {"impressions": 827, "clicks": 6, "ctr": 0.0073}, "post": {"impressions": 904, "clicks": 11, "ctr": 0.0122}}
    c = {"pre": {"impressions": 54154, "clicks": 860, "ctr": 0.0159}, "post": {"impressions": 68893, "clicks": 1091, "ctr": 0.0158}}
    ev = {"metrics": m, "control": c, "primary_metric": "counts",
          "tests": {"impressions": {"assessable": True, "t": -2.1}, "clicks": {"assessable": True, "t": 0.8},
                    "ctr": {"assessable": True, "t": 1.0}}}
    lines = {x["key"]: x for x in ce._metric_lines(ev, 2.0)}
    assert lines["impressions"]["target"] == "827→904（+9.3%）" and lines["impressions"]["control"] == "+27.2%"
    assert lines["impressions"]["diff"] == "比較対照を下回る（対象ページの値は増加）"
    assert lines["clicks"]["diff"] == "差は日々の変動の範囲"
    assert lines["ctr"]["target"] == "0.7%→1.2%" and not lines["ctr"]["judged"]
    assert "減少" not in ce._observed_text(list(lines.values()))


def test_domain_type():
    assert ce._domain_type("dhw.ac.jp") == "教育機関"
    assert ce._domain_type("mirasapo-plus.go.jp") == "公的機関"
    assert ce._domain_type("ja.wikipedia.org") == "百科事典"
    assert ce._domain_type("prdx.co.jp") == "企業・メディア等"


def test_citation_count_separates_total_and_change():
    summary = {"comparison_available": True, "previous_count": 4, "new_domains": ["b.jp", "c.jp"], "dropped_domains": ["z.jp"]}
    ch = ce._citation_change(summary, [], ["a.jp", "b.jp", "c.jp", "d.jp", "e.jp"])
    assert ce.citation_count_label(ch) == "現在5件（うち前回なし2件）／前回から消失1件（前回4件）"
    assert ce.citation_count_label(ce._citation_change({"comparison_available": False}, [], ["a.jp"])) == \
        "現在1件（前回比較不可）"
    seen = [{"evidence": {"direction": "new", "domain": "b.jp", "periods_cited": ["2026-10-30", "2026-11-20"]}}]
    assert ce._citation_change(summary, seen, ["b.jp"])["new_seen_before"] == ["b.jp"]


def test_comparison_window_notes_excluded_initial_period():
    ev = {"implemented_at": "2026-10-01", "windows": {"days": 28, "pre": {"start": "2026-09-03", "end": "2026-09-30"},
                                                     "post": {"start": "2026-10-24", "end": "2026-11-20"}}}
    w = ce.comparison_window(ev)
    assert w["label"] == "実装前28日間（9/3〜9/30）と直近28日間（10/24〜11/20）の比較"
    assert "実装後の初期期間（10/2〜10/23）は含まない" in w["note"]
    ev2 = {"implemented_at": "2026-11-01", "windows": {"days": 14, "pre": {"start": "2026-10-18", "end": "2026-10-31"},
                                                      "post": {"start": "2026-11-02", "end": "2026-11-15"}}}
    assert ce.comparison_window(ev2)["excluded"] is None


def _snap(gen=True, cited=False, rec="not_recommended", status="success", ts="2026-11-16T00:11:23Z"):
    return {"observed_at": ts, "acquisition_status": status, "aio_generated": gen, "citation_status": "extracted",
            "tcd_cited": cited, "recommendation_status": rec, "cited_source_count": 5, "resolvable_domain_count": 5}


def _kw(query, tier="deal"):
    return {"query": query, "tier": tier, "market_label": query, "short_label": query}


def _mf(query, prev, cur):
    return {"evidence": {"query": query, "aio_previous": prev, "aio_current": cur}}


def test_aio_changes_detect_loss_and_put_it_first():
    kws = [_kw("東京"), _kw("ブランディング", tier="theme")]
    mfs = [_mf("東京", _snap(cited=True, rec="recommended"), _snap(ts="2026-11-23T01:46:48Z")),
           _mf("ブランディング", _snap(rec="not_applicable"), _snap(cited=True, rec="not_applicable"))]
    changes = ce.build_aio_changes(mfs, kws)
    tokyo = changes[0]["items"]
    assert tokyo["recommended"]["change"] == "あり→なし" and tokyo["cited"]["change"] == "あり→なし"
    assert tokyo["generated"]["change"] == "変化なし"
    assert changes[0]["previous_observed"] == "11/16 09:11" and changes[0]["current_observed"] == "11/23 10:46"
    assert changes[1]["items"]["recommended"]["change"] == "対象外"
    assert changes[1]["items"]["cited"]["change"] == "なし→あり"
    lines = ce.aio_event_lines(ce.aio_change_events(changes))
    assert lines[0]["lost"] and "TCDの会社推薦があり→なし、TCDの引用があり→なし" in lines[0]["text"]
    assert not lines[1]["lost"]


def test_aio_changes_without_previous_are_not_guessed():
    changes = ce.build_aio_changes([_mf("東京", None, _snap())], [_kw("東京")])
    assert all(v["change"].startswith("前回比較不可") and v["direction"] is None for v in changes[0]["items"].values())
    undeterminable = ce.build_aio_changes([_mf("大阪", _snap(rec="undeterminable"), _snap(rec="not_recommended"))], [_kw("大阪")])
    assert undeterminable[0]["items"]["recommended"]["change"] == "前回比較不可"


def test_aio_generation_change_is_separate_from_citation():
    changes = ce.build_aio_changes([_mf("東京", _snap(gen=True, cited=True, rec="recommended"), _snap(gen=False))], [_kw("東京")])
    items = changes[0]["items"]
    assert items["generated"]["change"] == "あり→なし"
    assert items["cited"]["cur"] == "なし（AIO未生成）"


def test_lead_always_keeps_aio_losses_and_caps_lengths():
    lost = [{"text": f"loss{i}", "query": f"k{i}", "lost": True, "priority": 0, "events": [{"market_label": f"k{i}", "label": "TCDの引用", "direction": "lost"}]}
            for i in range(4)]
    lead = ce.build_lead([], [], {"summary": "", "change_lines": lost},
                         {"counts": {"評価可能": 0, "要調査": 0, "評価保留": 0}, "item_count": 0, "rows": []}, {"tiles": []}, {})
    assert lead["changes"] == ["loss0", "loss1", "loss2", "loss3"]  # 消失は上限を超えても落とさない
    assert len(lead["actions"]) <= ce.LEAD_MAX_ACTIONS


def test_citation_count_not_compared_when_previous_domains_unresolved():
    ch = ce._citation_change({"comparison_available": True, "previous_count": 0, "new_domains": ["a.jp"], "dropped_domains": []},
                             [], ["a.jp"], {"cited_source_count": 20, "resolvable_domain_count": 0})
    assert ce.citation_count_label(ch) == "現在1件（前回比較不可: 前回の引用元はドメインを特定できず）"


def test_aio_daily_note_summarizes_daily_and_missing_days():
    daily = {"by_query": {"東京": [
        {"date": f"2026-11-{d:02d}", "status": "OK_WITH_AIO", "tcd_cited": True, "recommendation_status": "recommended"}
        for d in (9, 10, 11, 12, 13, 16, 17, 18, 19, 20)]}}
    note = ce.aio_daily_note(daily, "東京", "2026-11-23T01:46:48Z")
    assert note == "日次のAIO監視では、11/9〜11/20の10回の観測すべてで推薦あり・引用あり。11/21〜11/23の日次データはない"
    daily["by_query"]["東京"][0]["tcd_cited"] = False
    assert "推薦あり10回・引用あり9回" in ce.aio_daily_note(daily, "東京", "2026-11-23T01:46:48Z")
    assert ce.aio_daily_note({"by_query": {}}, "東京", None) is None


def test_aio_daily_context_reads_only_window_and_queries(tmp_path):
    import json
    from src_v2.analysis import summary
    for day in ("2026-11-01", "2026-11-20", "2026-11-24"):
        (tmp_path / f"{day}.json").write_text(json.dumps([
            {"query": "東京", "status": "OK_WITH_AIO", "aio_exists": True, "tcd_cited": True, "recommendation_status": "recommended"},
            {"query": "他", "status": "OK_WITH_AIO"}]), encoding="utf-8")
    bundle = {"aio_observations": [{"query": "東京", "observed_at": "2026-11-23T01:46:48Z"}]}
    ctx = summary._aio_daily_context(bundle, tmp_path)
    assert ctx["window"] == {"start": "2026-11-09", "end": "2026-11-23"}
    assert [r["date"] for r in ctx["by_query"]["東京"]] == ["2026-11-20"] and "他" not in ctx["by_query"]


def test_regional_lp_flat_is_not_called_stable_for_region_keywords():
    f = _page(_m(283, 2, 9.1), _m(269, 0, 9.1), objective="regional", url="https://tcd.jp/osaka")
    f["evidence"]["title"] = "大阪のブランディング会社｜TCD"
    f["evidence"]["top_queries"] = [{"query": "tcd", "impressions": 30}, {"query": "dccd", "impressions": 10}]
    p = ce._evaluate_page(f, TH, [], [])
    assert p["status"] == ce.STATUS_STABLE  # 区分（判定）は変えない
    assert p["evaluation"].startswith("全クエリ平均順位は横ばい（平均9.1位）。ただし地域キーワードでの評価は別途確認")
    assert "「大阪」を含まない検索語" in p["evaluation"] and "1ページ目" not in p["evaluation"]


def test_aio_comparison_supplements_only_explain_uncomparable_items():
    changes = ce.build_aio_changes([_mf("大阪", _snap(rec="undeterminable"), _snap(rec="undeterminable")),
                                    _mf("東京", _snap(cited=True, rec="recommended"), _snap())], [_kw("大阪"), _kw("東京")])
    sup = ce.aio_comparison_supplements(changes)
    assert sup == ["大阪: TCDの会社推薦は前回 判定不可・今回 判定不可のため前回比較不可"]
