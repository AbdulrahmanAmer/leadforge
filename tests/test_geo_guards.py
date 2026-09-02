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


# --- v0.3 A1: geocoder coordinate-first ambiguity + addresstype preference + area_bbox override ---
def test_geocode_same_city_returned_twice_is_not_ambiguous(cfg, monkeypatch):
    """docs/09 A1: a city row and an administrative row for the same city carry different address
    dicts (one has no 'city' key at all) but sit at almost the same point — must resolve, not raise."""
    _fake_httpx(monkeypatch, [
        {"lat": "53.4808", "lon": "-2.2426", "boundingbox": ["53.40", "53.56", "-2.35", "-2.13"],
         "display_name": "Manchester, Greater Manchester, England, UK", "importance": 0.75,
         "addresstype": "city",
         "address": {"city": "Manchester", "county": "Greater Manchester", "state": "England"}},
        {"lat": "53.4831", "lon": "-2.2441", "boundingbox": ["53.39", "53.55", "-2.34", "-2.12"],
         "display_name": "Manchester, Greater Manchester, England, UK", "importance": 0.72,
         "addresstype": "administrative",
         "address": {"state_district": "Greater Manchester", "state": "England"}},
    ])
    out = geocode("Manchester", cfg, "GB")
    assert "Manchester" in out["display"]


def test_geocode_prefers_city_over_canal(cfg, monkeypatch):
    """docs/09 A1: 'City of Manchester' used to resolve to the Manchester Ship Canal (Warrington)
    because it topped the Nominatim results — a disfavored addresstype must never win, or even be
    compared for ambiguity, against a real place row."""
    _fake_httpx(monkeypatch, [
        {"lat": "53.39", "lon": "-2.57", "boundingbox": ["53.35", "53.45", "-2.65", "-2.50"],
         "display_name": "Manchester Ship Canal, Warrington, Cheshire, England, UK", "importance": 0.6,
         "addresstype": "canal",
         "address": {"waterway": "Manchester Ship Canal", "county": "Cheshire", "state": "England"}},
        {"lat": "53.4808", "lon": "-2.2426", "boundingbox": ["53.40", "53.56", "-2.35", "-2.13"],
         "display_name": "Manchester, Greater Manchester, England, UK", "importance": 0.55,
         "addresstype": "city",
         "address": {"city": "Manchester", "county": "Greater Manchester", "state": "England"}},
    ])
    out = geocode("City of Manchester", cfg, "GB")
    assert out["display"] == "Manchester, Greater Manchester, England, UK"


def test_geocode_not_found_error_names_area_bbox_as_the_fix(cfg, monkeypatch):
    """A1 review (minor): the actual fix docs/09 introduced for a stubborn geocoder is the
    discovery.area_bbox override — the not-found message must say so, not just point at the
    (different, pre-existing) target.geography.bbox setting."""
    _fake_httpx(monkeypatch, [])
    with pytest.raises(InputError) as e:
        geocode("Nowhereville", cfg, "GB")
    assert "area_bbox" in str(e.value)


def test_geocode_ambiguous_error_names_area_bbox_as_the_fix(cfg, monkeypatch):
    _fake_httpx(monkeypatch, [
        {"lat": "39.8", "lon": "-89.6", "boundingbox": ["39.7", "39.9", "-89.7", "-89.5"],
         "display_name": "Springfield, Illinois", "importance": 0.51, "address": {"state": "Illinois"}},
        {"lat": "37.2", "lon": "-93.3", "boundingbox": ["37.1", "37.3", "-93.4", "-93.2"],
         "display_name": "Springfield, Missouri", "importance": 0.50, "address": {"state": "Missouri"}},
    ])
    with pytest.raises(InputError) as e:
        geocode("Springfield", cfg, "US")
    assert "area_bbox" in str(e.value)


