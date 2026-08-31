"""U4.6 registry cross-check tests — canned JSON fixtures, no network."""
import pytest

from leadforge.config import load_config
from leadforge.providers import registry as regmod
from leadforge.providers.registry import CompaniesHouseRegistry, get_registries


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


BIZ = {"id": "b1", "name": "Acme Widgets Ltd", "address_city": "Leeds", "address_postal": "LS1 4AB",
       "address_country": "GB", "address_region": None}

SEARCH = {"items": [{"company_number": "01234567", "title": "ACME WIDGETS LTD",
                     "address": {"locality": "Leeds", "postal_code": "LS1 4AB"}}]}
OFFICERS = {"items": [
    {"name": "SMITH, Jane", "officer_role": "director", "appointed_on": "2019-03-01"},
    {"name": "GONE, Bob", "officer_role": "director", "resigned_on": "2021-01-01"},
]}


def _cfg_with_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.companies_house_key = "test-key"
    return cfg


def test_companies_house_maps_active_officers(tmp_path, monkeypatch):
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    reg = CompaniesHouseRegistry(cfg)
    calls = iter([_Resp(SEARCH), _Resp(OFFICERS)])
    monkeypatch.setattr("httpx.get", lambda *a, **k: next(calls))
    out = reg.lookup(BIZ)
    assert len(out) == 1  # resigned officer excluded
    person, ev = out[0]
    assert person.name == "Smith, Jane"
    assert person.title == "Director"
    assert person.labeled_by == "registry" and person.is_dm == 0
    assert ev.fact == "registry_officer" and "01234567" in ev.url


def test_companies_house_locality_mismatch_returns_empty(tmp_path, monkeypatch):
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    reg = CompaniesHouseRegistry(cfg)
    far = {"items": [{"company_number": "999", "address": {"locality": "Glasgow", "postal_code": "G1 1AA"}}]}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(far))
    assert reg.lookup(BIZ) == []


def test_companies_house_429_disables_without_raising(tmp_path, monkeypatch):
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    reg = CompaniesHouseRegistry(cfg)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp({}, status=429))
    monkeypatch.setattr(regmod.time, "sleep", lambda s: None)
    assert reg.lookup(BIZ) == []
    assert reg.disabled is True
    # once disabled, no further network calls
    monkeypatch.setattr("httpx.get", lambda *a, **k: pytest.fail("network call after disable"))
    assert reg.lookup(BIZ) == []


def test_no_keys_is_silent_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    assert get_registries(cfg) == []


# --- v0.1.1: DM auto-pick from registry -------------------------------------------------------
def _mk_person(name, biz="b1"):
    from leadforge.models import Person
    return Person(business_id=biz, name=name, title="Director", labeled_by="registry")


def _seeded_conn(tmp_path, monkeypatch, names):
    from leadforge import db
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    from leadforge.models import Business
    db.upsert_business(conn, Business(id="b1", run_id="r", name="Acme", source="gosom"))
    for n in names:
        db.add_person(conn, _mk_person(n))
    return conn


def test_auto_pick_single_individual_director(tmp_path, monkeypatch):
    from leadforge import db
    from leadforge.enrich.runner import _auto_pick_registry_dm
    people = [_mk_person("Smith, Jane")]
    conn = _seeded_conn(tmp_path, monkeypatch, [p.name for p in people])
    counts = {}
    _auto_pick_registry_dm(conn, {"id": "b1"}, people, counts)
    rows = db.people_for(conn, "b1")
    assert any(r["is_dm"] == 1 and r["labeled_by"] == "registry" for r in rows)
    assert counts["dm_auto_picked"] == 1
    assert db.dm_pending(conn, 10) == []  # no longer queued for the agent


def test_no_auto_pick_with_two_directors(tmp_path, monkeypatch):
    from leadforge import db
    from leadforge.enrich.runner import _auto_pick_registry_dm
    people = [_mk_person("Smith, Jane"), _mk_person("Doe, John")]
    conn = _seeded_conn(tmp_path, monkeypatch, [p.name for p in people])
    _auto_pick_registry_dm(conn, {"id": "b1"}, people, {})
    assert all(r["is_dm"] == 0 for r in db.people_for(conn, "b1"))


def test_corporate_officer_never_auto_picked(tmp_path, monkeypatch):
    from leadforge import db
    from leadforge.enrich.runner import _auto_pick_registry_dm, _is_corporate_officer
    assert _is_corporate_officer("Churchills London Ltd")
    assert _is_corporate_officer("Some Services LLP")
    assert not _is_corporate_officer("Smith, Jane")
    people = [_mk_person("Churchills London Ltd")]
    conn = _seeded_conn(tmp_path, monkeypatch, [p.name for p in people])
    _auto_pick_registry_dm(conn, {"id": "b1"}, people, {})
    assert all(r["is_dm"] == 0 for r in db.people_for(conn, "b1"))


# --- v0.1.1: OpenCorporates fixture tests -----------------------------------------------------
def test_opencorporates_maps_officers(tmp_path, monkeypatch):
    from leadforge.providers.registry import OpenCorporatesRegistry
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.opencorporates_token = "tok"
    reg = OpenCorporatesRegistry(cfg)
    payload = {"results": {"companies": [{"company": {
        "registered_address_in_full": "1 High St, Leeds LS1 4AB",
        "opencorporates_url": "https://opencorporates.com/companies/gb/01",
        "officers": [
            {"officer": {"name": "jane smith", "position": "director", "start_date": "2019-01-01"}},
            {"officer": {"name": "gone guy", "position": "director", "end_date": "2020-01-01"}},
        ]}}]}}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(payload))
    out = reg.lookup(BIZ)
    assert len(out) == 1
    person, ev = out[0]
    assert person.name == "Jane Smith" and person.labeled_by == "registry"
    assert ev.fact == "registry_officer"


def test_opencorporates_403_disables(tmp_path, monkeypatch):
    from leadforge.providers.registry import OpenCorporatesRegistry
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.opencorporates_token = "tok"
    reg = OpenCorporatesRegistry(cfg)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp({}, status=403))
    assert reg.lookup(BIZ) == []
    assert reg.disabled is True
    monkeypatch.setattr("httpx.get", lambda *a, **k: pytest.fail("network call after disable"))
    assert reg.lookup(BIZ) == []


def test_opencorporates_jurisdiction_code(tmp_path, monkeypatch):
    from leadforge.providers.registry import OpenCorporatesRegistry
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.opencorporates_token = "tok"
    reg = OpenCorporatesRegistry(cfg)
    assert reg._jurisdiction_code({"address_country": "GB", "address_region": None}) == "gb"
    assert reg._jurisdiction_code({"address_country": "US", "address_region": "tx"}) == "us_tx"
    assert reg._jurisdiction_code({"address_country": "US", "address_region": "Texas"}) == ""
