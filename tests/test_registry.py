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
    PROFILE = {"sic_codes": ["45200"], "date_of_creation": "2003-02-01", "company_status": "active"}
    calls = iter([_Resp(SEARCH), _Resp(PROFILE), _Resp(OFFICERS)])
    monkeypatch.setattr("httpx.get", lambda *a, **k: next(calls))
    out, profile = reg.lookup_with_profile(BIZ)
    assert profile["company_number"] == "01234567"
    assert profile["sic_codes"] == ["45200"] and profile["incorporated"] == "2003-02-01"
    assert profile["legal_name"] == "ACME WIDGETS LTD"  # v0.3: profile carries the registry's own title
    assert profile["match_similarity"] >= 0.45  # >= min_name_similarity, else it would never have been accepted
    assert len(out) == 1  # resigned officer excluded
    person, ev = out[0]
    assert person.name == "Jane Smith"  # 'SMITH, Jane' registry order flipped for the call sheet
    assert person.title == "Director"
    assert person.labeled_by == "registry" and person.is_dm == 0
    assert ev.fact == "registry_officer" and "01234567" in ev.url


def test_companies_house_unrelated_name_same_locality_rejected(tmp_path, monkeypatch):
    """v0.3: a same-locality hit whose name shares nothing with the business is worse than no match
    (measured 7-10% wrong-company matches) — rejected purely on name_similarity, before any profile
    or officers call is even made (only the search call happens)."""
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    reg = CompaniesHouseRegistry(cfg)
    biz = {"id": "b2", "name": "Osman Motors", "address_city": "Leeds", "address_postal": "LS1 4AB",
           "address_country": "GB", "address_region": None}
    unrelated = {"items": [{"company_number": "999888", "title": "ACME PROPERTY GROUP LIMITED",
                            "address": {"locality": "Leeds", "postal_code": "LS1 4AB"}}]}
    calls = []
    monkeypatch.setattr("httpx.get", lambda *a, **k: (calls.append(1), _Resp(unrelated))[1])
    out, profile = reg.lookup_with_profile(biz)
    assert out == [] and profile is None
    assert len(calls) == 1  # rejected purely from the search hit — no profile or officers call


def test_companies_house_dissolved_company_rejected(tmp_path, monkeypatch):
    """v0.3: cfg.registry.active_only (default True) rejects a dissolved/liquidated match even when
    the name and locality both match — known here straight from the search hit's own company_status."""
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    assert cfg.registry.active_only is True
    reg = CompaniesHouseRegistry(cfg)
    dissolved = {"items": [{"company_number": "01234567", "title": "ACME WIDGETS LTD",
                            "company_status": "dissolved",
                            "address": {"locality": "Leeds", "postal_code": "LS1 4AB"}}]}
    calls = []
    monkeypatch.setattr("httpx.get", lambda *a, **k: (calls.append(1), _Resp(dissolved))[1])
    out, profile = reg.lookup_with_profile(BIZ)
    assert out == [] and profile is None
    assert len(calls) == 1  # rejected from the search hit alone — no profile/officers call either


def test_companies_house_dissolved_status_only_known_from_profile(tmp_path, monkeypatch):
    """When the search hit itself carries no company_status, the gate falls back to fetching the
    profile (as documented) — still rejected, still no officers call. A 3rd (officers) response IS
    queued so that a broken gate would actually surface real officers rather than accidentally
    hitting an exhausted-iterator StopIteration that happens to look like a correct rejection."""
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    reg = CompaniesHouseRegistry(cfg)
    PROFILE_DISSOLVED = {"sic_codes": [], "date_of_creation": "2003-02-01", "company_status": "dissolved"}
    responses = iter([_Resp(SEARCH), _Resp(PROFILE_DISSOLVED), _Resp(OFFICERS)])
    seen = []

    def fake_get(*a, **k):
        seen.append(1)
        return next(responses)

    monkeypatch.setattr("httpx.get", fake_get)
    out, profile = reg.lookup_with_profile(BIZ)
    assert out == [] and profile is None
    assert len(seen) == 2  # search + profile only — the queued officers response was never consumed


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


_ACTIVE_PROFILE = {"company_number": "1", "company_status": "active", "legal_name": "x", "match_similarity": 1.0}


def test_auto_pick_single_individual_director(tmp_path, monkeypatch):
    from leadforge import db
    from leadforge.enrich.runner import _auto_pick_registry_dm
    people = [_mk_person("Smith, Jane")]
    conn = _seeded_conn(tmp_path, monkeypatch, [p.name for p in people])
    counts = {}
    _auto_pick_registry_dm(conn, {"id": "b1"}, people, counts, _ACTIVE_PROFILE)
    rows = db.people_for(conn, "b1")
    assert any(r["is_dm"] == 1 and r["labeled_by"] == "registry" for r in rows)
    assert counts["dm_auto_picked"] == 1
    assert db.dm_pending(conn, 10) == []  # no longer queued for the agent


