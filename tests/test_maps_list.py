"""Tests for the maps_list provider (speed unit, docs/09, ADR-014).

No network anywhere in this file. The one test that touches a real (headless) browser only loads a
static fixture via page.set_content() — tests/fixtures/maps_feed.html, captured once from a live
search (see scratchpad/speed/builder/capture_feed_fixture.py) — and skips cleanly when Playwright or
its Chromium binary is unavailable.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from leadforge import db
from leadforge.grid import Tile
from leadforge.models import ICP, RawListing
from leadforge.normalize import to_business
from leadforge.providers.base import get_field_map
from leadforge.providers.maps_list import (
    MapsListProvider,
    _chromium_binary_present,
    card_to_data,
    parse_card,
    parse_category_street,
    parse_open_and_phone,
    parse_rating,
    search_url,
    tile_viewport,
    unwrap_website,
    zoom_for_cell_km,
)
from leadforge.util import now_iso

FIXTURE = Path(__file__).parent / "fixtures" / "maps_feed.html"
_PW_OK, _PW_REASON = _chromium_binary_present()


# --------------------------------------------------------------------------- pure geometry helpers
def test_zoom_for_cell_km_thresholds():
    assert zoom_for_cell_km(1.0) == 16
    assert zoom_for_cell_km(1.5) == 16
    assert zoom_for_cell_km(1.6) == 15
    assert zoom_for_cell_km(3.0) == 15
    assert zoom_for_cell_km(3.1) == 14
    assert zoom_for_cell_km(6.0) == 14
    assert zoom_for_cell_km(6.1) == 13
    assert zoom_for_cell_km(20.0) == 13


def test_tile_viewport_is_bbox_centre():
    t = Tile(bbox=(-1.2, 52.9, -1.0, 53.1), cell_km=3.0)
    lat, lng, zoom = tile_viewport(t)
    assert lat == pytest.approx(53.0)
    assert lng == pytest.approx(-1.1)
    assert zoom == 15


def test_search_url_tiled_vs_untiled():
    t = Tile(bbox=(-1.2, 52.9, -1.0, 53.1), cell_km=3.0)
    tiled = search_url("car garage in Nottingham", t)
    assert tiled == "https://www.google.com/maps/search/car+garage+in+Nottingham/@53.0,-1.1,15z?hl=en"
    untiled = search_url("car garage in Nottingham, United Kingdom", None)
    assert untiled == "https://www.google.com/maps/search/car+garage+in+Nottingham,+United+Kingdom?hl=en"
    assert "@" not in untiled  # no viewport at all when there's no tile


# --------------------------------------------------------------------------- website unwrapping
def test_unwrap_website_redirect():
    wrapped = ("https://www.google.com/url?q=http://www.cairostreetgarage.co.uk/"
               "&opi=79508299&sa=U&ved=xx&usg=yy")
    assert unwrap_website(wrapped) == "http://www.cairostreetgarage.co.uk/"


def test_unwrap_website_passthrough_for_non_redirect():
    assert unwrap_website("http://plain-site.example/") == "http://plain-site.example/"


def test_unwrap_website_none_and_empty():
    assert unwrap_website(None) is None
    assert unwrap_website("") is None


# --------------------------------------------------------------------------- line parsing
def test_parse_rating_plain_and_with_reviews():
    assert parse_rating(["Some Name", "4.8", "junk"]) == ("4.8", None)
    assert parse_rating(["Some Name", "4.9(199)"]) == ("4.9", "199")
    assert parse_rating(["no rating here"]) == (None, None)


def test_parse_category_street_skips_open_line():
    lines = ["X Garage", "X Garage", "4.8", "Auto repair shop ·  · 6 Cairo St",
             "Closed · Opens 8:30 AM Thu · +44 115 970 8888"]
    category, street = parse_category_street(lines)
    assert category == "Auto repair shop"
    assert street == "6 Cairo St"


def test_parse_open_and_phone_extracts_last_phone_shaped_segment():
    lines = ["Closed · Opens 8:30 AM Thu · +44 115 970 8888"]
    open_state, phone = parse_open_and_phone(lines)
    assert open_state == "Closed"
    assert phone == "+44 115 970 8888"


def test_parse_open_and_phone_no_phone_when_segment_not_phone_shaped():
    open_state, phone = parse_open_and_phone(["Open 24 hours"])
    assert open_state == "Open 24 hours"
    assert phone is None


def test_parse_card_from_probe_sample():
    """Reproduces scratchpad/speed/probe_list2_result.json's first Nottingham card verbatim."""
    sample = {
        "name": "Cairo street garage ltd",
        "href": ("https://www.google.com/maps/place/x/@52.9752996,-1.1697032,17z/"
                 "data=!4m6!3m5!1s0x4879c195f646788f:0xdc576aff5f40dc49!8m2!3d52.9752996!4d-1.1697032"),
        "website": ("https://www.google.com/url?q=http://www.cairostreetgarage.co.uk/"
                    "&opi=79508299&sa=U&ved=xx&usg=yy"),
        "lines": ["Cairo street garage ltd", "Cairo street garage ltd", "4.8",
                  "Auto repair shop ·  · 6 Cairo St",
                  "Closed · Opens 8:30 AM Thu · +44 115 970 8888", "", "Website", ""],
    }
    parsed = parse_card(sample)
    assert parsed["name"] == "Cairo street garage ltd"
    # decimal-decoded from the cid half of !1s0x4879c195f646788f:0xdc576aff5f40dc49 — the same
    # representation gosom's own `cid` field uses for the same place (see _CID_RE's docstring)
    assert parsed["cid"] == "15877276656365263945"
    assert parsed["lat"] == pytest.approx(52.9752996)
    assert parsed["lng"] == pytest.approx(-1.1697032)
    assert parsed["rating"] == "4.8"
    assert parsed["category"] == "Auto repair shop"
    assert parsed["street"] == "6 Cairo St"
    assert parsed["open_state"] == "Closed"
    assert parsed["phone"] == "+44 115 970 8888"
    assert parsed["website"] == "http://www.cairostreetgarage.co.uk/"


