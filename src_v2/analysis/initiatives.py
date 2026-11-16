"""施策の効果確認（docs/current-initiatives.md × 実装前後の比較、2026-11-23週〜）。

docs/current-initiatives.md はユーザー管理の正本。ここでは記載内容をパースして評価するのみで、
実装日・評価予定日をコード側から推測・確定することはしない（unknown は unknown のまま扱う）。

比較の方法（2026-11-16 指示）:
  - 実装前と実装後の同じ日数（最大28日。実装日当日は除く）を比べる。今週と先週の比較では評価しない。
  - 「数値の変化」と「施策の効果」を分ける。対象ページの変化を、同じ期間の対象ページ以外（比較対照）の
    変化と比べ、偶然の変動で説明できる範囲かを統計的に確かめる。説明できない差があるときだけ
    「効果の可能性あり」とし、最終判断は担当者に委ねる。
  - ±20% や平均10位のような固定の線で成功・失敗を決めない。掲載順位は検索クエリの構成で変わるため、
    判定には使わず参考として示す。
  - 評価できない場合は理由を書く（実装日なし・対象ページ不明・実装後のデータ不足・クリック0件 など）。

データは pipeline（src_v2/fetch_initiatives.py）が normalized.json の initiatives_gsc に書き込む。
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date
from pathlib import Path
from statistics import mean, variance

from src_v2 import normalize
from src_v2.analysis.common import make_finding

_KEY_MAP = {
    "対象": "target",
    "対象URL": "target_url",
    "状態": "status",
    "実装日": "implemented_at",
    "評価予定日": "evaluation_due",
    "メモ": "note",
    "仮説": "hypothesis",
    "成功シグナル": "success_signal",
    "主指標": "primary_metric",
    "評価方法": "evaluation_method",
}

# 判定（judgment）。観測した事実（比較対照と異なる変化があるか）と、原因（施策の効果か）を分ける。
# 比較対照と異なる変化を観測しても、原因は確定しない（施策・検索クエリの構成・同時期の変更・季節要因などがありうる）。
VERDICT_NOT_ASSESSABLE = "評価不能"
VERDICT_NOT_SET = "評価対象未設定"
VERDICT_OTHER_METHOD = "検索流入の前後比較の対象外"
VERDICT_WAITING = "効果確認待ち"
VERDICT_CHANGE_ONLY = "数値の変化のみ"
VERDICT_POSSIBLE = "比較対照と異なる増加（原因未確定）"
VERDICT_ADVERSE = "比較対照と異なる減少（原因未確定）"
VERDICT_MIXED = "指標で方向が異なる（原因未確定）"
ALL_VERDICTS = (VERDICT_NOT_ASSESSABLE, VERDICT_NOT_SET, VERDICT_OTHER_METHOD, VERDICT_WAITING, VERDICT_CHANGE_ONLY,
                VERDICT_POSSIBLE, VERDICT_ADVERSE, VERDICT_MIXED)

# 評価方法。正規化（canonical）・インデックスの施策は、検索流入（表示回数・クリック率）の前後比較では判定しない。
METHOD_SEARCH = "search"
METHOD_INDEX = "index"
_INDEX_KEYWORDS = ("canonical", "正規化", "インデックス")

# 主指標
METRIC_CTR = "ctr"
METRIC_COUNTS = "counts"  # 表示回数とクリック数
_PRIMARY_LABELS = {"クリック率": METRIC_CTR, "ctr": METRIC_CTR, "表示回数": METRIC_COUNTS, "クリック": METRIC_COUNTS,
                   "クリック数": METRIC_COUNTS}
METRIC_NAMES = {"impressions": "表示回数", "clicks": "クリック数", "ctr": "クリック率"}


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
            key = _KEY_MAP.get(item.group(1).strip())
            if key and key not in current:
                current[key] = item.group(2).strip()

    if current is not None:
        initiatives.append(current)
    return initiatives


def parse_implemented_at(initiative: dict) -> date | None:
    raw = (initiative.get("implemented_at") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


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


def resolve_target_urls(initiative: dict, pages_cfg: list[dict]) -> list[str]:
    """対象ページのURL。対象URL が書かれていれば pages.yaml に無くてもそのURLを使う。"""
    if initiative.get("target_url"):
        return [normalize.normalize_url(initiative["target_url"])["normalized_url"]]
    ids = set(_resolve_target_page_ids(initiative, pages_cfg))
    return sorted(normalize.normalize_url(p["url"])["normalized_url"] for p in pages_cfg if p["id"] in ids)


def primary_metric(initiative: dict) -> str:
    """主指標。「主指標」欄があればそれ、なければ仮説にクリック率とあればクリック率、それ以外は表示回数・クリック数。"""
    explicit = (initiative.get("primary_metric") or "").strip().lower()
    for label, metric in _PRIMARY_LABELS.items():
        if explicit and label in explicit:
            return metric
    return METRIC_CTR if "クリック率" in (initiative.get("hypothesis") or "") else METRIC_COUNTS


def evaluation_method(initiative: dict) -> str:
    """「評価方法」欄に 正規化・インデックス とあれば index。欄が無ければ施策名に canonical・正規化・インデックス を含むかで判定。"""
    text = initiative.get("evaluation_method") or initiative["name"]
    return METHOD_INDEX if any(k in text.lower() for k in _INDEX_KEYWORDS) else METHOD_SEARCH


def _slug(name: str) -> str:
    """英数字が全く含まれない施策名（日本語のみ等）でもidが衝突しないよう、
    ASCIIスラッグが空になる場合は名前のハッシュを使う（決定的・一意）。"""
    ascii_slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    if ascii_slug:
        return ascii_slug
    return "initiative-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


# ── 集計 ─────────────────────────────────────────────────────────

def _in(day: str, window: dict) -> bool:
    return window["start"] <= day <= window["end"]


def _window_totals(pages: dict[str, dict]) -> dict:
    clicks = sum(p["clicks"] for p in pages.values())
    impressions = sum(p["impressions"] for p in pages.values())
    pos_x = sum((p["position"] or 0) * p["impressions"] for p in pages.values())
    return {"clicks": clicks, "impressions": impressions,
            "ctr": clicks / impressions if impressions else None,
            "position": pos_x / impressions if impressions else None}


def _daily_series(item: dict, site_daily: dict, window: dict) -> list[tuple[dict, dict]]:
    """[(対象, 比較対照=サイト全体−対象)]。日付は期間内のすべての日（データが無い日は0）。"""
    out = []
    d = date.fromisoformat(window["start"])
    end = date.fromisoformat(window["end"])
    while d <= end:
        key = d.isoformat()
        t = item["daily"].get(key, {"clicks": 0, "impressions": 0})
        s = site_daily.get(key, {"clicks": 0, "impressions": 0})
        out.append((t, {"clicks": s["clicks"] - t["clicks"], "impressions": s["impressions"] - t["impressions"]}))
        d = date.fromordinal(d.toordinal() + 1)
    return out


def _control_totals(series: list[tuple[dict, dict]]) -> dict:
    clicks = sum(c["clicks"] for _, c in series)
    impressions = sum(c["impressions"] for _, c in series)
    return {"clicks": clicks, "impressions": impressions, "ctr": clicks / impressions if impressions else None}


def _pct(new, old) -> float | None:
    if new is None or not old:
        return None
    return (new - old) / old * 100


def _welch_t(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = variance(a), variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return None
    return (mean(b) - mean(a)) / se


def count_test(pre: list[tuple[dict, dict]], post: list[tuple[dict, dict]], metric: str, min_count: int = 0) -> dict:
    """対象ページの値が比較対照に占める割合（日別）を、実装前と実装後で比べる（Welch の t）。

    サイト全体の季節変動・曜日変動は比較対照にも現れるため、割合で見ると施策以外の変化をある程度除ける。
    日ごとのばらつきを使うので、表示回数が大きくても、日々の揺れの範囲の差は「差あり」にしない。
    """
    t_pre = sum(t[metric] for t, _ in pre)
    t_post = sum(t[metric] for t, _ in post)
    if t_pre == 0 and t_post == 0:
        return {"assessable": False, "reason": f"{METRIC_NAMES[metric]}が実装前・実装後とも0件"}
    if min(t_pre, t_post) < min_count:
        # 少数データ（thresholds.yaml の low_data）。日々の変動を見積もれないため、評価できるかどうかの条件として使う
        return {"assessable": False,
                "reason": f"{METRIC_NAMES[metric]}が実装前{t_pre:,}・実装後{t_post:,}と少なく、日々の変動と区別できない"}
    a = [t[metric] / c[metric] for t, c in pre if c[metric] > 0]
    b = [t[metric] / c[metric] for t, c in post if c[metric] > 0]
    stat = _welch_t(a, b)
    if stat is None:
        return {"assessable": False, "reason": f"{METRIC_NAMES[metric]}の日ごとのばらつきを計算できない"}
    return {"assessable": True, "t": stat}


def ctr_test(target_pre: dict, target_post: dict, control_pre: dict, control_post: dict, min_clicks: int = 1) -> dict:
    """クリック率の変化が比較対照と違うか（オッズ比の比の対数 ÷ 標準誤差）。クリック0件の期間があれば評価不能。"""
    cells = [target_pre["clicks"], target_pre["impressions"] - target_pre["clicks"],
             target_post["clicks"], target_post["impressions"] - target_post["clicks"],
             control_pre["clicks"], control_pre["impressions"] - control_pre["clicks"],
             control_post["clicks"], control_post["impressions"] - control_post["clicks"]]
    if min(target_pre["clicks"], target_post["clicks"]) < max(min_clicks, 1):
        return {"assessable": False,
                "reason": f"対象ページのクリックが実装前{target_pre['clicks']}件・実装後{target_post['clicks']}件で、"
                          "クリック率の変化を評価できない"}
    if min(cells) <= 0:
        return {"assessable": False, "reason": "クリック率を計算できない期間がある"}
    log_ratio = (math.log(cells[2] / cells[3]) - math.log(cells[0] / cells[1])) \
        - (math.log(cells[6] / cells[7]) - math.log(cells[4] / cells[5]))
    se = math.sqrt(sum(1 / c for c in cells))
    return {"assessable": True, "t": log_ratio / se}


def _top_queries(queries: dict[str, dict], n: int = 3) -> list[dict]:
    return [{"query": q, **v} for q, v in sorted(queries.items(), key=lambda kv: -kv[1]["impressions"])[:n]]


def query_composition(pre: dict[str, dict], post: dict[str, dict], n: int = 3) -> dict:
    """検索クエリの構成。実装後の表示回数のうち、実装前にも表示されていたクエリの割合と、増減の大きいクエリ。"""
    post_total = sum(v["impressions"] for v in post.values())
    shared = sum(v["impressions"] for q, v in post.items() if q in pre)
    deltas = []
    for q in set(pre) | set(post):
        before = pre.get(q, {}).get("impressions", 0)
        after = post.get(q, {}).get("impressions", 0)
        deltas.append({"query": q, "pre": before, "post": after, "delta": after - before,
                       "pre_position": pre.get(q, {}).get("position"), "post_position": post.get(q, {}).get("position")})
    return {
        "post_impressions_in_query_data": post_total,
        "shared_share": shared / post_total if post_total else None,
        "pre_query_count": len(pre), "post_query_count": len(post),
        "top_post": _top_queries(post, n),
        "increased": [d for d in sorted(deltas, key=lambda d: -d["delta"]) if d["delta"] > 0][:n],
        "decreased": [d for d in sorted(deltas, key=lambda d: d["delta"]) if d["delta"] < 0][:n],
    }


# ── 判定 ─────────────────────────────────────────────────────────

def _observation(m: str, tests: dict, metrics: dict, control: dict) -> str:
    """観測した事実の文。数値と、比較対照・日々の変動との関係だけを書き、原因には触れない。"""
    def pct(new, old):
        return "—" if not old else f"{(new - old) / old * 100:+.1f}%"
    if m == "ctr":
        tgt = f"{(metrics['post']['ctr'] or 0) * 100:.2f}%←{(metrics['pre']['ctr'] or 0) * 100:.2f}%"
        ctl = f"{(control['post']['ctr'] or 0) * 100:.2f}%←{(control['pre']['ctr'] or 0) * 100:.2f}%"
        return f"クリック率（対象 {tgt}、比較対照 {ctl}）"
    return (f"{METRIC_NAMES[m]}（対象 {pct(metrics['post'][m], metrics['pre'][m])}、"
            f"比較対照 {pct(control['post'][m], control['pre'][m])}）")


def _verdict(primary: str, tests: dict, t_crit: float, metrics: dict | None = None,
             control: dict | None = None) -> tuple[str, list[str]]:
    """主指標の検定結果から判定と理由を決める。理由は観測した事実に限り、原因は書かない。"""
    names = ["ctr"] if primary == METRIC_CTR else ["impressions", "clicks"]
    reasons, up, down, assessable = [], [], [], []
    for m in names:
        test = tests[m]
        if not test["assessable"]:
            reasons.append(test["reason"])
            continue
        assessable.append(m)
        if test["t"] >= t_crit:
            up.append(m)
        elif test["t"] <= -t_crit:
            down.append(m)

    def obs(ms):
        return "・".join(_observation(m, tests, metrics, control) if metrics and control else METRIC_NAMES[m] for m in ms)

    cause = "原因（施策・検索クエリの構成・同時期の変更・季節要因など）は確定していない"
    if not assessable:
        return VERDICT_NOT_ASSESSABLE, reasons
    if up and down:
        return VERDICT_MIXED, reasons + [f"{obs(up)}は比較対照より増え、{obs(down)}は比較対照より減った。{cause}"]
    if up:
        return VERDICT_POSSIBLE, reasons + [
            f"{obs(up)}で、対象ページの変化が比較対照と異なり、日々の変動の範囲を超えている。{cause}"]
    if down:
        return VERDICT_ADVERSE, reasons + [
            f"{obs(down)}で、対象ページの変化が比較対照と異なり、日々の変動の範囲を超えている。"
            f"{cause}。施策の悪影響とは判断していない"]
    return VERDICT_CHANGE_ONLY, reasons + [
        "対象ページの変化は、同じ期間の対象ページ以外の変化と日々の変動で説明できる範囲（施策の効果とは判断しない）"]


_RECOMMENDATIONS = {
    VERDICT_NOT_ASSESSABLE: "評価できる材料がそろうまで、効果とも失敗とも判断しない",
    VERDICT_NOT_SET: "対象URLを docs/current-initiatives.md に登録する",
    VERDICT_OTHER_METHOD: "Search Console の URL 検査（Google が選んだ正規URL）とページのインデックス登録レポートで確認する",
    VERDICT_WAITING: "実装後のデータがそろうまで観測を続ける（途中の数値で判断しない）",
    VERDICT_CHANGE_ONLY: "施策の効果とは判断せず、観測を続ける",
    VERDICT_POSSIBLE: "変化した検索クエリ・ページ、同時期の変更を担当者が確かめる（施策の効果とは断定していない）",
    VERDICT_ADVERSE: "変化した検索クエリ・ページ、同時期の変更を担当者が確かめる（施策の悪影響とは断定していない）",
    VERDICT_MIXED: "指標ごとの変化を担当者が確かめる（原因は確定していない）",
}


def _overlaps(name: str, urls: list[str], windows: dict, items: dict) -> list[str]:
    """対象ページが重なり、比較期間（実装前の初日〜実装後の末日）の中に実装された別の施策。"""
    out = []
    for other, item in items.items():
        if other == name or not item.get("implemented_at"):
            continue
        if not (windows["pre"]["start"] <= item["implemented_at"] <= windows["post"]["end"]):
            continue
        if set(urls) & set(item.get("urls") or []):
            out.append(other)
    return out


def _base_evidence(ini: dict) -> dict:
    return {"name": ini["name"], "status": ini.get("status"), "target": ini.get("target") or ini.get("target_url"),
            "hypothesis": ini.get("hypothesis"), "success_signal": ini.get("success_signal")}


def evaluate(ini: dict, item: dict | None, site_daily: dict, all_items: dict, review_cfg: dict,
             low_data: dict | None = None) -> dict:
    """1施策の評価結果（finding の evidence 部分）と判定。"""
    primary = primary_metric(ini)
    ev = {**_base_evidence(ini), "primary_metric": primary, "implemented_at": ini.get("implemented_at")}
    status = (item or {}).get("status")
    reasons: list[str] = []

    if parse_implemented_at(ini) is None:
        raw = (ini.get("implemented_at") or "").strip()
        reasons.append("実装日が未登録" if not raw or raw.lower() == "unknown" else f"実装日「{raw}」を日付として読めない")
        return {**ev, "verdict": VERDICT_NOT_ASSESSABLE, "reasons": reasons}
    method = evaluation_method(ini)
    ev["evaluation_method"] = method
    if method == METHOD_INDEX:
        # 正規化・インデックスの施策は、表示回数・クリック率の前後比較では判定しない（別種の施策として扱う）
        base = "正規化（canonical）・インデックスの施策のため、表示回数・クリック率の前後比較では判定しない"
        if status == "no_target" or not (item or {}).get("urls"):
            return {**ev, "verdict": VERDICT_NOT_SET,
                    "reasons": [base, "対象URLが docs/current-initiatives.md に登録されていない"],
                    "recommendation": "対象URLを登録したうえで、Search Console の URL 検査（Google が選んだ正規URL）と"
                                      "ページのインデックス登録レポートで確認する"}
        return {**ev, "verdict": VERDICT_OTHER_METHOD, "urls": item.get("urls"), "reasons": [base]}
    if item is None:
        return {**ev, "verdict": VERDICT_NOT_ASSESSABLE, "reasons": ["比較用のSearch Consoleデータが無い（取得前の週）"]}
    if status == "no_target":
        return {**ev, "verdict": VERDICT_NOT_SET,
                "reasons": ["対象ページを特定できない（docs/current-initiatives.md に「対象URL」が無い）"]}
    if status == "not_started":
        return {**ev, "verdict": VERDICT_WAITING, "reasons": ["実装後のSearch Consoleデータがまだ無い"]}
    if status == "failed":
        return {**ev, "verdict": VERDICT_NOT_ASSESSABLE, "reasons": [f"Search Consoleの取得に失敗（{item.get('error', '')[:80]}）"]}

    windows = item["windows"]
    ev.update(windows=windows, urls=item["urls"])
    pre_t, post_t = _window_totals(item["pre"]["pages"]), _window_totals(item["post"]["pages"])
    ev["metrics"] = {"pre": pre_t, "post": post_t}
    ev["queries"] = query_composition(item["pre"]["queries"], item["post"]["queries"])
    overlaps = _overlaps(ini["name"], item["urls"], windows, all_items)
    ev["overlaps"] = overlaps

    if not site_daily:
        return {**ev, "verdict": VERDICT_NOT_ASSESSABLE, "reasons": ["比較対照（サイト全体の日別値）を取得できない"]}
    pre_series = _daily_series(item, site_daily, windows["pre"])
    post_series = _daily_series(item, site_daily, windows["post"])
    ev["control"] = {"pre": _control_totals(pre_series), "post": _control_totals(post_series)}

    min_days = review_cfg["min_post_days"]
    if windows["days"] < min_days:
        return {**ev, "verdict": VERDICT_WAITING,
                "reasons": [f"実装後のデータが{windows['days']}日分（{min_days}日分そろってから比べる）"]}

    low = low_data or {}
    tests = {
        "impressions": count_test(pre_series, post_series, "impressions", low.get("low_impressions", 0)),
        "clicks": count_test(pre_series, post_series, "clicks", low.get("low_clicks", 0)),
        "ctr": ctr_test(pre_t, post_t, ev["control"]["pre"], ev["control"]["post"], low.get("low_clicks", 1)),
    }
    ev["tests"] = tests
    verdict, reasons = _verdict(primary, tests, review_cfg["significance_t"], ev["metrics"], ev["control"])
    if overlaps and verdict in (VERDICT_POSSIBLE, VERDICT_ADVERSE, VERDICT_MIXED):
        reasons.append(f"同じ期間に対象ページが重なる施策（{'・'.join(f'「{o}」' for o in overlaps)}）があり、どの施策と関係するかは切り分けられない")
    elif overlaps:
        reasons.append(f"対象ページが重なる施策（{'・'.join(f'「{o}」' for o in overlaps)}）が同じ期間にある")
    return {**ev, "verdict": verdict, "reasons": reasons}


def _message(name: str, ev: dict) -> str:
    """一覧用の1文。数値の変化と判定を分けて書く。"""
    windows = ev.get("windows")
    if not windows:
        return f"「{name}」: {ev['verdict']}（{'／'.join(ev['reasons'])}）"
    pre, post = ev["metrics"]["pre"], ev["metrics"]["post"]
    change = (f"表示回数 {pre['impressions']:,}→{post['impressions']:,}、クリック {pre['clicks']:,}→{post['clicks']:,}"
              f"（実装前後 各{windows['days']}日）")
    return f"「{name}」: 数値の変化＝{change}。判定＝{ev['verdict']}（{'／'.join(ev['reasons'])}）"


def analyze_initiatives(
    initiatives_md_path: Path | str,
    bundle: dict,
    pages_cfg: list[dict],
    thresholds_cfg: dict,
    weeks_available: int = 1,
) -> list[dict]:
    review_cfg = thresholds_cfg["initiative_review"]
    data = bundle.get("initiatives_gsc") or {}
    items = data.get("items") or {}
    site_daily = data.get("site_daily") or {}
    findings = []
    for ini in parse_initiatives_md(initiatives_md_path):
        name = ini["name"]
        ev = evaluate(ini, items.get(name) if data else None, site_daily, items, review_cfg, thresholds_cfg.get("low_data"))
        ev["gsc_end"] = data.get("gsc_end") or bundle["period"]["cur_end"]
        verdict = ev["verdict"]
        findings.append(make_finding(
            id=f"initiative-{_slug(name)}",
            category="initiative",
            severity="info",  # 比較対照と異なる変化でも原因は確定していないため、警告にしない
            confidence="Not assessable" if verdict in (VERDICT_NOT_ASSESSABLE, VERDICT_NOT_SET, VERDICT_OTHER_METHOD,
                                                       VERDICT_WAITING) else "Medium",
            message=_message(name, ev),
            recommendation=ev.pop("recommendation", None) or _RECOMMENDATIONS[verdict],
            judgment=verdict,
            evidence=ev,
            source={"initiative": name},
        ))
    return findings
