"""v0.3 ADR-013: register providers (dvsa) get their own planned queries, one per area, routed to that
provider alone — never used as a fallback for Maps, never consuming a Maps query."""

from __future__ import annotations

import json

from leadforge import db
from leadforge.grid import PlannedQuery, build_plan, plan_counts
from leadforge.models import RawListing
from leadforge.pipeline import _plan_into_db, run_discover
from leadforge.providers import base as pbase

# import the real providers first so their @register has already run — otherwise get_chain(only=...)
# imports them lazily during the test and overwrites the monkeypatched fakes
from leadforge.providers import dvsa as _real_dvsa  # noqa: F401
from leadforge.providers import gosom as _real_gosom  # noqa: F401
from leadforge.util import now_iso


def _icp(sample_icp):
    geo = sample_icp.target.geography.model_copy(update={"areas": ["Leeds", "Bristol"], "grid": "off"})
    return sample_icp.model_copy(update={"target": sample_icp.target.model_copy(update={"geography": geo})})


def test_build_plan_adds_one_register_query_per_area_first(cfg, sample_icp):
    cfg.discovery.providers = ["gosom", "dvsa"]
    cfg.discovery.grid_mode = "off"
    qs = build_plan(_icp(sample_icp), cfg)
    reg = [q for q in qs if q.provider]
    assert [q.provider for q in reg] == ["dvsa", "dvsa"]
    assert [q.area for q in reg] == ["Leeds", "Bristol"]
    assert qs[:2] == reg, "register queries come first"
    assert all(q.tile is None for q in reg)
    counts = plan_counts(qs, cfg)
    assert counts["registry_queries"] == 2 and counts["untiled_queries"] == 2
    assert counts["est_max_results"] == 2 * 120  # registers are not capped by Google's ceiling


def test_no_register_queries_when_provider_not_configured(cfg, sample_icp):
    cfg.discovery.providers = ["gosom"]
    cfg.discovery.grid_mode = "off"
    assert all(q.provider is None for q in build_plan(_icp(sample_icp), cfg))


class _Fake(pbase.DiscoveryProvider):
    name = "fake"
    calls: list[str] = []

    def available(self):
        return True, "fake"

    def fetch(self, query: PlannedQuery, limit=None):
        type(self).calls.append(f"{type(self).name}:{query.text}")
        phone = "+441132000000" if type(self).name == "dvsa" else "+441132000001"
        return [RawListing(provider=type(self).name, fetched_at=now_iso(), data={
            "title": f"{type(self).name} {query.text}", "phone": phone,
            "address": "1 High St, Leeds, LS1 1AA",
            "complete_address": {"street": "1 High St", "city": "Leeds", "postal_code": "LS1 1AA", "country": "United Kingdom"}})]


def test_register_query_is_routed_to_its_provider_only(cfg, sample_icp, monkeypatch, tmp_path):
    class FakeGosom(_Fake):
        name = "gosom"
        calls = []

    class FakeDvsa(_Fake):
        name = "dvsa"
        calls = []

    monkeypatch.setitem(pbase.PROVIDERS, "gosom", FakeGosom)
    monkeypatch.setitem(pbase.PROVIDERS, "dvsa", FakeDvsa)
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)
    cfg.discovery.providers = ["gosom", "dvsa"]
    cfg.discovery.grid_mode = "off"
    icp = _icp(sample_icp)
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, "icp.yaml", icp.icp_hash())
    _plan_into_db(conn, cfg, icp, run_id)
    markers = [json.loads(r["tile_json"]) for r in conn.execute("SELECT tile_json FROM queries WHERE tile_json IS NOT NULL")]
    assert markers == [{"provider": "dvsa"}, {"provider": "dvsa"}]
    run_discover(cfg, icp, tmp_path / "icp.yaml", run_id=run_id)
    assert len(FakeDvsa.calls) == 2, FakeDvsa.calls           # one register call per area
    assert all("dvsa" not in c for c in FakeGosom.calls)      # gosom never saw the register queries
    assert len(FakeGosom.calls) == 2                           # and ran its own (1 category x 2 areas)
    statuses = [r["status"] for r in conn.execute("SELECT status FROM queries ORDER BY id")]
    assert statuses == ["done"] * 4


def test_register_provider_is_never_a_fallback_for_a_maps_query(cfg, sample_icp, monkeypatch, tmp_path):
    from leadforge.util import ProviderDegraded

    class DegradedGosom(_Fake):
        name = "gosom"
        calls = []

        def fetch(self, query, limit=None):
            type(self).calls.append(query.text)
            raise ProviderDegraded("captcha")

    class FakeDvsa(_Fake):
        name = "dvsa"
        calls = []

    monkeypatch.setitem(pbase.PROVIDERS, "gosom", DegradedGosom)
    monkeypatch.setitem(pbase.PROVIDERS, "dvsa", FakeDvsa)
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)
    cfg.discovery.providers = ["gosom", "dvsa"]
    cfg.discovery.grid_mode = "off"
    icp = _icp(sample_icp)
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, "icp.yaml", icp.icp_hash())
    _plan_into_db(conn, cfg, icp, run_id)
    run_discover(cfg, icp, tmp_path / "icp.yaml", run_id=run_id)
    # the register answered ONLY its own two queries; the two degraded Maps queries stayed degraded
    assert len(FakeDvsa.calls) == 2
    statuses = [r["status"] for r in conn.execute("SELECT status FROM queries ORDER BY id")]
    assert statuses == ["done", "done", "degraded", "degraded"]