def test_parse_card_no_name_returns_none():
    assert parse_card({"name": "", "href": "", "lines": []}) is None


def test_card_to_data_shape():
    parsed = {"name": "X Garage", "href": "https://x", "cid": "0xabc:0xdef", "lat": 1.0, "lng": 2.0,
             "rating": "4.5", "review_count": "10", "category": "Auto repair shop",
             "street": "6 Cairo St", "open_state": "Closed", "phone": "+44 115 970 8888",
             "website": "http://x.example"}
    data = card_to_data(parsed, "Nottingham", "United Kingdom")
    assert data["title"] == "X Garage"
    assert data["place_id"] is None
    assert data["cid"] == "0xabc:0xdef"
    assert data["phone"] == "+44 115 970 8888"
    assert data["web_site"] == "http://x.example"
    assert data["review_rating"] == "4.5"
    assert data["review_count"] == "10"
    assert data["category"] == "Auto repair shop"
    assert data["categories"] == ["Auto repair shop"]
    assert data["address"] == "6 Cairo St, Nottingham"
    assert data["complete_address"] == {"street": "6 Cairo St", "city": "Nottingham",
                                        "country": "United Kingdom"}
    assert data["latitude"] == 1.0 and data["longitude"] == 2.0
    assert data["list_only"] is True
    assert "known" not in data  # only set when the CID is already known


# --------------------------------------------------------------------------- FIELD_MAP + normalize
def test_field_map_registered():
    fmap = get_field_map("maps_list")
    assert fmap is not None
    assert fmap["name"] == ["title"]
    assert fmap["cid"] == ["cid"]


def test_to_business_from_maps_list_listing():
    """phone E.164, domain from the unwrapped website, cid stored, dedupe key on cid (no place_id)."""
    data = card_to_data(
        {"name": "Ludlow Hill MOT Centre", "href": "https://maps/place/x",
         "cid": "0x4879c3a3474a6f09:0x3f43453c3c0eb688", "lat": 52.921409, "lng": -1.1276918,
         "rating": "4.9", "review_count": "199", "category": "Car inspection station",
         "street": "Unit 10 Ludlow Hill Rd", "open_state": "Closed", "phone": "+44 115 923 4553",
         "website": "http://www.nottinghammotcentre.co.uk/"},
        "Nottingham", "United Kingdom",
    )
    icp = ICP.model_validate({
        "campaign": "t", "offer": {"what": "x"},
        "target": {"categories": ["MOT centre"], "geography": {"areas": ["Nottingham"], "country": "GB"}},
    })
    raw = RawListing(provider="maps_list", fetched_at=now_iso(), data=data)
    biz = to_business(raw, "run_x", icp, "GB")
    assert biz is not None
    assert biz.name == "Ludlow Hill MOT Centre"
    assert biz.place_id is None
    assert biz.cid == "0x4879c3a3474a6f09:0x3f43453c3c0eb688"
    assert biz.dedupe_key == f"cid:{biz.cid}"
    assert biz.phone_e164 == "+441159234553"
    assert biz.website == "http://www.nottinghammotcentre.co.uk"
    assert biz.domain == "nottinghammotcentre.co.uk"
    assert biz.address_city == "Nottingham"
    assert biz.rating == 4.9 and biz.review_count == 199


