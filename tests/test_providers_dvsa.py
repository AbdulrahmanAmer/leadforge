"""Tests for the DVSA register provider (v0.3 unit B, docs/09).

No network: the module-level downloader (`dvsa._download_csv`) is monkeypatched to read the fixture
CSV file's real bytes (cp1252, one 0x92 byte). A counter on that monkeypatch proves the cache is
honored across a second fetch and that a stale cache triggers exactly one re-download.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from leadforge.config import load_config
from leadforge.grid import PlannedQuery
from leadforge.models import RawListing
from leadforge.normalize import to_business
from leadforge.providers import dvsa
from leadforge.providers.base import get_field_map
from leadforge.providers.dvsa import DvsaProvider
from leadforge.util import ProviderDegraded

FIXTURE = Path(__file__).parent / "fixtures" / "dvsa_sample.csv"


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _counting_downloader(counter: list[int]):
    def _fake(cfg):
        counter[0] += 1
        return _fixture_bytes()
    return _fake


def _mock_httpx_client(handler):
    """Monkeypatch target for `dvsa.httpx.Client`: forces every Client this module constructs onto
    an httpx.MockTransport, so `_download_csv`'s real request-building path (not just its return
    value) runs against the fixture instead of the network."""
    real_client_cls = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    return _factory


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


# --- httpx download path (real Client, MockTransport) -- reviewer finding 1 -----------------------

def test_dvsa_httpx_200_writes_cache_and_second_fetch_makes_no_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    calls = [0]

    def handler(request):
        calls[0] += 1
        return httpx.Response(200, content=_fixture_bytes())

    monkeypatch.setattr(dvsa.httpx, "Client", _mock_httpx_client(handler))
    prov = DvsaProvider(cfg)
    query = PlannedQuery(text="x in Leeds", category="", area="Leeds")

    out = prov.fetch(query)
    assert len(out) == 3
    assert calls[0] == 1
    path = dvsa._cache_path(cfg)
    assert path.exists()
    assert path.read_bytes() == _fixture_bytes()

    prov.fetch(query)
    assert calls[0] == 1  # cache is warm: the second fetch made no HTTP request


def test_dvsa_httperror_with_stale_cache_falls_back_and_warns(tmp_path, monkeypatch, caplog):
    import os
    import time

    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    path = dvsa._cache_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_fixture_bytes())
    old = time.time() - 200 * 86400  # well past the default 90-day refresh window
    os.utime(path, (old, old))

    def handler(request):
        return httpx.Response(503, content=b"upstream unavailable")

    monkeypatch.setattr(dvsa.httpx, "Client", _mock_httpx_client(handler))
    prov = DvsaProvider(cfg)
    with caplog.at_level(logging.WARNING, logger="leadforge"):
        out = prov.fetch(PlannedQuery(text="x in Leeds", category="", area="Leeds"))

    assert len(out) == 3  # served from the stale cache, not dropped
    assert any("stale cache" in r.message for r in caplog.records)


def test_dvsa_httperror_with_no_cache_raises_provider_degraded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)

    def handler(request):
        return httpx.Response(500, content=b"boom")

    monkeypatch.setattr(dvsa.httpx, "Client", _mock_httpx_client(handler))
    prov = DvsaProvider(cfg)
    with pytest.raises(ProviderDegraded):
        prov.fetch(PlannedQuery(text="x in Leeds", category="", area="Leeds"))
    assert not dvsa._cache_path(cfg).exists()


# --- header validation -- reviewer finding 2 -------------------------------------------------------

def test_dvsa_bad_header_raises_and_does_not_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    bad_csv = b"Foo,Bar\n1,2\n"

    def handler(request):
        return httpx.Response(200, content=bad_csv)

    monkeypatch.setattr(dvsa.httpx, "Client", _mock_httpx_client(handler))
    prov = DvsaProvider(cfg)
    with pytest.raises(ProviderDegraded) as exc_info:
        prov.fetch(PlannedQuery(text="x in Leeds", category="", area="Leeds"))
    assert cfg.discovery.dvsa.url in str(exc_info.value)
    assert not dvsa._cache_path(cfg).exists()  # a bad header must never be cached


# --- title-casing -- reviewer finding 3 -------------------------------------------------------------

def test_dvsa_title_case_m_and_t_transmissions():
    assert dvsa._title_case_name("M AND T TRANSMISSIONS LIMITED") == "M and T Transmissions Limited"


def test_dvsa_title_case_abs_mot_station_ltd():
    assert dvsa._title_case_name("A B S MOT STATION LTD") == "A B S MOT Station Ltd"


def test_dvsa_title_case_strips_stray_parentheses():
    assert dvsa._title_case_name("SPEEDY (MOT) CENTRE") == "Speedy MOT Centre"


def test_dvsa_title_case_llp_stays_upper_no_mapping():
    assert dvsa._title_case_name("ACME GARAGE LLP") == "Acme Garage LLP"


# --- available(): no mkdir, no raise -- reviewer finding 4 ------------------------------------------

def test_dvsa_available_does_not_create_cache_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    ok, reason = DvsaProvider(cfg).available()
    assert ok is True
    assert reason == "will download"
    assert not dvsa._cache_path(cfg).parent.exists()  # available() must be side-effect-free


# --- locality parse: last ' in ' segment, zero-match warning -- reviewer finding 5 -------------------

def test_dvsa_locality_uses_last_in_segment():
    query = PlannedQuery(
        text="Car repair shops in the Speedy Trading Estate in Bristol", category="", area="",
    )
    assert dvsa._locality_from_query(query) == "Bristol"


def test_dvsa_zero_matches_logs_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader([0]))
    prov = DvsaProvider(cfg)
    with caplog.at_level(logging.WARNING, logger="leadforge"):
        out = prov.fetch(PlannedQuery(text="x in Nonexistentville", category="", area="Nonexistentville"))
    assert out == []
    assert any("0 rows matched locality" in r.message for r in caplog.records)


# --- rows memoization keyed by (path, mtime) -- reviewer finding 6 ----------------------------------

def test_dvsa_rows_memoized_across_fetches_same_mtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader([0]))
    prov = DvsaProvider(cfg)
    query = PlannedQuery(text="x in Leeds", category="", area="Leeds")
    prov.fetch(query)  # first parse: populates the module-level rows cache

    parse_calls = [0]
    real_dict_reader = dvsa.csv.DictReader

    def counting_dict_reader(*a, **k):
        parse_calls[0] += 1
        return real_dict_reader(*a, **k)

    monkeypatch.setattr(dvsa.csv, "DictReader", counting_dict_reader)
    prov.fetch(query)
    prov.fetch(query)
    assert parse_calls[0] == 0  # cache hit both times: the CSV text was never re-parsed


def test_dvsa_rows_reparsed_after_cache_file_changes_mtime(tmp_path, monkeypatch):
    """The memo key includes mtime, so a refreshed cache file (new mtime) is re-parsed rather than
    silently serving stale rows from the old file."""
    import os
    import time

    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dvsa, "_download_csv", _counting_downloader([0]))
    prov = DvsaProvider(cfg)
    query = PlannedQuery(text="x in Leeds", category="", area="Leeds")
    prov.fetch(query)

    path = dvsa._cache_path(cfg)
    os.utime(path, (time.time() + 5, time.time() + 5))  # simulate a re-download's new mtime

    parse_calls = [0]
    real_dict_reader = dvsa.csv.DictReader

    def counting_dict_reader(*a, **k):
        parse_calls[0] += 1
        return real_dict_reader(*a, **k)

    monkeypatch.setattr(dvsa.csv, "DictReader", counting_dict_reader)
    prov.fetch(query)
    assert parse_calls[0] == 1  # different mtime: re-parsed, not served from the stale memo entry
