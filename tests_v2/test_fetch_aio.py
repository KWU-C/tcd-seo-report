import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2 import fetch_aio


def _install_fake_serpapi(monkeypatch, responses):
    calls = []

    class FakeSearch:
        def __init__(self, params):
            self.params = params
            calls.append(params)

        def get_dict(self):
            response = responses[self.params["engine"]]
            return response(self.params) if callable(response) else response

    monkeypatch.setitem(sys.modules, "serpapi", types.SimpleNamespace(GoogleSearch=FakeSearch))
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setattr(fetch_aio.time, "sleep", lambda seconds: None)
    return calls


def test_page_token_only_ai_overview_triggers_followup_request(monkeypatch, tmp_path):
    # ai_overviewがpage_tokenだけの場合、本文・引用元を追加取得する（2026-10-13 東京で引用元が空になった原因）
    calls = _install_fake_serpapi(monkeypatch, {
        "google": {"ai_overview": {"page_token": "tok", "serpapi_link": "https://serpapi.com/x"}},
        "google_ai_overview": {"ai_overview": {"references": [{"link": "https://example.com/"}]}},
    })
    record = fetch_aio.fetch_priority_queries(["ブランディング 会社 東京"], raw_dir=tmp_path)[0]
    assert [c["engine"] for c in calls] == ["google", "google_ai_overview"]
    assert calls[1]["page_token"] == "tok"
    assert record["acquisition_status"] == "success"
    assert record["raw_response"]["ai_overview_result"]["ai_overview"]["references"]


def test_ai_overview_with_content_does_not_trigger_followup(monkeypatch, tmp_path):
    calls = _install_fake_serpapi(monkeypatch, {
        "google": {"ai_overview": {"text_blocks": [{"snippet": "x"}], "references": []}},
    })
    fetch_aio.fetch_priority_queries(["ブランディング"], raw_dir=tmp_path)
    assert [c["engine"] for c in calls] == ["google"]


_NO_RESULTS = {"error": "Google hasn't returned any results for this query."}
_TOKEN_ONLY = {"ai_overview": {"page_token": "tok", "serpapi_link": "https://serpapi.com/x"}}


def test_failed_followup_is_retried_once_with_a_fresh_uncached_search(monkeypatch, tmp_path):
    # キャッシュ済み検索の失効tokenでは追加取得が空になる。no_cacheの再検索で回復する
    def google(params):
        if params.get("no_cache"):
            return {"ai_overview": {"references": [{"link": "https://example.com/"}]}}
        return _TOKEN_ONLY

    calls = _install_fake_serpapi(monkeypatch, {"google": google, "google_ai_overview": _NO_RESULTS})
    record = fetch_aio.fetch_priority_queries(["ブランディング 会社 東京"], raw_dir=tmp_path)[0]
    assert [(c["engine"], c.get("no_cache")) for c in calls] == [
        ("google", None), ("google_ai_overview", None), ("google", "true")]
    assert record["raw_response"]["search_result"]["ai_overview"]["references"]
    # 1回目の応答も元データとして保持する
    assert record["raw_response"]["previous_attempt"]["ai_overview_result"] == _NO_RESULTS


def test_retry_happens_at_most_once_and_stays_unavailable(monkeypatch, tmp_path):
    from src_v2 import normalize

    calls = _install_fake_serpapi(monkeypatch, {"google": _TOKEN_ONLY, "google_ai_overview": _NO_RESULTS})
    record = fetch_aio.fetch_priority_queries(["ブランディング 会社 東京"], raw_dir=tmp_path)[0]
    assert [c["engine"] for c in calls] == ["google", "google_ai_overview", "google", "google_ai_overview"]
    obs = normalize.build_aio_observation(record)
    # 取得できなければ「判定不可」。推測で「引用なし」にしない
    assert obs["citation_status"] == "unavailable"
    assert obs["tcd_cited"] is None


def test_non_retriable_errors_are_not_retried(monkeypatch, tmp_path):
    for error in ("Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key",
                  "Your account has run out of searches."):
        calls = _install_fake_serpapi(monkeypatch, {"google": _TOKEN_ONLY, "google_ai_overview": {"error": error}})
        fetch_aio.fetch_priority_queries(["ブランディング 会社 東京"], raw_dir=tmp_path)
        assert [c["engine"] for c in calls] == ["google", "google_ai_overview"]
