"""Tests for the DVSA register provider (v0.3 unit B, docs/09).

No network: the module-level downloader (`dvsa._download_csv`) is monkeypatched to read the fixture
CSV file's real bytes (cp1252, one 0x92 byte). A counter on that monkeypatch proves the cache is
honored across a second fetch and that a stale cache triggers exactly one re-download.
"""
from __future__ import annotations

from pathlib import Path

from leadforge.config import load_config
from leadforge.grid import PlannedQuery
from leadforge.models import RawListing
from leadforge.normalize import to_business
from leadforge.providers import dvsa
from leadforge.providers.base import get_field_map
from leadforge.providers.dvsa import DvsaProvider

FIXTURE = Path(__file__).parent / "fixtures" / "dvsa_sample.csv"


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _counting_downloader(counter: list[int]):
    def _fake(cfg):
        counter[0] += 1
        return _fixture_bytes()
    return _fake


# --- field map registration --------------------------------------------------------------------

def test_dvsa_field_map_registered():
    fmap = get_field_map("dvsa")
    assert fmap is not None
    assert fmap["name"] == ["name"]
    assert fmap["phone"] == ["phone"]
    assert fmap["website"] == ["website"]
    assert fmap["place_id"] == ["place_id"]
    assert fmap["maps_url"] == ["maps_url"]


# --- available() ---------------------------------------------------------------------------------

def test_dvsa_available_false_when_url_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.discovery.dvsa.url = ""
    ok, reason = DvsaProvider(cfg).available()
    assert ok is False
    assert "empty" in reason


def test_dvsa_available_true_without_network_when_no_cache(tmp_path, monkeypatch):
    """available() must never touch the network — only fetch() downloads lazily."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("available() must not download")

    monkeypatch.setattr(dvsa, "_download_csv", _boom)
    ok, reason = DvsaProvider(cfg).available()
    assert ok is True
    assert reason == "will download"


def test_dvsa_available_reports_cached_date(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    counter = [0]
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader(counter))
    DvsaProvider(cfg).fetch(PlannedQuery(text="MOT test station in Leeds, West Yorkshire", category="", area="Leeds, West Yorkshire"))
    ok, reason = DvsaProvider(cfg).available()
    assert ok is True
    assert reason.startswith("cached ")


# --- fetch(): locality filter, title-casing, cp1252 decode ---------------------------------------

def test_dvsa_fetch_yields_exactly_leeds_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    counter = [0]
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader(counter))
    prov = DvsaProvider(cfg)
    query = PlannedQuery(text="MOT test station in Leeds, West Yorkshire", category="", area="Leeds, West Yorkshire")
    out = prov.fetch(query)

    assert len(out) == 3
    assert all(isinstance(x, RawListing) and x.provider == "dvsa" for x in out)
    names = {x.data["name"] for x in out}
    assert names == {"Speedy MOT Centre", "M and T Transmissions Limited", "Northside Tyres and Exhausts"}
    # 'MOT' (<=3 chars, all caps, not a stopword) stays upper; 'AND' (a stopword) is lowercased.
    assert "Speedy MOT Centre" in names
    assert "M and T Transmissions Limited" in names

    for listing in out:
        assert listing.data["category"] == "MOT test station"
        assert listing.data["source_register"] == "dvsa"
        assert listing.data["complete_address"]["country"] == "United Kingdom"
        assert listing.data["complete_address"]["city"] == "Leeds"
        assert listing.data["website"] is None
        assert listing.data["place_id"] is None
        assert listing.data["maps_url"] is None
        assert "enrich" in listing.data and "dvsa" in listing.data["enrich"]
        assert listing.data["enrich"]["dvsa"]["site_number"]


def test_dvsa_cp1252_byte_decodes_correctly(tmp_path, monkeypatch):
    """The live file carries 0x92 (cp1252 right single quote) — must decode to U+2019, not mojibake."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    counter = [0]
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader(counter))
    prov = DvsaProvider(cfg)
    out = prov.fetch(PlannedQuery(text="x in Leeds", category="", area="Leeds"))
    row = next(x for x in out if x.data["name"] == "Northside Tyres and Exhausts")
    assert "’" in row.data["complete_address"]["street"]
    assert "St James’s Court" in row.data["complete_address"]["street"]


def test_dvsa_categories_include_enabled_classes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader([0]))
    prov = DvsaProvider(cfg)
    out = prov.fetch(PlannedQuery(text="x in Leeds", category="", area="Leeds"))
    speedy = next(x for x in out if x.data["name"] == "Speedy MOT Centre")
    assert speedy.data["categories"] == ["MOT test station", "MOT class 4"]
    northside = next(x for x in out if x.data["name"] == "Northside Tyres and Exhausts")
    assert northside.data["categories"] == ["MOT test station", "MOT class 4", "MOT class 7"]


def test_dvsa_respects_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader([0]))
    prov = DvsaProvider(cfg)
    out = prov.fetch(PlannedQuery(text="x in Leeds", category="", area="Leeds"), limit=2)
    assert len(out) == 2


def test_dvsa_locality_parsed_from_text_when_area_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader([0]))
    prov = DvsaProvider(cfg)
    out = prov.fetch(PlannedQuery(text="MOT test station in Sheffield, South Yorkshire", category="", area=""))
    assert len(out) == 2
    assert {x.data["name"] for x in out} == {"Sheffield Auto Care", "Steel City MOT"}


def test_dvsa_bristol_locality_single_row(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader([0]))
    prov = DvsaProvider(cfg)
    out = prov.fetch(PlannedQuery(text="x in Bristol", category="", area="Bristol"))
    assert len(out) == 1
    assert out[0].data["name"] == "West Country Garage Services Limited"


# --- normalize.to_business integration (phone -> E.164) ------------------------------------------

def test_dvsa_row_normalizes_to_e164_phone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader([0]))
    prov = DvsaProvider(cfg)
    out = prov.fetch(PlannedQuery(text="x in Leeds", category="", area="Leeds"))
    speedy = next(x for x in out if x.data["name"] == "Speedy MOT Centre")
    biz = to_business(speedy, "run_x", None, "GB")
    assert biz is not None
    assert biz.phone_e164 is not None
    assert biz.phone_e164.startswith("+44")
    assert biz.category == "MOT test station"
    assert biz.address_city == "Leeds"


# --- caching: no network on a warm cache; a stale cache re-downloads exactly once -----------------

def test_dvsa_second_fetch_uses_cache_no_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    counter = [0]
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader(counter))
    prov = DvsaProvider(cfg)
    query = PlannedQuery(text="x in Leeds", category="", area="Leeds")

    prov.fetch(query)
    assert counter[0] == 1
    prov.fetch(query)
    assert counter[0] == 1  # warm cache: no second download


def test_dvsa_stale_cache_redownloads(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.discovery.dvsa.refresh_days = 1
    counter = [0]
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader(counter))
    prov = DvsaProvider(cfg)
    query = PlannedQuery(text="x in Leeds", category="", area="Leeds")

    prov.fetch(query)
    assert counter[0] == 1

    # Push the cached file's mtime back past refresh_days.
    path = dvsa._cache_path(cfg)
    old = time.time() - (2 * 86400)
    os.utime(path, (old, old))

    prov.fetch(query)
    assert counter[0] == 2  # stale cache triggered exactly one re-download
