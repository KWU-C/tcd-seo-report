"""SEO/AIO・コンテンツ評価中心のレポート構成（2026-11-23 再設計）の評価・要約ロジック。

入力は analysis.json の Finding と、view_model が組み立てた既存の部品（重点キーワード表・問い合わせ・アクセス）だけ。
データ取得・分析（analysis/）の判定は変えず、表示の単位を「コンテンツ／ページ」に組み替える。

方針（ユーザー指示 2026-11-23、承認時の修正指示を含む）:
- 基本単位はページ。キーワードは「どのページが表示されたか」と合わせて見る。地域LP全体の検索実績は別に示す
- 事実・評価・確認すべき論点・次の対応を分ける。未確認の原因は断定せず、「確認すべき論点」として書く
- 前週比の順位変動（±2.0位）は「前週比の順位変動」で、施策の効果判定（④、実装前後28日・比較対照）とは別
- 表示100回以上・クリック0は「クリック未獲得・要確認」。直ちにコンテンツの問題とは判定しない
- 表示50回未満は「評価材料不足」。順位・クリックの変化は評価しないが、改善候補からは外さない
- 1週の変化だけで改修を決めない。少数データ・施策の重複がある場合は因果関係を書かない
- AIO: 引用ドメインと推薦企業を混同しない。競合の推薦は未取得。引用元の分類は機械判定
- 施策: 指標別（表示回数・クリック数・CTR）に対象と比較対照の変化を書く。増加率が比較対照を下回っただけで
  「減少」と書かない。表示上まとめた施策も、施策ID・評価結果は施策ごとに保持する
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

_INTENT_LABELS = {
    "conversion": "依頼先・サービスを検討する",
    "knowledge": "用語・考え方を調べる",
    "regional": "地域で依頼先を探す",
    "comparison": "依頼先を比較・選定する",
}

# ページ評価の区分（表示順）
STATUS_UP = "順位上昇（前週比）"
STATUS_DOWN = "順位低下（前週比）"
STATUS_NO_CLICK = "クリック未獲得・要確認"
STATUS_FLAT_LOW = "2ページ目以降で横ばい"
STATUS_STABLE = "順位安定"
STATUS_INSUFFICIENT = "評価材料不足"
STATUS_ORDER = [STATUS_DOWN, STATUS_NO_CLICK, STATUS_FLAT_LOW, STATUS_UP, STATUS_STABLE, STATUS_INSUFFICIENT]
STATUS_STATE = {STATUS_UP: "good", STATUS_DOWN: "warning", STATUS_NO_CLICK: "warning", STATUS_FLAT_LOW: "neutral",
                STATUS_STABLE: "neutral", STATUS_INSUFFICIENT: "neutral"}
# 改善候補（①「確認が必要なコンテンツ」）に入れる区分
_CANDIDATE_STATUSES = (STATUS_DOWN, STATUS_NO_CLICK, STATUS_FLAT_LOW)
STATUS_RULES = (
    "区分の基準: 順位上昇／順位低下（前週比）＝平均掲載順位が前週から2.0位以上変化（1週分の比較。施策の効果判定ではない）。"
    "クリック未獲得・要確認＝表示100回以上でクリック0（原因は未確認で、コンテンツの問題とは判定していない）。"
    "2ページ目以降で横ばい＝順位の変化が2.0位未満で平均10位より下。順位安定＝1ページ目で変化が2.0位未満。"
    "評価材料不足＝今週か前週の表示が50回未満（変化は評価しないが、改善候補からは外さない）。"
)

# 施策の区分（ユーザー指示: 評価可能／評価保留／要調査）。旧文言（2026-11-23 より前の analysis.json）も受ける
VERDICT_POSSIBLE = "比較対照を上回る変化（原因未確定）"
VERDICT_ADVERSE = "比較対照を下回る変化（原因未確定）"
VERDICT_MIXED = "指標で比較対照との差の向きが異なる（原因未確定）"
_LEGACY_VERDICTS = {"比較対照と異なる増加（原因未確定）": VERDICT_POSSIBLE,
                    "比較対照と異なる減少（原因未確定）": VERDICT_ADVERSE,
                    "指標で方向が異なる（原因未確定）": VERDICT_MIXED}
INITIATIVE_CLASS = {
    VERDICT_POSSIBLE: "評価可能",
    "数値の変化のみ": "評価可能",
    VERDICT_ADVERSE: "要調査",
    VERDICT_MIXED: "要調査",
    "効果確認待ち": "評価保留",
    "評価不能": "評価保留",
    "検索流入の前後比較の対象外": "評価保留",
    "評価対象未設定": "評価保留",
}
INITIATIVE_CLASS_STATE = {"評価可能": "good", "要調査": "warning", "評価保留": "neutral"}
_INITIATIVE_CLASS_ORDER = {"要調査": 0, "評価可能": 1, "評価保留": 2}
_METRIC_LABELS = {"impressions": "表示回数", "clicks": "クリック数", "ctr": "CTR"}

_REGION_WORDS = {"tokyo": "東京", "osaka": "大阪"}


# ── 共通 ──────────────────────────────────────────────────────

def _pos(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def _ctr(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _short_url(url: str) -> str:
    return url.replace("https://tcd.jp", "") or "/"


def short_title(title: str | None) -> str:
    """本文で使う短いページ名。「｜」以降（サイト名・補足）を落とす。"""
    if not title:
        return "—"
    return title.split("｜")[0].strip()


def normalize_verdict(verdict: str | None) -> str | None:
    return _LEGACY_VERDICTS.get(verdict, verdict)


def _page_index(page_findings: list[dict]) -> dict[str, dict]:
    """page_id・URL の両方から、ページの Finding を引けるようにする。"""
    index = {}
    for f in page_findings:
        ev = f.get("evidence") or {}
        for key in (ev.get("page_id"), ev.get("url")):
            if key:
                index[key] = f
    return index


def _page_name(key: str, index: dict[str, dict]) -> str:
    f = index.get(key)
    if f:
        return short_title((f.get("evidence") or {}).get("title")) or key
    if key in ("https://tcd.jp/", "https://tcd.jp"):
        return "トップページ"
    return _short_url(key) if key.startswith("https://tcd.jp") else key


def _page_path(key: str, index: dict[str, dict]) -> str:
    f = index.get(key)
    url = (f.get("evidence") or {}).get("url") if f else key
    return _short_url(url) if url and url.startswith("https://tcd.jp") else ""


def _label_with_path(name: str, path: str) -> str:
    return f"{name}（{path}）" if path and path != "/" and not name.startswith("/") else name


# ── ページ単位の評価（③の本体・①の材料） ───────────────────────────

def _initiatives_by_url(initiative_findings: list[dict]) -> dict[str, list[dict]]:
    by_url: dict[str, list[dict]] = {}
    for f in initiative_findings:
        ev = f.get("evidence") or {}
        for url in ev.get("urls") or []:
            by_url.setdefault(url, []).append({"name": ev.get("name"), "implemented_at": ev.get("implemented_at")})
    return by_url


def _keywords_by_page(market_rows: list[dict], market_findings: list[dict]) -> dict[str, list[str]]:
    """重点キーワードで最も多く表示されたページ → キーワード名。"""
    label_by_query = {r["query"]: r["market_label"] for r in market_rows}
    by_page: dict[str, list[str]] = {}
    for f in market_findings:
        ev = f.get("evidence") or {}
        breakdown = ev.get("page_breakdown") or []
        if breakdown and ev.get("query") in label_by_query:
            by_page.setdefault(breakdown[0]["page_id_or_url"], []).append(label_by_query[ev["query"]])
    return by_page


def _evaluate_page(f: dict, thresholds_cfg: dict, initiatives: list[dict], keywords: list[str]) -> dict:
    ev = f.get("evidence") or {}
    cur, prv = ev.get("current") or {}, ev.get("comparison") or {}
    low_imp = thresholds_cfg["low_data"]["low_impressions"]
    meaningful = thresholds_cfg["low_data"]["position_change_meaningful"]
    objective = (ev.get("objective") or ["knowledge"])[0]
    imp, clicks, pos = cur.get("impressions") or 0, cur.get("clicks") or 0, cur.get("position")
    p_imp, p_clicks, p_pos = prv.get("impressions") or 0, prv.get("clicks") or 0, prv.get("position")
    delta = None if pos is None or p_pos is None else p_pos - pos  # 正＝上位へ
    top_queries = sorted(ev.get("top_queries") or [], key=lambda q: -(q.get("impressions") or 0))
    top_query = top_queries[0]["query"] if top_queries else None
    tq = f"「{top_query}」" if top_query else ""

    facts = f"{_pos(p_pos)}→{_pos(pos)}位・表示{p_imp:,}→{imp:,}・クリック{p_clicks}→{clicks}"
    points: list[str] = []  # 確認すべき論点（未確認の原因を断定しない）
    if imp < low_imp or p_imp < low_imp:
        status = STATUS_INSUFFICIENT
        evaluation = f"表示回数が{low_imp}回未満の週があり、順位・クリックの変化は評価しない"
        if objective in ("conversion", "regional", "comparison"):
            evaluation = f"依頼先を探す検索の受け皿となるページだが、検索での表示が少ない（今週{imp:,}回）"
            points = ["このページで表示されたい検索クエリが決まっているか、そのクエリで表示されているか",
                      "表示が少ないのは検索需要の少なさによるものか、ページが検索結果に出ていないためか"]
            action = "対象とする検索クエリと、そのクエリでの表示状況を確認する（改善候補として扱う）"
        else:
            action = "データの蓄積を待つ（改善候補からは外さない）"
    elif delta is not None and delta >= meaningful:
        status = STATUS_UP
        evaluation = f"平均掲載順位が{_pos(p_pos)}→{_pos(pos)}位（前週比・1週の順位変動）"
        if clicks > p_clicks:
            evaluation += f"。クリックも{p_clicks}→{clicks}"
        action = "上昇が翌週も続くか確認する"
    elif delta is not None and delta <= -meaningful:
        status = STATUS_DOWN
        evaluation = f"平均掲載順位が{_pos(p_pos)}→{_pos(pos)}位（前週比・1週の順位変動）"
        points = [f"主要クエリ{tq}などで順位が下がったのか、表示されたクエリの構成が変わったのか（ページ×クエリで確認）",
                  "1週の変化か、翌週も続くか"]
        action = "ページ×クエリで前週と今週を比べる。1週の変化だけで改修は決めない"
    elif clicks == 0 and imp >= 100:
        status = STATUS_NO_CLICK
        if (pos or 99) > 10:
            evaluation = f"表示{imp:,}回でクリック0（平均{_pos(pos)}位）。原因は未確認で、コンテンツの問題とは判定していない"
            points = [f"平均{_pos(pos)}位（2ページ目以降）での表示が中心で、掲載位置だけでクリックが無いことを説明できるか",
                      f"主要クエリ{tq}で上位に出ているページが扱う論点と、このページの内容が合っているか（未比較）"]
            action = f"主要クエリ{tq}の検索結果（上位10件）を見て、内容の比較が必要か判断する"
        else:
            evaluation = f"1ページ目（平均{_pos(pos)}位）で表示{imp:,}回あるがクリック0。原因は未確認"
            points = [f"検索結果に表示されるタイトル・説明文が、主要クエリ{tq}と合っているか",
                      "表示が特定の日・クエリに偏っていないか"]
            action = "検索結果での見え方（タイトル・説明文）と、表示されたクエリの内訳を確認する"
    elif (pos or 99) > 10:
        status = STATUS_FLAT_LOW
        evaluation = f"順位の変化は2.0位未満（平均{_pos(pos)}位）で、2ページ目以降にとどまる"
        points = [f"主要クエリ{tq}で上位に出ているページと比べ、このページで扱っていない論点があるか（未比較）"]
        action = "主要クエリの上位ページと内容を比べるか判断する"
    else:
        status = STATUS_STABLE
        evaluation = f"1ページ目（平均{_pos(pos)}位）で順位安定。クリック{clicks}"
        action = "継続観測"

    related = [i for i in initiatives if i.get("implemented_at")]
    notes = []
    if related:
        names = "・".join(f"「{i['name']}」" for i in related)
        notes.append(f"施策{names}の対象ページ。前週比の順位変動は施策の効果判定ではない（効果の確認は④）")
    return {
        "id": f["id"], "title": ev.get("title") or ev.get("page_id"), "short_title": short_title(ev.get("title")),
        "page_id": ev.get("page_id"), "url": ev.get("url"), "path": _short_url(ev.get("url") or ""),
        "objective": objective, "intent_label": _INTENT_LABELS.get(objective, "—"),
        "status": status, "state": STATUS_STATE[status],
        "position": pos, "prev_position": p_pos, "position_delta": delta,
        "impressions": imp, "prev_impressions": p_imp, "clicks": clicks, "prev_clicks": p_clicks,
        "ctr": cur.get("ctr"), "prev_ctr": prv.get("ctr"),
        "top_query": top_query, "top_queries": [q["query"] for q in top_queries[:3]],
        "facts": facts, "evaluation": evaluation, "points": points, "next_action": action,
        "initiatives": [i["name"] for i in related], "keywords": keywords, "notes": notes,
    }


def _candidate_priority(p: dict) -> tuple:
    objective_rank = {"regional": 0, "conversion": 1, "comparison": 2, "knowledge": 3}.get(p["objective"], 9)
    status_rank = {STATUS_DOWN: 0, STATUS_NO_CLICK: 1, STATUS_FLAT_LOW: 2}.get(p["status"], 9)
    return (status_rank, 0 if p["keywords"] else 1, objective_rank, -p["impressions"])


def build_page_evaluations(page_findings: list[dict], initiative_findings: list[dict], market_rows: list[dict],
                           market_findings: list[dict], thresholds_cfg: dict) -> list[dict]:
    init_by_url = _initiatives_by_url(initiative_findings)
    kw_by_page = _keywords_by_page(market_rows, market_findings)
    out = []
    for f in page_findings:
        ev = f.get("evidence") or {}
        keywords = kw_by_page.get(ev.get("page_id"), []) + kw_by_page.get(ev.get("url"), [])
        out.append(_evaluate_page(f, thresholds_cfg, init_by_url.get(ev.get("url"), []), keywords))
    out.sort(key=lambda p: (STATUS_ORDER.index(p["status"]), *_candidate_priority(p)[1:]))
    return out


# ── キーワード → 表示されたページ（③の冒頭） ─────────────────────────

def _expected_pages(row: dict, page_findings: list[dict]) -> list[str]:
    """地域の商談キーワードは、その地域のLP（objective: regional・タイトルに地域名）を受け皿として期待する。"""
    region = _REGION_WORDS.get(row.get("region") or "")
    if row["tier"] != "deal" or not region:
        return []
    return [(f.get("evidence") or {}).get("page_id") for f in page_findings
            if "regional" in ((f.get("evidence") or {}).get("objective") or [])
            and region in ((f.get("evidence") or {}).get("title") or "")]


def _page_rows(ev: dict, index: dict) -> tuple[list[dict] | None, list[dict]]:
    """今週のページ別（前週の値を付ける）と、前週は表示されたが今週は上位にないページ。"""
    cur = ev.get("page_breakdown")
    if cur is None:
        return None, []
    prv = {b["page_id_or_url"]: b for b in ev.get("page_breakdown_comparison") or []}
    pages = []
    for b in cur:
        p = prv.get(b["page_id_or_url"]) or {}
        pages.append({"name": _page_name(b["page_id_or_url"], index), "key": b["page_id_or_url"],
                      "path": _page_path(b["page_id_or_url"], index),
                      "impressions": b["impressions"], "position": b["position"], "clicks": b.get("clicks"),
                      "prev_impressions": p.get("impressions"), "prev_position": p.get("position")})
    keys = {b["page_id_or_url"] for b in cur}
    gone = [{"name": _page_name(k, index), "path": _page_path(k, index), "key": k, "prev_impressions": b["impressions"], "prev_position": b["position"]}
            for k, b in prv.items() if k not in keys and b["impressions"] >= 5][:3]
    return pages, gone


def _page_move(p: dict) -> str:
    prev = (f"前週{p['prev_impressions']:,}回・{_pos(p['prev_position'])}位" if p.get("prev_impressions") is not None
            else "前週は表示なし")
    return f"{_label_with_path(p['name'], p.get('path'))}: {prev} → 今週{p['impressions']:,}回・{_pos(p['position'])}位"


def _keyword_move(row: dict, meaningful: float) -> tuple[str | None, str]:
    """前週比の順位変動（ページ評価と同じ ±meaningful 位）。最低表示に満たない参考値は判定しない。"""
    pos, prev, label = row.get("position"), row.get("prev_position"), row.get("delta_label") or ""
    if pos is None or prev is None:
        return None, label
    delta = prev - pos  # 正＝上位へ
    sign = "+" if delta > 0 else "−" if delta < 0 else "±"
    text = f"{sign}{abs(delta):.1f}位"
    if "参考値" in label:
        return None, f"{text}（参考値）"
    if delta >= meaningful:
        return "順位上昇", f"{text} 順位上昇"
    if delta <= -meaningful:
        return "順位低下", f"{text} 順位低下"
    return None, f"{text}（変化{meaningful:.1f}位未満）"


def build_keyword_pages(market_rows: list[dict], market_findings: list[dict], page_findings: list[dict],
                        meaningful: float = 2.0) -> list[dict]:
    index = _page_index(page_findings)
    ev_by_query = {(f.get("evidence") or {}).get("query"): (f.get("evidence") or {}) for f in market_findings}
    out = []
    for row in market_rows:
        ev = ev_by_query.get(row["query"]) or {}
        row = {**row, "region": ev.get("region")}
        pages, gone = _page_rows(ev, index)
        expected = _expected_pages(row, page_findings)
        evaluation, investigate, movement = None, None, []
        if pages is None:
            evaluation = "ページ別の内訳なし（この週の分析データに未収録）"
        elif not pages:
            evaluation = "TCDのページは表示されていない"
        elif expected and pages[0]["key"] not in expected:
            exp = next((p for p in pages if p["key"] in expected), None)
            exp_label = (f"{exp['name']}は{exp['impressions']:,}回・{_pos(exp['position'])}位"
                         if exp else "地域LPは上位に表示されていない")
            evaluation = (f"{pages[0]['name']}の表示（{pages[0]['impressions']:,}回・{_pos(pages[0]['position'])}位）が"
                          f"地域LPより多い（{exp_label}）")
            investigate = (f"{pages[0]['name']}の表示が地域LPより多い理由"
                           "（どのクエリで、どちらのページが表示されているか）を調べる。理由は未確認")
        elif expected:
            evaluation = f"地域LP（{pages[0]['name']}）の表示が最も多い"
        move, delta_text = _keyword_move(row, meaningful)
        if pages and row["tier"] == "deal" and move:
            # 順位変動の要因はページ×クエリで確認する（ここではキーワードに一致したクエリのページ別だけを示す）
            movement = [_page_move(p) for p in pages[:3]] + [
                f"{_label_with_path(g['name'], g.get('path'))}: 前週{g['prev_impressions']:,}回・{_pos(g['prev_position'])}位 → 今週は上位に表示なし" for g in gone]
        intent = "地域で依頼先を探す" if row["tier"] == "deal" else "用語・概念を調べる（推定）"
        out.append({**row, "delta_label": delta_text, "move": move, "pages": pages or [], "gone_pages": gone, "page_evaluation": evaluation,
                    "investigate": investigate, "movement": movement, "intent_label": intent,
                    "expected_pages": expected})
    return out


def build_regional_lp_summary(pages: list[dict]) -> list[dict]:
    """地域LPの検索実績（そのページに表示された全クエリの合計）。重点キーワード単位の結果とは別に示す。"""
    return [p for p in pages if p["objective"] == "regional"]


# ── ② AIO評価・競合比較 ─────────────────────────────────────────

def _domain_type(domain: str) -> str:
    """ドメイン名だけによる機械判定。サイトの内容は確認していない。"""
    if domain.endswith(".ac.jp") or domain.endswith(".ed.jp"):
        return "教育機関"
    if domain.endswith(".go.jp") or domain.endswith(".lg.jp"):
        return "公的機関"
    if "wikipedia.org" in domain:
        return "百科事典"
    return "企業・メディア等"


# ── AIOの前回差分（推薦・引用・生成の あり⇄なし） ─────────────────────────

_JST = timezone(timedelta(hours=9))
AIO_CHANGE_PRIORITY = {("recommended", "lost"): 0, ("cited", "lost"): 1, ("recommended", "gained"): 2,
                       ("cited", "gained"): 3, ("generated", "lost"): 4, ("generated", "gained"): 5}
_AIO_ITEM_LABELS = {"recommended": "TCDの会社推薦", "cited": "TCDの引用", "generated": "AIO生成"}


def _jst(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(_JST)
    except ValueError:
        return ts
    return f"{t.month}/{t.day} {t:%H:%M}"


def _aio_state(snap: dict | None, item: str) -> tuple[bool | None, str]:
    """観測1回分の、項目ごとの値（True/False/None＝判定できない）と表示ラベル。値は観測データのまま使う。"""
    if not snap or snap.get("acquisition_status") != "success":
        return None, "取得失敗" if snap else "観測なし"
    gen = snap.get("aio_generated")
    if item == "generated":
        return (bool(gen), "あり" if gen else "なし") if gen is not None else (None, "判定不可")
    if not gen:
        return False, "なし（AIO未生成）"
    if item == "cited":
        if snap.get("citation_status") == "unavailable" or snap.get("tcd_cited") is None:
            return None, "判定不可"
        return bool(snap["tcd_cited"]), "あり" if snap["tcd_cited"] else "なし"
    status = snap.get("recommendation_status")
    if status == "recommended":
        return True, "あり"
    if status == "not_recommended":
        return False, "なし"
    if status == "not_applicable":
        return None, "対象外"
    return None, "判定不可"


def build_aio_changes(market_findings: list[dict], keyword_pages: list[dict]) -> list[dict]:
    """定点観測キーワードの前回→今回の照合。前回の観測が無い・判定不可の項目は「前回比較不可」とし、推測で補わない。
    AIO未生成のときの引用・推薦は「なし（AIO未生成）」として扱うが、生成の変化と混同しないよう項目を分けて記録する。"""
    ev_by_query = {(f.get("evidence") or {}).get("query"): (f.get("evidence") or {}) for f in market_findings}
    out = []
    for r in keyword_pages:
        if r["tier"] == "reference":
            continue
        ev = ev_by_query.get(r["query"]) or {}
        prev, cur = ev.get("aio_previous"), ev.get("aio_current")
        items = {}
        for item in ("generated", "cited", "recommended"):
            if item == "recommended" and r["tier"] != "deal":
                items[item] = {"prev": "対象外", "cur": "対象外", "change": "対象外", "direction": None}
                continue
            pv, pl = _aio_state(prev, item)
            cv, cl = _aio_state(cur, item)
            if prev is None:
                change, direction = "前回比較不可（前回の観測データなし）", None
            elif pv is None or cv is None:
                change, direction = "前回比較不可", None
            elif pv and not cv:
                change, direction = "あり→なし", "lost"
            elif cv and not pv:
                change, direction = "なし→あり", "gained"
            else:
                change, direction = "変化なし", None
            items[item] = {"prev": pl, "cur": cl, "change": change, "direction": direction}
        out.append({"query": r["query"], "market_label": r["market_label"], "short_label": r["short_label"],
                    "tier": r["tier"], "previous_observed": _jst((prev or {}).get("observed_at")),
                    "current_observed": _jst((cur or {}).get("observed_at")), "items": items,
                    "previous_available": prev is not None})
    return out


def _md_date(d: str) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}"


def aio_daily_note(daily: dict | None, query: str, current_observed_at: str | None) -> str | None:
    """日次AIO監視の同じクエリの観測（今回の週次観測日まで）の要約。週次の判定は変えず、継続性の確認材料として添える。"""
    rows = [r for r in ((daily or {}).get("by_query") or {}).get(query) or [] if r.get("status", "").startswith("OK")]
    if not rows:
        return None
    rows.sort(key=lambda r: r["date"])
    n = len(rows)
    rec = sum(1 for r in rows if r.get("recommendation_status") == "recommended")
    cited = sum(1 for r in rows if r.get("tcd_cited") is True)
    span = f"{_md_date(rows[0]['date'])}〜{_md_date(rows[-1]['date'])}の{n}回"
    if rec == n and cited == n:
        text = f"日次のAIO監視では、{span}の観測すべてで推薦あり・引用あり"
    else:
        text = f"日次のAIO監視では、{span}の観測のうち推薦あり{rec}回・引用あり{cited}回"
    if current_observed_at:
        cur = (datetime.strptime(current_observed_at, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=9)).date()
        nxt = date.fromisoformat(rows[-1]["date"]) + timedelta(days=1)
        if nxt <= cur:
            text += f"。{_md_date(nxt.isoformat())}〜{_md_date(cur.isoformat())}の日次データはない"
    return text


def aio_change_events(changes: list[dict]) -> list[dict]:
    """あり⇄なしの変化だけを、推薦・引用の消失を先頭にして並べる。"""
    events = []
    for c in changes:
        for item, v in c["items"].items():
            if v["direction"]:
                events.append({"query": c["query"], "market_label": c["market_label"], "item": item,
                               "label": _AIO_ITEM_LABELS[item], "direction": v["direction"], "change": v["change"],
                               "previous_observed": c["previous_observed"], "current_observed": c["current_observed"],
                               "priority": AIO_CHANGE_PRIORITY[(item, v["direction"])]})
    events.sort(key=lambda e: e["priority"])
    return events


def aio_event_lines(events: list[dict]) -> list[dict]:
    """同じキーワードの変化（推薦と引用の消失など）は1行にまとめる。各行の priority は最も高いもの。"""
    by_query: dict[str, list[dict]] = {}
    for e in events:
        by_query.setdefault(e["query"], []).append(e)
    lines = []
    for q, es in by_query.items():
        parts = "、".join(f"{e['label']}が{e['change']}" for e in es)
        e0 = es[0]
        text = (f"「{e0['market_label']}」のAIOで{parts}（前回{e0['previous_observed']}→今回{e0['current_observed']}の観測）。"
                "各回1回ずつの観測の比較で、継続するかは次回以降の観測で確認する")
        lines.append({"text": text, "priority": min(e["priority"] for e in es), "query": q,
                      "lost": any(e["direction"] == "lost" and e["item"] in ("recommended", "cited") for e in es),
                      "events": es})
    lines.sort(key=lambda x: x["priority"])
    return lines


def _citation_change(summary: dict | None, findings: list[dict], domains: list[str],
                     previous: dict | None = None) -> dict:
    """引用ドメインの母数（今回の件数）と、前回の観測からの増減を分けて持つ。
    母数は今回の引用ドメイン一覧、増減は analysis の competitor_summary（前回の観測との差集合）。"""
    summary = summary or {}
    periods = {(f.get("evidence") or {}).get("domain"): (f.get("evidence") or {}).get("periods_cited") or []
               for f in findings if (f.get("evidence") or {}).get("direction") == "new"}
    new = list(summary.get("new_domains") or [])
    return {
        "current": len(domains),
        # 前回の引用元がGoogle内部リンクだけでドメインを特定できなかった場合は、件数を比べない（「前回0件」と誤読させない）
        "comparison_available": bool(summary.get("comparison_available")) and not (
            previous and previous.get("cited_source_count") and not previous.get("resolvable_domain_count")),
        "previous_unresolved": bool(previous and previous.get("cited_source_count") and not previous.get("resolvable_domain_count")),
        "previous": summary.get("previous_count"),
        "new": new,
        # 前回の観測には無かったが、直近4回の観測のどこかで引用されたことがあるもの（再出現）
        "new_seen_before": [d for d in new if len(periods.get(d) or []) >= 2],
        "dropped": list(summary.get("dropped_domains") or []),
    }


def citation_count_label(ch: dict) -> str:
    """例: 現在5件（うち前回なし2件）／前回から消失1件（前回4件）"""
    if ch.get("previous_unresolved"):
        return f"現在{ch['current']}件（前回比較不可: 前回の引用元はドメインを特定できず）"
    if not ch["comparison_available"]:
        return f"現在{ch['current']}件（前回比較不可）"
    head = f"現在{ch['current']}件（うち前回なし{len(ch['new'])}件）" if ch["new"] else f"現在{ch['current']}件（前回なしは0件）"
    prev = f"（前回{ch['previous']}件）" if ch.get("previous") is not None else ""
    return f"{head}／前回から消失{len(ch['dropped'])}件{prev}"


def build_aio_evaluation(keyword_pages: list[dict], competitor_findings: list[dict],
                         market_findings: list[dict] | None = None, aio_daily: dict | None = None) -> dict:
    summary_by_query = {(f.get("evidence") or {}).get("query"): (f.get("evidence") or {}).get("competitor_summary")
                        for f in market_findings or []}
    previous_by_query = {(f.get("evidence") or {}).get("query"): (f.get("evidence") or {}).get("aio_previous")
                         for f in market_findings or []}
    changes = build_aio_changes(market_findings or [], keyword_pages)
    lines = aio_event_lines(aio_change_events(changes))
    current_by_query = {(f.get("evidence") or {}).get("query"): ((f.get("evidence") or {}).get("aio_current") or {}).get("observed_at")
                        for f in market_findings or []}
    daily_notes = {}
    for x in lines:
        note = aio_daily_note(aio_daily, x["query"], current_by_query.get(x["query"]))
        if note:
            word = "低下" if x["lost"] else "向上" if all(e["direction"] == "gained" for e in x["events"]) else "変化"
            daily_notes[x["query"]] = note + f"。今回の変化は週次の観測1回で、恒常的な{word}とは判断しない"
    findings_by_query: dict[str, list[dict]] = {}
    for f in competitor_findings:
        findings_by_query.setdefault((f.get("evidence") or {}).get("query"), []).append(f)

    domain_queries: dict[str, list[str]] = {}
    rows = []
    for r in keyword_pages:
        if r["tier"] == "reference":
            continue
        domains = r.get("aio_domains") or []
        for d in domains:
            domain_queries.setdefault(d, []).append(r["short_label"])
        types: dict[str, int] = {}
        for d in domains:
            types[_domain_type(d)] = types.get(_domain_type(d), 0) + 1
        ch = _citation_change(summary_by_query.get(r["query"]), findings_by_query.get(r["query"], []), domains,
                              previous_by_query.get(r["query"]))
        top = r["pages"][0] if r.get("pages") else None
        facts, points = [], []
        if r["aio_generated_label"] == "あり":
            facts.append(f"AIO生成あり。TCDの引用{r['tcd_cited_label']}・TCDの推薦{r['tcd_recommended_label']}")
            facts.append(f"引用ドメイン: {citation_count_label(ch)}")
            facts.append(f"今回の引用ドメインの種類（ドメイン名による機械判定）: "
                         f"{'、'.join(f'{k}{v}件' for k, v in types.items()) or '—'}")
        else:
            facts.append(f"AIO: {r['aio_generated_label']}（TCDの引用 {r['tcd_cited_label']}）")
        if ch["new"] and ch["comparison_available"]:
            seen = ch["new_seen_before"]
            again = ("いずれも直近4回の観測で以前にも引用あり" if seen and len(seen) == len(ch["new"])
                     else f"うち{'、'.join(seen)}は直近4回の観測で以前にも引用あり" if seen else "")
            facts.append(f"前回なし（今回引用）: {'、'.join(ch['new'])}" + (f"。{again}" if again else ""))
        if ch["dropped"] and ch["comparison_available"]:
            facts.append(f"前回から消失: {'、'.join(ch['dropped'])}")
        if r["tier"] == "deal":
            facts.append("競合の推薦（AIOが依頼先として挙げた会社）: 未取得")
        if top:
            facts.append(f"TCDで最も表示されたページ: {top['name']}（{_pos(top['position'])}位・表示{top['impressions']:,}）")

        if not r.get("tcd_cited"):
            if r["tier"] == "deal":
                points.append("引用されたページが、所在地・対応領域・実績をどのように示しているか（引用元ページの内容は未確認）")
                if top and (top["position"] or 0) <= 10:
                    points.append(f"TCDのページは1ページ目（平均{_pos(top['position'])}位）に表示されているが引用されていない。"
                                  "順位以外にどのような違いがあるか")
            elif types.get("教育機関") or types.get("公的機関") or types.get("百科事典"):
                points.append("教育機関・公的機関・百科事典と機械判定されたドメインが引用されている。"
                              "定義・一般論の出典が選ばれているのか、TCDの実務の事例・方法論が引用の対象になりうるか")
            if top and (top["position"] or 0) > 10:
                points.append(f"TCDのページは平均{_pos(top['position'])}位（2ページ目以降）。"
                              "検索順位が引用の有無に関係しているか")
        next_action = ("引用元の上位3ページと、TCDのページの実績・事例・方法論の記述を人手で比べる"
                       if not r.get("tcd_cited") else "引用されたページと引用箇所を確認する")
        rows.append({
            "market_label": r["market_label"], "short_label": r["short_label"], "tier": r["tier"],
            "aio_generated_label": r["aio_generated_label"], "tcd_cited_label": r["tcd_cited_label"],
            "tcd_cited": r.get("tcd_cited"), "tcd_recommended_label": r["tcd_recommended_label"],
            "competitor_recommendation_label": "未取得" if r["tier"] == "deal" else "—",
            "citation_change": ch, "citation_count_label": citation_count_label(ch), "domains": domains, "domain_types": types, "top_page": top,
            "facts": facts, "points": points, "next_action": next_action,
        })

    cross = {d: qs for d, qs in domain_queries.items() if len(qs) >= 2}
    cited = [r["short_label"] for r in rows if r["tcd_cited"]]
    summary = (f"TCDの引用あり: {'・'.join(cited)}" if cited
               else f"今回の観測では定点観測{len(rows)}キーワードでTCDの引用なし")
    lost = [x for x in lines if x["lost"]]
    if lost:
        summary = "前回からの消失あり（" + "／".join(
            "・".join(f"{e['label']}{e['change']}" for e in x["events"]) + f"：{x['events'][0]['market_label']}" for x in lost) + "）。" + summary
    recommended = [r for r in rows if r["tier"] == "deal"]
    if recommended:
        summary += ("。TCDの推薦（会社を探す検索）: "
                    + "、".join(f"{r['short_label']}{r['tcd_recommended_label']}" for r in recommended)
                    + "。競合の推薦は未取得")
    notes = [
        "観測は週1回・各キーワード1回の検索結果。Google検索全体での引用状況ではない。",
        "引用ドメイン＝AIOの回答の出典として示されたサイト。推薦＝AIOの回答本文で依頼先として挙げられた会社。"
        "引用ドメインに企業のサイトがあっても、その会社が推薦されたことにはならない。",
        "競合の推薦状況は未取得（現行の取得処理はTCDの推薦の有無だけを判定する。抽出処理は今回追加していない）。",
        "引用ドメインの種類（教育機関・公的機関・百科事典・企業・メディア等）は、ドメイン名（.ac.jp・.go.jp など）による"
        "機械判定で、サイトの内容は確認していない。",
        "引用ドメインの件数は今回の観測の件数（母数）。「前回なし」「前回から消失」は、前回の観測（前週）との比較による増減で、母数とは別に数える。",
        "「確認する論点」は観測事実から立てた問いで、原因の推定ではない。",
        "前回との比較は、前週のレポート時の観測（各キーワード1回）と今回の観測の照合。前回の観測データが無い・判定不可の項目は"
        "「前回比較不可」とし、推測で補わない。TCDの会社推薦・TCDの引用・AIO生成は別の項目として判定する。"
        "1回の観測での変化を、恒常的な評価の低下・向上とは判断しない。",
    ]
    return {"rows": rows, "cross_domains": cross, "summary": summary, "notes": notes,
            "changes": changes, "change_lines": lines,
            "change_notes": [f"{next(c['market_label'] for c in changes if c['query'] == q)}: {t}" for q, t in daily_notes.items()]}


# ── ④ 実施施策と効果検証 ────────────────────────────────────────

def _pct(new, old) -> str:
    if old in (0, None) or new is None:
        return "—"
    return f"{(new - old) / old * 100:+.1f}%"


def _metric_lines(ev: dict, t_crit: float) -> list[dict]:
    """指標別（表示回数・クリック数・CTR）の、対象ページと比較対照の変化。比較対照との差は検定（t値）で表す。"""
    m, c, tests = ev.get("metrics"), ev.get("control"), ev.get("tests") or {}
    if not m:
        return []
    judged = ["ctr"] if ev.get("primary_metric") == "ctr" else ["impressions", "clicks"]
    out = []
    for key in ("impressions", "clicks", "ctr"):
        pre, post = m["pre"].get(key), m["post"].get(key)
        if key == "ctr":
            target = f"{_ctr(pre)}→{_ctr(post)}"
            control = f"{_ctr(c['pre'].get('ctr'))}→{_ctr(c['post'].get('ctr'))}" if c else "—"
        else:
            target = f"{pre:,}→{post:,}（{_pct(post, pre)}）"
            control = _pct(c["post"].get(key), c["pre"].get(key)) if c else "—"
        test = tests.get(key) or {}
        if not test:
            diff = "—"
        elif not test.get("assessable"):
            diff = "評価しない（件数が少ない）"
        elif test["t"] >= t_crit:
            diff = "比較対照を上回る"
        elif test["t"] <= -t_crit:
            diff = "比較対照を下回る"
            if (post or 0) > (pre or 0):
                diff += "（対象ページの値は増加）"
        else:
            diff = "差は日々の変動の範囲"
        out.append({"key": key, "label": _METRIC_LABELS[key], "target": target, "control": control,
                    "diff": diff, "judged": key in judged})
    return out


def _md(d: str) -> str:
    """2026-10-24 → 10/24"""
    return f"{int(d[5:7])}/{int(d[8:10])}"


def comparison_window(ev: dict) -> dict | None:
    """比較期間の表示。実装後の期間が実装日の翌日から始まらない（直近28日を使う）場合は、
    実装後の初期期間を含まないことを示す。"""
    w = ev.get("windows")
    if not w or not w.get("pre") or not w.get("post"):
        return None
    days = w.get("days")
    pre, post = w["pre"], w["post"]
    impl = ev.get("implemented_at")
    excluded = None
    if impl:
        first_after = date.fromisoformat(impl) + timedelta(days=1)
        post_start = date.fromisoformat(post["start"])
        if post_start > first_after:
            excluded = {"start": first_after.isoformat(), "end": (post_start - timedelta(days=1)).isoformat()}
    label = (f"実装前{days}日間（{_md(pre['start'])}〜{_md(pre['end'])}）と直近{days}日間（{_md(post['start'])}〜{_md(post['end'])}）の比較")
    note = (f"実装後の初期期間（{_md(excluded['start'])}〜{_md(excluded['end'])}）は含まない" if excluded
            else "実装日の翌日からの期間")
    if impl:
        note = f"実装日{_md(impl)}は除く。" + note
    return {"label": label, "note": note, "excluded": excluded, "days": days}


def _observed_text(lines: list[dict]) -> str:
    if not lines:
        return "—"
    return "／".join(f"{x['label']} {x['target']}・比較対照 {x['control']}・{x['diff']}"
                     + ("" if x["judged"] else "（参考）") for x in lines)


def build_initiative_summary(initiative_findings: list[dict], t_crit: float = 2.0) -> dict:
    """施策の要約表。対象ページ・実装日・判定・数値がすべて同じ施策は、表示上だけ1行にまとめる。
    施策ID・名前・判定は items に施策ごとに残す（Appendix の施策別詳細もそのまま）。"""
    groups: dict[tuple, list[dict]] = {}
    for f in initiative_findings:
        ev = f.get("evidence") or {}
        metrics_key = repr(ev.get("metrics")) + repr(ev.get("control"))
        key = (tuple(sorted(ev.get("urls") or [])) or (f["id"],), ev.get("implemented_at"),
               normalize_verdict(f.get("judgment")), metrics_key)
        groups.setdefault(key, []).append(f)
    rows = []
    for (_urls, implemented_at, verdict, _m), items in groups.items():
        ev = items[0].get("evidence") or {}
        cls = INITIATIVE_CLASS.get(verdict, "評価保留")
        names = [(i.get("evidence") or {}).get("name") for i in items]
        lines = _metric_lines(ev, t_crit)
        reasons = ev.get("reasons") or []
        overlap_names = [o for o in (ev.get("overlaps") or []) if o not in names]
        next_action = items[0].get("recommendation")
        q = ev.get("queries") or {}
        if cls == "要調査" and q.get("decreased"):
            d = q["decreased"][0]
            next_action = (f"表示が減ったクエリ「{d['query']}」（{d['pre']:,}→{d['post']:,}）の順位・表示URLを確認する"
                           "（施策との関係は判断していない）")
        elif cls == "評価保留" and ev.get("evaluation_method") == "index":
            next_action = "対象URLを登録し、URL検査（Googleが選んだ正規URL）とインデックス登録レポートで確認する"
        hypotheses = list(dict.fromkeys((i.get("evidence") or {}).get("hypothesis") for i in items
                                        if (i.get("evidence") or {}).get("hypothesis")))
        rows.append({
            "items": [{"id": i["id"], "name": (i.get("evidence") or {}).get("name"),
                       "verdict": normalize_verdict(i.get("judgment"))} for i in items],
            "names": names, "merged": len(names) > 1, "implemented_at": implemented_at,
            "hypothesis": "／".join(hypotheses) or None,
            "verdict": verdict, "verdict_short": (verdict or "").replace("（原因未確定）", ""), "target": ev.get("target"),
            "class": cls, "state": INITIATIVE_CLASS_STATE[cls],
            "metric_lines": lines, "observed": _observed_text(lines), "window": comparison_window(ev),
            "reason": None if lines else (reasons[0] if reasons else None),
            "overlaps": overlap_names, "next_action": next_action, "url_count": len(ev.get("urls") or []),
        })
    rows.sort(key=lambda r: (_INITIATIVE_CLASS_ORDER[r["class"]], r["implemented_at"] or ""))
    counts = {k: sum(1 for r in rows if r["class"] == k) for k in ("評価可能", "評価保留", "要調査")}
    n_items = sum(len(r["items"]) for r in rows)
    notes = [
        "比較期間は「実装前28日間」と「直近28日間」（実装後の日数が28日未満の間は、実装前後の同じ日数）。"
        "実装から28日を超えた施策では、実装後の初期期間を含まないため、単純な実装直後との前後比較ではない。",
        "それぞれの期間の変化を、同じ期間の対象ページ以外（比較対照）の変化と比べる。前週比の順位変動（①・③）は施策の効果判定に使わない。",
        "「比較対照を上回る／下回る」は、対象ページの変化と比較対照の変化の差が日々の変動の範囲を超えたことを示す。"
        "「下回る」は対象ページの値が減ったことを意味しない（増加率が比較対照より小さい場合を含む）。",
        "施策の効果と、検索順位・クリック数の変動との因果関係は判断していない。",
        "評価可能＝比較対照と比べられる／要調査＝比較対照と異なる変化があり原因の確認が必要（効果・悪影響とは判断していない）／"
        "評価保留＝データ不足・評価方法が別などで判断しない。",
        "判定に使う指標は表示回数・クリック数（CTRは参考）。施策の主指標がCTRの場合はCTRで判定する。",
        f"施策{n_items}件のうち、対象ページ・実装日・判定・数値がすべて同じものは表示上1行にまとめた。"
        "施策ID・評価結果は施策ごとに保持しており、Appendixに施策別に掲載している。",
    ]
    return {"rows": rows, "counts": counts, "notes": notes, "item_count": n_items}


# ── ⑤ 問い合わせ（ダッシュボード） ─────────────────────────────────

def build_inquiry_dashboard(inq: dict) -> dict:
    by_key = {r["key"]: r for r in inq["rows"]}
    cum4 = inq["cumulative"][4]
    tiles = []
    for key, label in (("general_total", "一般問い合わせ"), ("project_consultation", "うち案件相談"),
                       ("sales_solicitation", "うち売り込み・提携勧誘"), ("other", "うちその他"),
                       ("recruiting_form", "採用フォーム（別集計）")):
        r = by_key.get(key)
        if not r:
            continue
        tiles.append({"key": key, "label": label, "current": r["current"], "cumulative_4w": r["cumulative_4w"],
                      "cumulative_complete": cum4["complete"], "primary": key == "project_consultation"})
    rate = inq.get("rate") or {}
    line = f"集計: {inq.get('status_label', '-')}・問い合わせ本文で分類"
    if rate.get("current_label"):
        line += f"。案件相談率 当週{rate['current_label']}・4週{rate['cumulative_4w_label']}"
    return {"tiles": tiles, "status_line": line, "cumulative": cum4, "warnings": inq.get("warnings", []),
            "entered": inq.get("entered")}


# ── ① 今週のSEO/AIO・コンテンツ評価 ───────────────────────────────

LEAD_MAX_CHANGES = 3
LEAD_MAX_ACTIONS = 3


def build_lead(keyword_pages: list[dict], pages: list[dict], aio: dict, initiatives: dict,
               inquiry_dashboard: dict, site_usage: dict) -> dict:
    """① 冒頭の意思決定サマリー: 最重要の変化（最大3件。AIOの推薦・引用の獲得／消失が最優先で、
    消失は件数に関係なく必ず載せる）・今週の評価（改善・悪化・判断保留の要点）・次の対応（最大3件）。
    ページ一覧・判定基準などの詳細は③・Appendix に置く。"""
    deal = [r for r in keyword_pages if r["tier"] == "deal"]
    lps = {p["page_id"]: p for p in build_regional_lp_summary(pages)}

    # 1. 最重要の変化
    aio_lines = aio.get("change_lines") or []
    must = [x["text"] for x in aio_lines if x["lost"]]
    optional = [x["text"] for x in aio_lines if not x["lost"]]
    for r in deal:
        if r.get("move") == "順位低下" or r.get("move") == "順位上昇":
            line = f"「{r['market_label']}」の平均順位が{r['prev_position']:.1f}→{r['position']:.1f}位（{r['move']}・前週比1週）"
            if r.get("pages"):
                line += f"。最も表示された{_page_move(r['pages'][0])}"
            optional.append(line)
    for r in deal:
        if r.get("investigate"):
            optional.append(f"調査対象: 「{r['market_label']}」で{r['page_evaluation']}。理由は未確認")
    changes = must + optional[:max(0, LEAD_MAX_CHANGES - len(must))]

    # 2. 今週の評価（改善・悪化・判断保留）。「最重要の変化」の数値を繰り返さず、解釈・判断だけを書く
    evaluation = []
    lost = [x for x in aio_lines if x["lost"]]
    lost_queries = {x["query"] for x in lost}
    for r in deal:
        aio_lost = r["query"] in lost_queries
        lp = next((lps[k] for k in r.get("expected_pages") or [] if k in lps), None)
        lp_down = bool(lp and lp["status"] == STATUS_DOWN)
        if r.get("move") == "順位低下" or aio_lost:
            parts = [x for x, ok in (("AIOの推薦・引用", aio_lost), ("検索順位", r.get("move") == "順位低下")) if ok]
            text = f"「{r['market_label']}」は{'と'.join(parts)}がそろって前回より後退した"
            if lp_down:
                text += "。地域LPは全クエリの合計でも順位が下がっており、このキーワードだけの変動ではない"
            text += "。ただし1回・1週の観測で、恒常的な低下とは判断しない。次回以降も続けば対応を検討する"
            evaluation.append({"kind": "悪化", "text": text})
    for x in lost:
        if x["query"] not in {r["query"] for r in deal}:
            evaluation.append({"kind": "悪化", "text": f"「{x['events'][0]['market_label']}」のAIOでTCDの推薦・引用が消失した。1回の観測で、恒常的な低下とは判断しない"})
    ups = [p for p in pages if p["status"] == STATUS_UP]
    if ups:
        with_clicks = [p for p in ups if p["clicks"] > p["prev_clicks"]]
        intents: dict[str, int] = {}
        for p in ups:
            intents[p["intent_label"]] = intents.get(p["intent_label"], 0) + 1
        top_intent, top_n = max(intents.items(), key=lambda kv: kv[1])
        where = f"検索意図が「{top_intent}」のページを中心に" if top_n * 2 >= len(ups) else ""
        evaluation.append({"kind": "改善", "text": f"{where}前週より順位が上がったページが{len(ups)}ページある。"
                                                  f"クリックが増えたのはそのうち{len(with_clicks)}ページで、成果につながるかは翌週以降に確認する"})
    c = initiatives["counts"]
    pending = []
    for r in deal:
        if r.get("investigate"):
            pending.append(f"「{r['market_label']}」は地域LP以外のページが多く表示される理由が未確認のため、良否を判断しない")
    pending.append(f"施策は効果・悪影響を判断できる段階にない（要調査{c['要調査']}・評価保留{c['評価保留']}、④）")
    evaluation.append({"kind": "判断保留", "text": "。".join(pending)})

    # 3. 次の対応（最大3件）
    actions = []
    for x in lost:
        actions.append(f"「{x['events'][0]['market_label']}」のAIOで推薦・引用が戻るかを次回以降の観測（日次のAIO監視を含む）で確認し、"
                       "今回引用されたページとTCDのページ（所在地・実績の示し方）を比べる")
    for r in deal:
        if r.get("move") == "順位低下":
            actions.append(f"「{r['market_label']}」の順位低下の要因を、ページ×クエリで前週と比べて確認する（1週の変化のため、改修はまだ決めない）")
    for r in deal:
        if r.get("investigate"):
            top = r["pages"][0]["name"] if r.get("pages") else "他のページ"
            actions.append(f"「{r['market_label']}」で{top}の表示が地域LPより多い理由を、どのクエリでどちらのページが表示されているかで調べる")
    for row in initiatives["rows"]:
        if row["class"] == "要調査" and row.get("next_action"):
            actions.append(f"施策「{'／'.join(row['names'])}」: {row['next_action']}")
    actions = actions[:LEAD_MAX_ACTIONS]

    progressed = [p for p in pages if p["status"] == STATUS_UP]
    thin = [p for p in pages if p["status"] == STATUS_INSUFFICIENT and p["objective"] in ("conversion", "regional", "comparison")]
    # 地域LPは③の「地域LPの検索実績」に区分・論点・次の対応を載せるため、改善候補には重ねて載せない
    candidates = sorted([p for p in pages if p["status"] in _CANDIDATE_STATUSES and p["objective"] != "regional"],
                        key=_candidate_priority)
    return {"changes": changes, "evaluation": evaluation, "actions": actions,
            "progressed": progressed, "candidates": candidates, "thin_candidates": thin}
