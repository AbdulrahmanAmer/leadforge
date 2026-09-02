"""Unit H: company mode (docs/09 Wave 2 H) — GainLev's own client pipeline on Companies House.

No network: httpx.get and SiteCrawler.crawl are monkeypatched throughout.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from leadforge import db
from leadforge.config import load_config
from leadforge.models import ICP, RawListing
from leadforge.util import now_iso

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ch_advanced_search.json").read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def _cfg_with_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.companies_house_key = "test-key"
    return cfg


def _company_icp(**over):
    body = {
        "campaign": "gainlev-own-pipeline", "offer": {"what": "outsourced outreach VAs"},
        "target": {"mode": "company", "categories": [], "sic_codes": ["70229", "62012"],
                  "geography": {"areas": ["Stockport, Greater Manchester"], "country": "GB"}},
        "scoring": {"profile": "company"}, "compliance": {"region_profile": "uk"},
    }
    body.update(over)
    return ICP.model_validate(body)


# ---------------------------------------------------------------------- provider / discovery
def test_advanced_search_maps_to_raw_listings_82200_excluded(tmp_path, monkeypatch):
    from leadforge.grid import PlannedQuery
    from leadforge.providers.companies_house import CompaniesHouseDiscovery

    cfg = _cfg_with_key(tmp_path, monkeypatch)
    prov = CompaniesHouseDiscovery(cfg)
    monkeypatch.setattr("leadforge.providers.companies_house.httpx.get",
                        lambda *a, **k: _Resp(FIXTURE))
    out = prov.fetch(PlannedQuery(text="sic:70229 loc:Stockport, Greater Manchester", category="70229",
                                  area="Stockport, Greater Manchester"))
    # 3 items in the fixture: one clean match, one 82200-only (excluded), one dissolved (excluded)
    assert len(out) == 1
    assert all(isinstance(r, RawListing) for r in out)
    row = out[0]
    assert row.provider == "companies_house"
    assert row.data["company_number"] == "01234567"
    assert row.data["name"] == "ACME CONSULTING LIMITED"
    assert row.data["sic_codes"] == ["70229"]
    assert "82200" not in [c for r in out for c in r.data["sic_codes"]]


def test_available_requires_key(tmp_path, monkeypatch):
    from leadforge.providers.companies_house import CompaniesHouseDiscovery
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    ok, reason = CompaniesHouseDiscovery(cfg).available()
    assert ok is False and "companies_house_key" in reason


def test_raw_listing_maps_to_business_with_registry_profile_via_enrich_for(tmp_path, monkeypatch):
    """to_business() must accept companies_house's data shape (no per-provider field-map registry
    exists in this build — normalize.py's fallbacks are what make this work), and enrich_for() must
    produce the registry_profile the export sheet's Company No/Incorporated/SIC Codes columns read."""
    from leadforge.normalize import to_business
    from leadforge.providers.companies_house import CompaniesHouseDiscovery, enrich_for

    icp = _company_icp()
    raw = RawListing(provider="companies_house", fetched_at="2026-09-02T00:00:00Z", data={
        "name": "ACME CONSULTING LIMITED", "address": "1 High Street, Stockport, SK1 1AA, United Kingdom",
        "complete_address": {"street": "1 High Street", "city": "Stockport", "state": "Greater Manchester",
                             "postal_code": "SK1 1AA", "country": "United Kingdom"},
        "category": "Management consultancy activities other than financial management",
        "categories": ["Management consultancy activities other than financial management"],
        "company_number": "01234567", "company_status": "active", "company_type": "ltd",
        "incorporated": "2019-06-15", "sic_codes": ["70229"],
    })
    biz = to_business(raw, "run_x", icp, "GB")
    assert biz is not None
    assert biz.name == "ACME CONSULTING LIMITED"
    assert biz.address_city == "Stockport"
    assert biz.address_postal == "SK1 1AA"
    assert biz.source == "companies_house"

    enrich = CompaniesHouseDiscovery.enrich_for(raw.data)
    assert enrich == enrich_for(raw.data)  # module-level and class-attached versions agree
    assert enrich["registry_checked"] is True
    assert enrich["registry_profile"]["company_number"] == "01234567"
    assert enrich["registry_profile"]["sic_codes"] == ["70229"]
    assert enrich["registry_profile"]["match_similarity"] == 1.0


