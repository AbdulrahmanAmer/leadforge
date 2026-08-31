"""Provider adapter tests. gosom parsing is fixture-based (no binary/network).

The fallback_rest test is a TODO stub tied to ICM U3.6 — see docs/05.
"""
import json

from leadforge.config import load_config
from leadforge.providers.gosom import GosomProvider


def test_gosom_parse_ndjson(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = GosomProvider(cfg)
    out = tmp_path / "out.json"
    rows = [
        {"title": "A Shop", "phone": "713-555-0100", "web_site": "http://a.com", "place_id": "P1"},
        {"title": "B Shop", "phone": "713-555-0111", "place_id": "P2"},
    ]
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    listings = list(prov._parse(out))
    assert len(listings) == 2
    assert listings[0].provider == "gosom"
    assert listings[0].data["title"] == "A Shop"


def test_gosom_parse_json_array(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = GosomProvider(cfg)
    out = tmp_path / "out.json"
    out.write_text(json.dumps([{"title": "Solo"}]), encoding="utf-8")
    assert len(list(prov._parse(out))) == 1


def test_gosom_available_without_binary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    ok, reason = GosomProvider(cfg).available()
    assert ok is False and "doctor" in reason


# --- U3.6 acceptance stub (xfail until implemented) ---------------------------------------
import pytest  # noqa: E402


@pytest.mark.xfail(reason="ICM U3.6: fallback_rest not implemented yet", strict=False)
def test_fallback_rest_parse():
    from leadforge.providers.fallback_rest import FallbackRestProvider  # noqa: F401
    raise AssertionError("implement per fallback_rest.py spec + a canned JSON fixture")