def test_no_auto_pick_with_two_directors(tmp_path, monkeypatch):
    from leadforge import db
    from leadforge.enrich.runner import _auto_pick_registry_dm
    people = [_mk_person("Smith, Jane"), _mk_person("Doe, John")]
    conn = _seeded_conn(tmp_path, monkeypatch, [p.name for p in people])
    _auto_pick_registry_dm(conn, {"id": "b1"}, people, {}, _ACTIVE_PROFILE)
    assert all(r["is_dm"] == 0 for r in db.people_for(conn, "b1"))


def test_corporate_officer_never_auto_picked(tmp_path, monkeypatch):
    from leadforge import db
    from leadforge.enrich.runner import _auto_pick_registry_dm, _is_corporate_officer
    assert _is_corporate_officer("Churchills London Ltd")
    assert _is_corporate_officer("Some Services LLP")
    assert not _is_corporate_officer("Smith, Jane")
    people = [_mk_person("Churchills London Ltd")]
    conn = _seeded_conn(tmp_path, monkeypatch, [p.name for p in people])
    _auto_pick_registry_dm(conn, {"id": "b1"}, people, {}, _ACTIVE_PROFILE)
    assert all(r["is_dm"] == 0 for r in db.people_for(conn, "b1"))


def test_no_auto_pick_without_active_profile(tmp_path, monkeypatch):
    """v0.3: a single individual director is not enough on its own — the profile must also say the
    company is active. Belt-and-braces on top of the registry's own active_only gate."""
    from leadforge import db
    from leadforge.enrich.runner import _auto_pick_registry_dm
    people = [_mk_person("Smith, Jane")]
    conn = _seeded_conn(tmp_path, monkeypatch, [p.name for p in people])
    _auto_pick_registry_dm(conn, {"id": "b1"}, people, {})  # no profile at all
    assert all(r["is_dm"] == 0 for r in db.people_for(conn, "b1"))
    _auto_pick_registry_dm(conn, {"id": "b1"}, people, {}, {"company_status": "dissolved"})
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


def test_opencorporates_unrelated_name_rejected(tmp_path, monkeypatch):
    """v0.3: the same two gates apply where OpenCorporates' payload carries the data — a 'name' field
    lets the similarity gate run; an unrelated name at the same locality is rejected."""
    from leadforge.providers.registry import OpenCorporatesRegistry
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.opencorporates_token = "tok"
    reg = OpenCorporatesRegistry(cfg)
    payload = {"results": {"companies": [{"company": {
        "name": "Totally Different Enterprises",
        "registered_address_in_full": "1 High St, Leeds LS1 4AB",
        "opencorporates_url": "https://opencorporates.com/companies/gb/01",
        "officers": [{"officer": {"name": "jane smith", "position": "director"}}]}}]}}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(payload))
    assert reg.lookup(BIZ) == []


def test_opencorporates_dissolved_rejected(tmp_path, monkeypatch):
    """v0.3: active_only applies to OpenCorporates' current_status field when present."""
    from leadforge.providers.registry import OpenCorporatesRegistry
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.opencorporates_token = "tok"
    reg = OpenCorporatesRegistry(cfg)
    payload = {"results": {"companies": [{"company": {
        "name": "Acme Widgets Ltd", "current_status": "Dissolved",
        "registered_address_in_full": "1 High St, Leeds LS1 4AB",
        "opencorporates_url": "https://opencorporates.com/companies/gb/01",
        "officers": [{"officer": {"name": "jane smith", "position": "director"}}]}}]}}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(payload))
    assert reg.lookup(BIZ) == []


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


def test_registry_stage_covers_siteless_businesses(tmp_path, monkeypatch):
    """v0.1.2: businesses with no website must still get registry officers + profile."""
    from leadforge import db
    from leadforge.enrich.runner import _registry_stage
    from leadforge.models import Business, Evidence, Person
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.companies_house_key = "k"
    conn = db.connect(cfg.db_path)
    db.upsert_business(conn, Business(id="b1", run_id="r", name="No Site Garage", source="gosom",
                                      address_country="GB"))  # no website/domain
    from leadforge.providers import registry as regmod

    def fake_lwp(self, b):
        person = Person(business_id=b["id"], name="Smith, Jane", title="Director", labeled_by="registry")
        ev = Evidence(business_id=b["id"], ref_table="people", fact="registry_officer", url="u", snippet="s")
        return [(person, ev)], {"company_number": "999", "incorporated": "2010-01-01",
                                "company_status": "active", "legal_name": "NO SITE GARAGE LTD",
                                "sic_codes": ["45200"]}

    monkeypatch.setattr(regmod.CompaniesHouseRegistry, "lookup_with_profile", fake_lwp)
    counts = {}
    _registry_stage(conn, cfg, counts)
    people = db.people_for(conn, "b1")
    assert people and people[0]["labeled_by"] == "registry"
    assert any(p["is_dm"] == 1 for p in people)  # single director auto-picked
    import json as _json
    enrich = _json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()[0])
    assert enrich["registry_profile"]["company_number"] == "999"
    assert enrich["registry_checked"] is True
    # second pass is a no-op (registry_checked)
    monkeypatch.setattr(regmod.CompaniesHouseRegistry, "lookup_with_profile",
                        lambda self, b: pytest.fail("re-looked-up a checked business"))
    _registry_stage(conn, cfg, {})