def test_run_discover_routes_companies_house_query_and_applies_enrich_for(cfg, monkeypatch):
    """End-to-end through pipeline.run_discover: a query marked provider="companies_house" must be
    routed ONLY to that provider (never the Maps fallback chain), and the returned listing's
    enrich_for() hook (pipeline.py's `providers_base.PROVIDERS.get(raw.provider)` lookup) must land
    in the persisted business's enrich_json — this is what enrich/runner.py's registry stage and the
    export sheet's Company No/Incorporated/SIC Codes columns both read."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    import leadforge.providers.companies_house  # noqa: F401 — real @register runs first (see docs/09)
    from leadforge.providers.base import DiscoveryProvider, register

    icp = _company_icp()
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    @register  # overwrites the real "companies_house" registration for this test only
    class FakeCH(DiscoveryProvider):
        name = "companies_house"
        supports_tiles = False

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            assert query.text.startswith("sic:")  # only the register query reaches this provider
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "name": "ACME CONSULTING LIMITED",
                "complete_address": {"street": "1 High Street", "city": "Stockport",
                                     "postal_code": "SK1 1AA", "country": "United Kingdom"},
                "category": "Management consultancy", "categories": ["Management consultancy"],
                "company_number": "01234567", "company_status": "active",
                "incorporated": "2019-06-15", "sic_codes": ["70229"],
            })]

    FakeCH.enrich_for = staticmethod(lambda raw_data: {
        "registry_profile": {"company_number": raw_data["company_number"], "match_similarity": 1.0},
        "registry_checked": True,
    })

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    db.add_queries(conn, run_id, [
        ("sic:70229,62012 loc:Stockport, Greater Manchester", {"provider": "companies_house"})])

    from leadforge.pipeline import run_discover
    run_discover(cfg, icp, icp_path, run_id=run_id)

    conn = db.connect(cfg.db_path)
    rows = db.all_businesses(conn)
    assert len(rows) == 1
    assert rows[0]["source"] == "companies_house"
    enrich = json.loads(rows[0]["enrich_json"])
    assert enrich["registry_checked"] is True
    assert enrich["registry_profile"]["company_number"] == "01234567"


# ---------------------------------------------------------------------- planning
def test_shard_plan_one_area_seven_codes_is_two_queries(cfg):
    from leadforge.company import build_company_plan

    icp = _company_icp(target={
        "mode": "company", "categories": [],
        "sic_codes": ["11111", "22222", "33333", "44444", "55555", "66666", "77777"],
        "geography": {"areas": ["Stockport, Greater Manchester"], "country": "GB"},
    })
    qs = build_company_plan(icp, cfg)
    assert len(qs) == 2
    assert all(q.tile is None for q in qs)
    assert qs[0].text.startswith("sic:11111,22222,33333,44444,55555 loc:")
    assert qs[1].text.startswith("sic:66666,77777 loc:")


def test_grid_build_plan_dispatches_to_company_plan(cfg):
    from leadforge.grid import build_plan

    icp = _company_icp()
    qs = build_plan(icp, cfg)
    assert len(qs) == 1  # one area, one shard (<=5 codes)
    assert qs[0].text == "sic:70229,62012 loc:Stockport, Greater Manchester"


# ---------------------------------------------------------------------- domain resolution
class _Page:
    def __init__(self, text, html=""):
        self.text = text
        self.html = html or text
        self.url = "https://example.co.uk"


class _CrawlResult:
    def __init__(self, ok=True, pages=None):
        self.ok = ok
        self.pages = pages or []


class _FakeCrawler:
    """Only the domains in `hits` "resolve"; everything else looks unreachable."""

    def __init__(self, hits: dict[str, str]):
        self.hits = hits
        self.closed = False

    def crawl(self, url):
        for domain, text in self.hits.items():
            if domain in url:
                return _CrawlResult(ok=True, pages=[_Page(text)])
        return _CrawlResult(ok=False, pages=[])

    def close(self):
        self.closed = True


def test_resolve_verifies_postcode_match_and_rejects_a_page_without_it(tmp_path, monkeypatch, cfg):
    from leadforge.enrich import resolve_domain
    from leadforge.models import Business

    conn = db.connect(cfg.db_path)
    biz = Business(id="b1", name="ACME CONSULTING LIMITED", source="companies_house",
                   address_postal="SK1 1AA", enrich={"registry_profile": {"company_number": "01234567"}})
    db.upsert_business(conn, biz)
    row = conn.execute("SELECT * FROM businesses WHERE id='b1'").fetchone()

    # candidate "acmeconsulting.co.uk" is tried first (joined slug, .co.uk first) and its fixture page
    # DOES carry the registered postcode -> match.
    fake = _FakeCrawler({"acmeconsulting.co.uk": "Welcome to Acme Consulting. Find us at SK1 1AA, UK."})
    monkeypatch.setattr("leadforge.enrich.crawler.SiteCrawler", lambda cfg_: fake)
    result = resolve_domain.resolve(conn, cfg, row)
    assert result is not None
    assert result["domain"] == "acmeconsulting.co.uk"
    assert "SK1 1AA" in result["matched_on"] or result["matched_on"] == "SK1 1AA"
    updated = conn.execute("SELECT website, domain FROM businesses WHERE id='b1'").fetchone()
    assert updated["domain"] == "acmeconsulting.co.uk"
    ev = db.evidence_for(conn, "b1")
    assert any(e["fact"] == "domain_resolved" for e in ev)


def test_resolve_rejects_a_page_without_any_match_and_records_tried(tmp_path, monkeypatch, cfg):
    from leadforge.enrich import resolve_domain
    from leadforge.models import Business

    conn = db.connect(cfg.db_path)
    # a 3-content-word name so joined/hyphenated/first-two-words are 3 DISTINCT slugs (a 2-word name
    # collapses first-two-words == joined, which is exercised separately above)
    biz = Business(id="b2", name="Acme Bright Consulting Limited", source="companies_house",
                   dedupe_key="na:b2", address_postal="SK1 1AA")
    db.upsert_business(conn, biz)
    row = conn.execute("SELECT * FROM businesses WHERE id='b2'").fetchone()

    # crawler "succeeds" for every candidate but the page never contains the postcode/name/number
    fake = _FakeCrawler({"": "This page has nothing relevant on it at all."})
    monkeypatch.setattr("leadforge.enrich.crawler.SiteCrawler", lambda cfg_: fake)
    result = resolve_domain.resolve(conn, cfg, row)
    assert result is None
    updated = conn.execute("SELECT website, domain, enrich_json FROM businesses WHERE id='b2'").fetchone()
    assert updated["domain"] is None
    dr = json.loads(updated["enrich_json"])["domain_resolution"]
    assert dr["resolved"] is False
    assert len(dr["tried"]) == 9  # 3 distinct slug variants x 3 tlds


def test_run_resolve_only_touches_company_mode_businesses(tmp_path, monkeypatch, cfg):
    """A local_business (source='gosom') row must never get domain-guessed."""
    from leadforge.enrich import resolve_domain
    from leadforge.models import Business

    conn = db.connect(cfg.db_path)
    db.upsert_business(conn, Business(id="g1", name="Joe's Garage", source="gosom", dedupe_key="na:g1"))
    db.upsert_business(conn, Business(id="c1", name="ACME CONSULTING LIMITED", source="companies_house",
                                      dedupe_key="na:c1", address_postal="SK1 1AA"))
    fake = _FakeCrawler({})  # nothing resolves; we only care what gets ATTEMPTED
    monkeypatch.setattr("leadforge.enrich.crawler.SiteCrawler", lambda cfg_: fake)
    counts = resolve_domain.run_resolve(conn, cfg, 10)
    assert counts["domain_resolve_attempted"] == 1  # only c1
    g1 = conn.execute("SELECT enrich_json FROM businesses WHERE id='g1'").fetchone()
    assert json.loads(g1["enrich_json"] or "{}").get("domain_resolution") is None


# ---------------------------------------------------------------------- scoring
def _seed_company_business(conn, **kw):
    from leadforge.models import Business, Evidence

    base = dict(id="biz_c1", name="Acme Consulting Ltd", name_norm="acme consulting",
               dedupe_key="na:x", source="companies_house", domain="acmeconsulting.co.uk",
               address_city="Stockport",
               enrich={"registry_profile": {"company_number": "01234567", "sic_codes": ["70229"],
                                            "incorporated": "2020-01-01", "company_status": "active"},
                      "crawled_at": "2026-08-01T00:00:00Z", "signals": {"careers": True}})
    base.update(kw)
    b = Business(**base)
    db.upsert_business(conn, b)
    db.add_evidence(conn, Evidence(business_id="biz_c1", ref_table="people", fact="registry_officer",
                                   url="u", snippet="Director — appointed 2026-06-01"))
    return b


def test_company_icp_compiles_via_intake(tmp_path, monkeypatch):
    from leadforge.intake import compile_icp
    monkeypatch.chdir(tmp_path)
    answers = tmp_path / "answers.yaml"
    answers.write_text(
        "campaign: gainlev-own-pipeline\n"
        "offer: {what: outsourced outreach VAs}\n"
        "target:\n"
        "  mode: company\n"
        "  categories: []\n"
        "  sic_codes: ['70229', '62012']\n"
        "  geography: {areas: ['Stockport, Greater Manchester'], country: GB}\n"
        "scoring: {profile: company}\n",
        encoding="utf-8",
    )
    out = tmp_path / "icp.yaml"
    icp, warns = compile_icp(answers, out)
    assert icp.target.mode == "company"
    assert icp.target.sic_codes == ["70229", "62012"]
    assert out.is_file()
    assert any("companies_house" in w for w in warns)


def test_company_profile_scores_deterministically(conn, cfg):
    icp = _company_icp()
    from leadforge.intake import _activate_company_mode
    _activate_company_mode(icp)  # ensures score.register_profile() has run
    from leadforge.score import score_run

    run_id = db.create_run(conn, "icp.yaml", icp.icp_hash())
    _seed_company_business(conn)
    counts1 = score_run(conn, icp, run_id)
    row1 = dict(db.scores_for_run(conn, run_id)[0])
    counts2 = score_run(conn, icp, run_id)
    row2 = dict(db.scores_for_run(conn, run_id)[0])
    assert counts1 == counts2
    assert row1["total"] == row2["total"] and row1["tier"] == row2["tier"]
    assert row1["tier"] in ("A", "B", "C", "DQ")
    factors = json.loads(row1["factors_json"])
    names = {f["factor"] for f in factors}
    # named "status" (not "readiness") on purpose: export._row_for's default-profile column block
    # reads meta["status"]["why"] for ANY non-account_fit profile, company mode included — that block
    # is gated behind `if profile == "account_fit":` so this never trips the account_fit columns.
    assert {"industry_fit", "incorporation_age", "new_director", "hiring",
           "domain_resolved", "data_confidence", "contactability", "status"} <= names


def test_company_profile_industry_fit_rewards_sic_overlap(conn, cfg):
    icp = _company_icp()
    from leadforge.intake import _activate_company_mode
    _activate_company_mode(icp)
    from leadforge.company import score_business_company

    run_id = db.create_run(conn, "icp.yaml", icp.icp_hash())
    _seed_company_business(conn)
    s = score_business_company(conn, icp, run_id, db.all_businesses(conn)[0])
    ind = next(f for f in s.factors if f.factor == "industry_fit")
    assert ind.points == 25.0 and "70229" in ind.why


def test_local_business_scoring_is_untouched_by_registration(conn, sample_icp):
    """Registering the company profile must not change what the default Scorer does for a normal
    local_business ICP (score.py's own dispatch — `if profile == 'account_fit' ... else default` —
    is only ever wrapped, never replaced)."""
    from leadforge.intake import _activate_company_mode
    from leadforge.models import Business
    from leadforge.score import score_run

    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    db.upsert_business(conn, Business(id="biz_local", name="Indie Auto", name_norm="indie auto",
                                      dedupe_key="pid:X", place_id="X", category="auto repair shop",
                                      categories=["auto repair shop"], source="gosom",
                                      phone_e164="+17135550100", address_city="Houston"))
    counts_before = score_run(conn, sample_icp, run_id)
    company_icp = _company_icp()
    _activate_company_mode(company_icp)  # registers/patches score.score_run
    counts_after = score_run(conn, sample_icp, run_id)  # still a local_business ICP
    assert counts_before == counts_after


def test_local_business_icp_categories_still_required(cfg):
    """Existing behavior for the default mode must be untouched."""
    with pytest.raises(ValidationError):
        ICP.model_validate({
            "campaign": "x", "offer": {"what": "y"},
            "target": {"categories": [], "geography": {"areas": ["Austin, TX"], "country": "US"}},
        })


def test_company_mode_requires_sic_codes_and_areas():
    with pytest.raises(ValidationError):
        ICP.model_validate({
            "campaign": "x", "offer": {"what": "y"},
            "target": {"mode": "company", "categories": [],
                      "geography": {"areas": ["Stockport, Greater Manchester"], "country": "GB"}},
        })
    with pytest.raises(ValidationError):
        ICP.model_validate({
            "campaign": "x", "offer": {"what": "y"},
            "target": {"mode": "company", "categories": [], "sic_codes": ["70229"],
                      "geography": {"country": "GB"}},
        })
