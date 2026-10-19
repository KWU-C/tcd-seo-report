import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2 import config_loader, normalize
from src_v2.analysis.initiatives import _resolve_target_page_ids


def test_pages_yaml_loads_without_duplicates():
    pages = config_loader.load_pages()
    assert len({p["id"] for p in pages}) == len(pages)


def test_tokyo_lp_is_monitored_as_regional_page():
    pages = config_loader.load_pages()
    tokyo = [p for p in pages if p["url"] == "https://tcd.jp/tokyo"]
    assert len(tokyo) == 1
    assert tokyo[0]["id"] == "branding-tokyo"
    assert tokyo[0]["objective"] == ["regional"]
    assert normalize.build_page_lookup(pages)["https://tcd.jp/tokyo"] == "branding-tokyo"


def test_tokyo_initiative_target_url_resolves_to_page():
    pages = config_loader.load_pages()
    assert _resolve_target_page_ids({"target_url": "https://tcd.jp/tokyo"}, pages) == ["branding-tokyo"]
