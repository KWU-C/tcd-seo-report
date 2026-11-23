"""施策の効果確認（実装前後の比較、2026-11-23週〜）。"""
import random
from datetime import date, timedelta

from src_v2.analysis import initiatives
from src_v2.fetch_initiatives import initiative_windows, page_regex

REVIEW = {"max_window_days": 28, "min_post_days": 14, "significance_t": 2.0}
THRESHOLDS = {"initiative_review": REVIEW}
URL = "https://tcd.jp/branding/definition/inner-branding.html"


def _md(tmp_path, body):
    p = tmp_path / "current-initiatives.md"
    p.write_text(body, encoding="utf-8")
    return p


def _days(start, n):
    d = date.fromisoformat(start)
    return [(d + timedelta(days=i)).isoformat() for i in range(n)]


def _item(implemented="2026-10-02", gsc_end="2026-11-13", target=lambda d, pre: (100, 2), site=lambda d: (10000, 200),
          queries_pre=None, queries_post=None, urls=(URL,)):
    """target(day, is_pre) → (impressions, clicks)。site(day) → サイト全体の (impressions, clicks)。"""
    w = initiative_windows(date.fromisoformat(implemented), date.fromisoformat(gsc_end))
    daily, site_daily, totals = {}, {}, {"pre": [0, 0], "post": [0, 0]}
    for key in ("pre", "post"):
        for d in _days(w[key]["start"], w["days"]):
            imp, clk = target(d, key == "pre")
            daily[d] = {"clicks": clk, "impressions": imp, "position": 10.0}
            s_imp, s_clk = site(d)
            site_daily[d] = {"clicks": s_clk + clk, "impressions": s_imp + imp, "position": 10.0}
            totals[key][0] += imp
            totals[key][1] += clk
    item = {"status": "ok", "implemented_at": implemented, "urls": list(urls), "windows": w, "daily": daily}
    for key, q in (("pre", queries_pre), ("post", queries_post)):
        item[key] = {"pages": {URL: {"impressions": totals[key][0], "clicks": totals[key][1], "position": 10.0}},
                     "queries": q or {"インナーブランディング": {"impressions": totals[key][0], "clicks": totals[key][1], "position": 10.0}}}
    return item, site_daily


def _bundle(name, item, site_daily, gsc_end="2026-11-13"):
    return {"period": {"cur_end": gsc_end},
            "initiatives_gsc": {"version": 1, "gsc_end": gsc_end, "site_daily": site_daily, "items": {name: item}}}


MD_CTR = ("## メタ施策\n\n- 対象URL: " + URL + "\n- 実装日: 2026-10-02\n"
          "- 仮説: 説明文を差し替えることで、検索結果でのクリック率が改善する\n")
MD_COUNTS = "## 内部リンク施策\n\n- 対象URL: " + URL + "\n- 実装日: 2026-10-02\n- 仮説: 内部リンクで評価が向上する\n"


def test_windows_exclude_implementation_day_and_cap_at_28_days():
    w = initiative_windows(date(2026, 10, 2), date(2026, 11, 13))
    assert w["post"] == {"start": "2026-10-17", "end": "2026-11-13"}
    assert w["pre"] == {"start": "2026-09-04", "end": "2026-10-01"}
    assert w["days"] == 28 and w["elapsed_days"] == 42 and w["excluded_day"] == "2026-10-02"


def test_windows_short_after_implementation_use_same_length_before():
    w = initiative_windows(date(2026, 11, 3), date(2026, 11, 13))
    assert w["days"] == 10
    assert w["post"]["start"] == "2026-11-04" and w["pre"] == {"start": "2026-10-24", "end": "2026-11-02"}


def test_page_regex_matches_path_exactly():
    import re
    rx = page_regex([URL])
    assert re.match(rx, URL) and re.match(rx, "https://www.tcd.jp/branding/definition/inner-branding.html")
    assert not re.match(rx, URL + "?x=1") and not re.match(rx, "https://tcd.jp/branding/definition/inner-branding.htmlx")


def test_zero_clicks_make_ctr_initiative_not_assessable_with_reason(tmp_path):
    """表示回数 302→268 のように減っていても、クリック0件ならクリック率は評価不能（再調査にしない）。"""
    item, site = _item(target=lambda d, pre: (11 if pre else 10, 0))
    f = initiatives.analyze_initiatives(_md(tmp_path, MD_CTR), _bundle("メタ施策", item, site), [], THRESHOLDS)[0]
    assert f["judgment"] == "評価不能"
    assert "クリックが実装前0件・実装後0件" in f["message"]
    assert f["evidence"]["primary_metric"] == "ctr"
    assert "実装前後 各28日" in f["message"]


