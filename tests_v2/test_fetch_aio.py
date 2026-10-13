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
            return responses[self.params["engine"]]

    monkeypatch.setitem(sys.modules, "serpapi", types.SimpleNamespace(GoogleSearch=FakeSearch))
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
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
