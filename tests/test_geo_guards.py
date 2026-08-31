"""Location-quality guards: a vague or wrong-country area must fail loudly, never scrape garbage."""

import json

import pytest

from leadforge.grid import _distinct_places, build_plan, geocode, qualify_area
from leadforge.util import InputError


def test_query_text_is_country_qualified(cfg, sample_icp):
    queries = build_plan(sample_icp, cfg)
    assert queries
    for q in queries:
        assert "United States" in q.text, q.text


def test_qualify_area_no_double_country():
    assert qualify_area("Houston, TX", "US") == "Houston, TX, United States"
    # already qualified -> unchanged
    assert qualify_area("Houston, TX, United States", "US") == "Houston, TX, United States"
    # unknown code falls back to the code itself
    assert qualify_area("Someplace", "ZW").endswith("ZW")


def _fake_httpx(monkeypatch, rows):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return rows

    captured = {}

    def _get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr("leadforge.grid.httpx.get", _get)
    monkeypatch.setattr("leadforge.grid.time.sleep", lambda s: None)
    return captured


def test_geocode_constrains_by_country(cfg, monkeypatch):
    captured = _fake_httpx(monkeypatch, [
        {"lat": "29.76", "lon": "-95.36", "boundingbox": ["29.5", "30.1", "-95.8", "-95.0"],
         "display_name": "Houston, Texas, United States", "importance": 0.8, "address": {"state": "Texas"}},
    ])
    out = geocode("Houston", cfg, "US")
    assert captured["params"]["countrycodes"] == "us"   # the wrong-country guard
    assert out["bbox"] == [-95.8, 29.5, -95.0, 30.1]


def test_geocode_refuses_ambiguous(cfg, monkeypatch):
    _fake_httpx(monkeypatch, [
        {"lat": "39.8", "lon": "-89.6", "boundingbox": ["39.7", "39.9", "-89.7", "-89.5"],
         "display_name": "Springfield, Illinois", "importance": 0.51, "address": {"state": "Illinois"}},
        {"lat": "37.2", "lon": "-93.3", "boundingbox": ["37.1", "37.3", "-93.4", "-93.2"],
         "display_name": "Springfield, Missouri", "importance": 0.50, "address": {"state": "Missouri"}},
    ])
    with pytest.raises(InputError) as e:
        geocode("Springfield", cfg, "US")
    assert "ambiguous" in str(e.value).lower()
    assert "Illinois" in str(e.value)


def test_geocode_accepts_clear_winner(cfg, monkeypatch):
    _fake_httpx(monkeypatch, [
        {"lat": "29.76", "lon": "-95.36", "boundingbox": ["29.5", "30.1", "-95.8", "-95.0"],
         "display_name": "Houston, Texas", "importance": 0.90, "address": {"state": "Texas"}},
        {"lat": "37.0", "lon": "-89.0", "boundingbox": ["36.9", "37.1", "-89.1", "-88.9"],
         "display_name": "Houston, Missouri", "importance": 0.40, "address": {"state": "Missouri"}},
    ])
    assert geocode("Houston", cfg, "US")["display"].startswith("Houston, Texas")


def test_geocode_not_found_is_actionable(cfg, monkeypatch):
    _fake_httpx(monkeypatch, [])
    with pytest.raises(InputError) as e:
        geocode("Nowhereville", cfg, "GB")
    msg = str(e.value)
    assert "not found" in msg and "GB" in msg


def test_geocode_cache_is_country_scoped(cfg, monkeypatch):
    _fake_httpx(monkeypatch, [
        {"lat": "1", "lon": "1", "boundingbox": ["0", "2", "0", "2"], "display_name": "X, US",
         "importance": 0.9, "address": {"state": "S"}},
    ])
    geocode("Ambiguousville", cfg, "US")
    cache = json.loads((cfg.cache_dir / "geocode.json").read_text())
    assert "US|ambiguousville" in cache  # keyed by country, so GB lookups can't reuse a US hit


def test_distinct_places_helper():
    a = {"lat": "1", "lon": "1", "address": {"state": "A"}}
    b = {"lat": "1", "lon": "1", "address": {"state": "B"}}
    same = {"lat": "1", "lon": "1", "address": {"state": "A"}}
    assert _distinct_places(a, b) is True
    assert _distinct_places(a, same) is False


def test_max_leads_is_a_hard_stop(cfg, sample_icp, monkeypatch, tmp_path):
    """U8.4 audit item 3: caps.max_leads stops discovery upserts."""
    import yaml

    from leadforge.models import RawListing
    from leadforge.util import now_iso
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.available", lambda self: (True, "mock"))
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.fetch", lambda self, q, limit=None: [
        RawListing(provider="gosom", fetched_at=now_iso(), data={
            "title": f"Shop {i}", "address": f"{i} A St, Houston, TX 77001",
            "phone": f"713-555-0{i:03d}", "place_id": f"PID_{i}"}) for i in range(10)])
    sample_icp.caps.max_leads = 3
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")
    from leadforge import db
    from leadforge.pipeline import run_discover
    run_discover(cfg, sample_icp, icp_path)
    conn = db.connect(cfg.db_path)
    n = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    assert n <= 3, f"max_leads=3 but {n} businesses upserted"