def test_geocode_prefers_preferred_addresstype_over_a_merely_undisfavored_higher_importance_row(cfg, monkeypatch):
    """A1 review (minor): _PREFERRED_ADDRESSTYPES must actually be used — a non-disfavored,
    non-preferred addresstype (e.g. 'road') at higher importance must not beat a lower-importance
    'town' row; disfavoring alone (waterway/amenity/tourism/information/canal) is not enough."""
    _fake_httpx(monkeypatch, [
        {"lat": "53.10", "lon": "-2.90", "boundingbox": ["53.05", "53.15", "-2.95", "-2.85"],
         "display_name": "Manchester Road, Northwich, Cheshire, England, UK", "importance": 0.65,
         "addresstype": "road",
         "address": {"road": "Manchester Road", "county": "Cheshire", "state": "England"}},
        {"lat": "53.4808", "lon": "-2.2426", "boundingbox": ["53.40", "53.56", "-2.35", "-2.13"],
         "display_name": "Manchester, Greater Manchester, England, UK", "importance": 0.55,
         "addresstype": "town",
         "address": {"town": "Manchester", "county": "Greater Manchester", "state": "England"}},
    ])
    out = geocode("Manchester", cfg, "GB")
    assert out["display"] == "Manchester, Greater Manchester, England, UK"


def test_geocode_canal_loses_to_undisfavored_type_with_no_preferred_row_present(cfg, monkeypatch):
    """A1 review (minor): a disfavored canal (higher importance, listed first) must lose to a
    'railway' row that is in NEITHER _PREFERRED_ADDRESSTYPES nor _DISFAVORED_ADDRESSTYPES — proving
    _DISFAVORED_ADDRESSTYPES itself does the exclusion, independent of the preferred-tier logic
    (which has nothing to prefer here). Must go red if _DISFAVORED_ADDRESSTYPES is removed: with no
    disfavored filter, the canal (0.8 > 0.4 importance) would win outright."""
    _fake_httpx(monkeypatch, [
        {"lat": "53.39", "lon": "-2.57", "boundingbox": ["53.35", "53.45", "-2.65", "-2.50"],
         "display_name": "Random Canal, Cheshire, England, UK", "importance": 0.8,
         "addresstype": "canal",
         "address": {"waterway": "Random Canal", "county": "Cheshire", "state": "England"}},
        {"lat": "53.10", "lon": "-2.90", "boundingbox": ["53.05", "53.15", "-2.95", "-2.85"],
         "display_name": "Random Railway Station, Cheshire, England, UK", "importance": 0.4,
         "addresstype": "railway",
         "address": {"railway": "Random Railway Station", "county": "Cheshire", "state": "England"}},
    ])
    out = geocode("Randomtown", cfg, "GB")
    assert out["display"] == "Random Railway Station, Cheshire, England, UK"


def test_area_bbox_override_skips_nominatim_entirely(cfg, monkeypatch):
    """docs/09 A1: cfg.discovery.area_bbox[area] (exact or casefolded) is used verbatim and the
    Nominatim transport must never be touched — not even to populate the cache."""
    cfg.discovery.area_bbox = {"City of Manchester": [-2.35, 53.40, -2.13, 53.56]}

    def _must_not_call(*a, **k):
        raise AssertionError("geocode() called Nominatim despite an area_bbox override")

    monkeypatch.setattr("leadforge.grid.httpx.get", _must_not_call)
    out = geocode("city of manchester", cfg, "GB")  # casefolded form still matches the config key
    assert out["bbox"] == [-2.35, 53.40, -2.13, 53.56]


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


def test_duplicate_listings_do_not_consume_max_leads(cfg, sample_icp, monkeypatch, tmp_path):
    """Found live: a 1000-cap run stopped at 709 uniques because cross-category duplicates
    counted against the cap. The cap must count unique upserts only."""
    import yaml

    from leadforge.models import RawListing
    from leadforge.util import now_iso
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.available", lambda self: (True, "mock"))
    # every query returns the SAME 5 businesses -> duplicates across queries
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.fetch", lambda self, q, limit=None: [
        RawListing(provider="gosom", fetched_at=now_iso(), data={
            "title": f"Shop {i}", "address": f"{i} A St, Houston, TX 77001",
            "phone": f"713-555-0{i:03d}", "place_id": f"PID_{i}"}) for i in range(5)])
    sample_icp.caps.max_leads = 8
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")
    from leadforge import db
    from leadforge.pipeline import run_discover
    run_id, counts, _ = run_discover(cfg, sample_icp, icp_path)
    conn = db.connect(cfg.db_path)
    n = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    assert n == 5  # all uniques kept
    # all queries ran (duplicates never ate the cap of 8)
    pending = conn.execute("SELECT COUNT(*) c FROM queries WHERE status='pending'").fetchone()["c"]
    assert pending == 0