def test_decline_matching_control_is_change_only_not_failure(tmp_path):
    """対象ページが30%減っても、サイト全体も同じだけ減っていれば施策の効果（失敗）とは判断しない。"""
    rng = random.Random(1)

    def target(d, pre):
        base = 100 if pre else 70
        return base + rng.randint(-10, 10), 3

    def site(d):
        base = 10000 if d < "2026-10-02" else 7000
        return base + rng.randint(-800, 800), 200

    item, sd = _item(target=target, site=site)
    f = initiatives.analyze_initiatives(_md(tmp_path, MD_COUNTS), _bundle("内部リンク施策", item, sd), [], THRESHOLDS)[0]
    assert f["judgment"] == "数値の変化のみ"
    rows = f["evidence"]["metrics"]
    assert rows["post"]["impressions"] < rows["pre"]["impressions"]
    assert "施策の効果とは判断しない" in f["message"]


def test_rise_beyond_control_is_possible_effect(tmp_path):
    rng = random.Random(2)
    item, sd = _item(target=lambda d, pre: ((100 if pre else 160) + rng.randint(-5, 5), (3 if pre else 6)),
                     site=lambda d: (10000 + rng.randint(-300, 300), 200))
    f = initiatives.analyze_initiatives(_md(tmp_path, MD_COUNTS), _bundle("内部リンク施策", item, sd), [], THRESHOLDS)[0]
    assert f["judgment"] == "比較対照を上回る変化（原因未確定）"
    assert "施策の効果とは断定していない" in f["recommendation"]
    assert "原因（施策・検索クエリの構成・同時期の変更・季節要因など）は確定していない" in f["message"]


def test_waiting_until_min_post_days(tmp_path):
    md = "## 新施策\n\n- 対象URL: " + URL + "\n- 実装日: 2026-11-05\n"
    item, sd = _item(implemented="2026-11-05")
    f = initiatives.analyze_initiatives(_md(tmp_path, md), _bundle("新施策", item, sd), [], THRESHOLDS)[0]
    assert f["judgment"] == "効果確認待ち"
    assert "8日分（14日分そろってから比べる）" in f["message"]


def test_unknown_date_and_no_target_reasons(tmp_path):
    md = "## A\n\n- 対象: x\n- 実装日: unknown\n\n## B\n\n- 対象: 一覧系ページ全般\n- 実装日: 2026-10-19\n"
    bundle = {"period": {"cur_end": "2026-11-13"},
              "initiatives_gsc": {"version": 1, "gsc_end": "2026-11-13", "site_daily": {},
                                  "items": {"A": {"status": "no_date"}, "B": {"status": "no_target", "implemented_at": "2026-10-19"}}}}
    fa, fb = initiatives.analyze_initiatives(_md(tmp_path, md), bundle, [], THRESHOLDS)
    assert fa["judgment"] == "評価不能" and "実装日が未登録" in fa["message"]
    assert fb["judgment"] == "評価対象未設定" and "対象ページを特定できない" in fb["message"]


def test_legacy_bundle_without_prepost_data_is_not_assessable(tmp_path):
    f = initiatives.analyze_initiatives(_md(tmp_path, MD_CTR), {"period": {"cur_end": "2026-11-13"}}, [], THRESHOLDS)[0]
    assert f["judgment"] == "評価不能" and "比較用のSearch Consoleデータが無い" in f["message"]


def test_overlapping_initiatives_are_noted(tmp_path):
    md = MD_COUNTS + "\n## 同日施策\n\n- 対象URL: " + URL + "\n- 実装日: 2026-10-02\n"
    item, sd = _item()
    bundle = {"period": {"cur_end": "2026-11-13"},
              "initiatives_gsc": {"version": 1, "gsc_end": "2026-11-13", "site_daily": sd,
                                  "items": {"内部リンク施策": item, "同日施策": item}}}
    f = initiatives.analyze_initiatives(_md(tmp_path, md), bundle, [], THRESHOLDS)[0]
    assert f["evidence"]["overlaps"] == ["同日施策"]
    assert "同日施策" in f["message"]