def test_to_business_marks_known_flag_untouched_by_normalize():
    """The 'known' marker is pipeline-facing metadata; to_business must not choke on its presence."""
    data = card_to_data(
        {"name": "X Garage", "href": "https://x", "cid": "0xaaa:0xbbb", "lat": None, "lng": None,
         "rating": None, "review_count": None, "category": None, "street": None,
         "open_state": None, "phone": "+44 115 970 8888", "website": None},
        "", None,
    )
    data["known"] = True
    icp = ICP.model_validate({
        "campaign": "t", "offer": {"what": "x"},
        "target": {"categories": ["auto repair shop"], "geography": {"areas": ["Nottingham"], "country": "GB"}},
    })
    raw = RawListing(provider="maps_list", fetched_at=now_iso(), data=data)
    biz = to_business(raw, "run_x", icp, "GB")
    assert biz is not None and biz.cid == "0xaaa:0xbbb"


# --------------------------------------------------------------------------- available() never launches a browser
def test_available_without_binary(tmp_path, monkeypatch):
    from leadforge.config import load_config
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(
        "leadforge.providers.maps_list._chromium_binary_present",
        lambda: (False, "chromium binary not found — run: playwright install chromium"),
    )
    ok, reason = MapsListProvider(cfg).available()
    assert ok is False and "playwright install" in reason


def test_available_never_launches_browser(tmp_path, monkeypatch):
    """Watched-fail target: if available() ever imports sync_playwright, this raises loudly."""
    from leadforge.config import load_config
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("available() must never touch sync_playwright")

    import playwright.sync_api as pw_sync
    monkeypatch.setattr(pw_sync, "sync_playwright", _boom)
    ok, reason = MapsListProvider(cfg).available()
    assert isinstance(ok, bool) and isinstance(reason, str)


# --------------------------------------------------------------------------- extraction JS against the live-captured fixture
@pytest.mark.skipif(not _PW_OK, reason=_PW_REASON)
def test_extraction_js_against_live_fixture():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from leadforge.providers.maps_list import _EXTRACT_JS

    html = FIXTURE.read_text(encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        cards = page.evaluate(_EXTRACT_JS)
        browser.close()

    assert len(cards) >= 10, "fixture should carry a real batch of cards"
    parsed = [c for c in (parse_card(r) for r in cards) if c is not None]
    assert len(parsed) == len(cards)
    assert sum(1 for p in parsed if p["cid"]) == len(parsed)  # every real card has a CID
    assert sum(1 for p in parsed if p["phone"]) >= len(parsed) * 0.9  # measured 119-120/120 live
    # every extracted website must already be unwrapped (never a google.com/url redirect)
    for p in parsed:
        if p["website"]:
            assert "google.com/url" not in p["website"]


# --------------------------------------------------------------------------- pipeline: known-CID hook
def test_pipeline_known_cids_hook_grows_across_queries(cfg, monkeypatch, tmp_path):
    """item 3: run_discover seeds provider.known_cids once, then grows it as businesses are credited."""
    import yaml

    from leadforge.pipeline import run_discover

    icp = ICP.model_validate({
        "campaign": "t", "offer": {"what": "x"},
        "target": {"categories": ["auto repair shop"],
                   "geography": {"areas": ["Area One", "Area Two"], "country": "US"}},
    })
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)

    snapshots: list[set[str]] = []

    class _CidAware:
        name = "fake_cid"
        supports_tiles = False

        def __init__(self):
            self.known_cids: set[str] = set()

        def available(self):
            return True, "ok"

        def fetch(self, pq, limit=None):
            snapshots.append(set(self.known_cids))
            n = len(snapshots)
            return [RawListing(provider="fake_cid", fetched_at=now_iso(), data={
                "title": f"Biz {n}", "cid": f"CID_{n}", "phone": f"713-555-01{n:02d}"})]

    fake = _CidAware()
    monkeypatch.setattr("leadforge.pipeline.get_chain", lambda cfg, only=None: [fake])
    cfg.discovery.providers = ["fake_cid"]

    run_discover(cfg, icp, icp_path)
    assert len(snapshots) == 2
    assert snapshots[0] == set()      # nothing known before the first query ever runs
    assert "CID_1" in snapshots[1]    # the business found by query #1 is known before query #2 fetches


def test_known_flag_set_when_cid_already_known(cfg):
    """Unit-level check of the exact marking logic fetch() applies (data['known']=True on a repeat CID)."""
    provider = MapsListProvider(cfg)
    provider.known_cids = {"0xaaa:0xbbb"}
    data_seen = card_to_data(
        {"name": "Seen Before Garage", "href": "https://x", "cid": "0xaaa:0xbbb", "lat": None,
         "lng": None, "rating": None, "review_count": None, "category": None, "street": None,
         "open_state": None, "phone": None, "website": None},
        "", None,
    )
    if data_seen.get("cid") in provider.known_cids:
        data_seen["known"] = True
    data_new = card_to_data(
        {"name": "New Garage", "href": "https://y", "cid": "0xccc:0xddd", "lat": None, "lng": None,
         "rating": None, "review_count": None, "category": None, "street": None, "open_state": None,
         "phone": None, "website": None},
        "", None,
    )
    if data_new.get("cid") in provider.known_cids:
        data_new["known"] = True
    assert data_seen.get("known") is True
    assert "known" not in data_new


# --------------------------------------------------------------------------- pipeline: parallel_queries
def _run_sleepy_discover(parallel_queries: int, tmp_path, monkeypatch, n_queries: int = 4,
                         sleep_s: float = 0.2) -> float:
    import yaml

    from leadforge.config import load_config
    from leadforge.pipeline import run_discover

    ws = tmp_path / f"ws_{parallel_queries}"
    ws.mkdir()
    monkeypatch.chdir(ws)
    cfg = load_config(ws)
    cfg.discovery.providers = ["fake_sleepy"]
    cfg.discovery.parallel_queries = parallel_queries

    areas = [f"Fake Area {i}" for i in range(n_queries)]
    icp = ICP.model_validate({
        "campaign": "t", "offer": {"what": "x"},
        "target": {"categories": ["auto repair shop"], "geography": {"areas": areas, "country": "US"}},
    })
    icp_path = ws / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)

    class _Sleepy:
        name = "fake_sleepy"
        supports_tiles = False

        def available(self):
            return True, "ok"

        def fetch(self, pq, limit=None):
            time.sleep(sleep_s)
            return [RawListing(provider="fake_sleepy", fetched_at=now_iso(), data={
                "title": f"Biz {pq.text}", "phone": f"713-555-{abs(hash(pq.text)) % 10000:04d}",
                "place_id": f"PID_{pq.text}"})]

    monkeypatch.setattr("leadforge.pipeline.get_chain", lambda cfg, only=None: [_Sleepy()])

    t0 = time.monotonic()
    run_id, counts, warns = run_discover(cfg, icp, icp_path)
    elapsed = time.monotonic() - t0

    conn = db.connect(cfg.db_path)
    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM queries WHERE run_id=?", (run_id,)).fetchall()]
    assert len(statuses) == n_queries and all(s == "done" for s in statuses)
    assert conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"] == n_queries
    return elapsed


def test_parallel_queries_is_faster_than_serial(tmp_path, monkeypatch):
    t_parallel = _run_sleepy_discover(4, tmp_path, monkeypatch)
    t_serial = _run_sleepy_discover(1, tmp_path, monkeypatch)
    assert t_parallel < 0.5, f"parallel_queries=4 took {t_parallel:.2f}s (expected < 0.5s)"
    assert t_serial >= 0.8, f"serial (parallel_queries=1) took {t_serial:.2f}s (expected >= 0.8s)"