def test_primary_metric_explicit_field_wins():
    assert initiatives.primary_metric({"primary_metric": "表示回数", "hypothesis": "クリック率が上がる"}) == "counts"
    assert initiatives.primary_metric({"hypothesis": "クリック率が上がる"}) == "ctr"
    assert initiatives.primary_metric({}) == "counts"


def test_view_model_shows_elapsed_days_and_periods(tmp_path):
    from src_v2.report import view_model
    item, sd = _item(target=lambda d, pre: (11 if pre else 10, 0))
    f = initiatives.analyze_initiatives(_md(tmp_path, MD_CTR), _bundle("メタ施策", item, sd), [], THRESHOLDS)[0]
    v = view_model._initiative_view({**f, "display_message": f["message"]})
    assert v["elapsed_label"] == "実装日 10/2(金) から42日（Search Consoleの確定済み最新日 11/13(金) まで）"
    assert "実装前28日間 9/4(金)〜10/1(木) と 直近28日間 10/17(土)〜11/13(金)" in v["period_label"]
    assert "実装後の初期期間 10/3(土)〜10/16(金) は含まない" in v["period_label"]
    assert [r["label"] for r in v["metrics_rows"]] == ["表示回数", "クリック数", "クリック率", "平均掲載順位（参考・小さいほど上位）"]


def test_small_counts_are_not_assessable_even_if_ratio_jumps(tmp_path):
    """表示回数 1→14 のような少数データは、増え方が大きくても「効果の可能性あり」にしない。"""
    def target(d, pre):
        return (1 if d == "2026-09-10" else 0) if pre else (1 if d.endswith(("1", "5")) else 0), 0
    item, sd = _item(target=target)
    th = {**THRESHOLDS, "low_data": {"low_impressions": 50, "low_clicks": 5}}
    f = initiatives.analyze_initiatives(_md(tmp_path, MD_COUNTS), _bundle("内部リンク施策", item, sd), [], th)[0]
    assert f["judgment"] == "評価不能"
    assert "表示回数が実装前1・実装後" in f["message"] and "日々の変動と区別できない" in f["message"]


def test_adverse_label_is_hedged(tmp_path):
    rng = random.Random(3)
    item, sd = _item(target=lambda d, pre: ((160 if pre else 100) + rng.randint(-5, 5), (6 if pre else 3)),
                     site=lambda d: (10000 + rng.randint(-300, 300), 200))
    f = initiatives.analyze_initiatives(_md(tmp_path, MD_COUNTS), _bundle("内部リンク施策", item, sd), [], THRESHOLDS)[0]
    assert f["judgment"] == "比較対照を下回る変化（原因未確定）" and f["severity"] == "info"
    assert "施策の悪影響とは判断していない" in f["message"]
    assert "表示回数（対象 " in f["message"] and "比較対照 " in f["message"]
    for word in ("悪化", "逆効果", "失敗"):
        assert word not in f["message"] + f["recommendation"]


def test_canonical_without_target_is_not_set_and_not_compared(tmp_path):
    md = "## アーカイブページのcanonical・URL整合性改善\n\n- 対象: 一覧系ページ全般\n- 実装日: 2026-10-19\n"
    name = "アーカイブページのcanonical・URL整合性改善"
    bundle = {"period": {"cur_end": "2026-11-13"},
              "initiatives_gsc": {"version": 1, "gsc_end": "2026-11-13", "site_daily": {},
                                  "items": {name: {"status": "no_target", "implemented_at": "2026-10-19"}}}}
    f = initiatives.analyze_initiatives(_md(tmp_path, md), bundle, [], THRESHOLDS)[0]
    assert f["judgment"] == "評価対象未設定"
    assert "表示回数・クリック率の前後比較では判定しない" in f["message"]
    assert "URL 検査" in f["recommendation"]
    assert "metrics" not in f["evidence"]


def test_index_method_with_urls_is_not_judged_by_search_metrics(tmp_path):
    md = "## 一覧の正規URL修正\n\n- 対象URL: " + URL + "\n- 実装日: 2026-10-02\n- 評価方法: 正規化・インデックス確認\n"
    item, sd = _item(target=lambda d, pre: (100 if pre else 300, 3 if pre else 30))
    f = initiatives.analyze_initiatives(_md(tmp_path, md), _bundle("一覧の正規URL修正", item, sd), [], THRESHOLDS)[0]
    assert f["judgment"] == "検索流入の前後比較の対象外"
    assert "metrics" not in f["evidence"]
