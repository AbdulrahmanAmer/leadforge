"""v0.3 speed unit (2026-09-02): enrichment throughput — build items 2-5 from docs/09's speed unit.

Item 1 (fail-fast tail) and item 3's per-host-pacing proof live in tests/test_crawler_politeness.py
(extended, not this new file — they're crawler.py-level politeness proofs against a real local
server). This file covers: queue interleaving (item 3), the browser-render concurrency gate (item 2),
DNS-pooled email validation (item 5), and the overlapped 'all' stage (item 4) — including the
single-writer discipline and DB-state parity with the serial path.

No network: SiteCrawler.crawl, registry lookups and validate_email are all monkeypatched.
"""
from __future__ import annotations

import time

from leadforge import db
from leadforge.config import load_config
from leadforge.enrich import runner
from leadforge.enrich.crawler import CrawlResult, Page, SiteCrawler
from leadforge.models import Business, Evidence, Person
from leadforge.providers.registry import CompaniesHouseRegistry


# ============================================================================ build item 3: queue order
def test_interleave_by_domain_round_robins_chains():
    """A 3-location chain sharing one domain must NOT land as 3 consecutive submissions — the first
    len(distinct domains) entries must cover every distinct domain once, so ThreadPoolExecutor
    submission order maximizes distinct hosts among the first `workers` futures (see the docstring on
    _interleave_by_domain for why this matters). Each domain's own internal (prior priority) order is
    preserved — only the CROSS-domain interleaving changes."""
    queue_in = [
        {"id": "c1", "domain": "chain.test"},
        {"id": "c2", "domain": "chain.test"},
        {"id": "c3", "domain": "chain.test"},
        {"id": "s1", "domain": "solo1.test"},
        {"id": "s2", "domain": "solo2.test"},
    ]
    out = runner._interleave_by_domain(queue_in)
    assert len(out) == 5
    assert {b["id"] for b in out} == {"c1", "c2", "c3", "s1", "s2"}
    first_domains = [b["domain"] for b in out[:3]]
    assert len(set(first_domains)) == 3, f"expected 3 distinct domains up front, got {first_domains}"
    chain_order = [b["id"] for b in out if b["domain"] == "chain.test"]
    assert chain_order == ["c1", "c2", "c3"]  # the chain's own internal priority order survives


def test_interleave_by_domain_no_repeats_is_a_no_op():
    """Watched-fail control: with no shared domains (the common case), interleaving must be identity —
    proves the function isn't reordering things it has no reason to touch."""
    queue_in = [{"id": f"x{i}", "domain": f"solo{i}.test"} for i in range(5)]
    assert runner._interleave_by_domain(queue_in) == queue_in


# ============================================================================ build item 2: render gate
def test_render_gate_honors_browser_concurrency():
    from leadforge.enrich import browser

    browser._render_gate = None
    browser._render_gate_size = None
    try:
        gate = browser._get_render_gate(3)
        for _ in range(3):
            assert gate.acquire(blocking=False) is True
        assert gate.acquire(blocking=False) is False, "a 4th acquire must block when concurrency=3"
    finally:
        # drain whatever we acquired so the module-level semaphore isn't left held for other tests
        for _ in range(3):
            try:
                gate.release()
            except ValueError:
                pass
        browser._render_gate = None
        browser._render_gate_size = None


def test_render_gate_resizes_when_concurrency_changes():
    from leadforge.enrich import browser

    browser._render_gate = None
    browser._render_gate_size = None
    try:
        gate1 = browser._get_render_gate(1)
        assert gate1.acquire(blocking=False) is True
        assert gate1.acquire(blocking=False) is False
        gate1.release()
        gate2 = browser._get_render_gate(2)
        assert gate2 is not gate1  # rebuilt for the new size
        assert gate2.acquire(blocking=False) is True
        assert gate2.acquire(blocking=False) is True
        assert gate2.acquire(blocking=False) is False
        gate2.release()
        gate2.release()
    finally:
        browser._render_gate = None
        browser._render_gate_size = None


# ============================================================================ build item 5: DNS pool
def test_validate_emails_parallel_matches_serial_and_is_faster(cfg, monkeypatch):
    from leadforge.enrich import validate as validate_mod

    calls: list[str] = []

    def fake_validate_email(email, label, cfg_):
        time.sleep(0.05)
        calls.append(email)
        return ("valid", {"email": email})

    monkeypatch.setattr(validate_mod, "validate_email", fake_validate_email)
    cfg.enrich.dns_workers = 8
    rows = [{"id": i, "value": f"user{i}@example.test", "label": "personal"} for i in range(16)]

    t0 = time.monotonic()
    results = validate_mod.validate_emails_parallel(rows, cfg)
    elapsed = time.monotonic() - t0

    assert len(results) == 16
    assert {r_id for r_id, _tier, _meta in results} == {r["id"] for r in rows}
    for r_id, tier, meta in results:
        assert tier == "valid"
        assert meta["email"] == f"user{r_id}@example.test"
    assert len(calls) == 16  # every row actually validated, none skipped/duplicated
    # serial would take 16*0.05s = 0.8s; pooled with 8 workers should be roughly 2*0.05s (+overhead)
    assert elapsed < 0.4, f"pooled validation took {elapsed:.2f}s — expected well under the serial 0.8s"


def test_validate_emails_parallel_empty_input_returns_empty():
    from leadforge.enrich.validate import validate_emails_parallel

    class _Cfg:
        class enrich:
            dns_workers = 8

    assert validate_emails_parallel([], _Cfg()) == []


# ============================================================================ build item 4: overlap
def _seed_fixture(conn) -> None:
    """b1: has a domain, crawls OK, gets a registry match (GB). b2: has a domain, crawl fails, no
    registry match. b3: site-less (no domain), GB, registry match. b4: site-less, US — no covering
    registry, so it must get NO registry write at all (in either path)."""
    db.upsert_business(conn, Business(id="b1", name="Alpha Auto", name_norm="alpha auto",
                                       domain="alpha.test", website="http://alpha.test",
                                       address_country="GB", source="gosom", dedupe_key="dk:b1"))
    db.upsert_business(conn, Business(id="b2", name="Beta Motors", name_norm="beta motors",
                                       domain="beta.test", website="http://beta.test",
                                       address_country="GB", source="gosom", dedupe_key="dk:b2"))
    db.upsert_business(conn, Business(id="b3", name="Siteless One", name_norm="siteless one",
                                       address_country="GB", source="gosom", dedupe_key="dk:b3"))
    db.upsert_business(conn, Business(id="b4", name="Siteless Two", name_norm="siteless two",
                                       address_country="US", source="gosom", dedupe_key="dk:b4"))
    conn.commit()


def _fake_crawl(self, website, business_domain=None):
    if "alpha" in website:
        html = ('<html><body>Owner Alice Alpha runs the shop. '
                '<a href="mailto:info@alpha.test">info@alpha.test</a></body></html>')
        text = "Owner Alice Alpha runs the shop. Contact info@alpha.test"
        return CrawlResult(ok=True, pages=[Page(website, html, text)], signals={"https": True})
    return CrawlResult(ok=False, error="unreachable")


def _fake_lookup_with_profile(self, biz):
    if biz["id"] == "b1":
        person = Person(business_id="b1", name="Alice Alpha", title="Director",
                        labeled_by="registry", origin="registry")
        ev = Evidence(business_id="b1", ref_table="people", fact="registry_officer",
                     url="u1", snippet="Director — appointed 2020-01-01")
        profile = {"company_number": "1", "company_status": "active", "legal_name": "ALPHA LTD",
                  "match_similarity": 0.9, "incorporated": "2020-01-01", "sic_codes": []}
        return [(person, ev)], profile
    if biz["id"] == "b3":
        person = Person(business_id="b3", name="Sam Siteless", title="Director",
                        labeled_by="registry", origin="registry")
        ev = Evidence(business_id="b3", ref_table="people", fact="registry_officer",
                     url="u3", snippet="Director — appointed 2019-01-01")
        profile = {"company_number": "3", "company_status": "active", "legal_name": "SITELESS LTD",
                  "match_similarity": 0.9, "incorporated": "2019-01-01", "sic_codes": []}
        return [(person, ev)], profile
    return [], None  # b2: same jurisdiction, no match. b4 never reaches here (US, no covering registry).


def _snapshot(conn) -> tuple:
    contacts = conn.execute(
        "SELECT business_id, kind, value, tier, affinity FROM contacts ORDER BY business_id, kind, value"
    ).fetchall()
    people = conn.execute(
        "SELECT business_id, name, title, origin, is_dm FROM people ORDER BY business_id, name"
    ).fetchall()
    evidence_facts = conn.execute(
        "SELECT business_id, fact, COUNT(*) n FROM evidence GROUP BY business_id, fact ORDER BY business_id, fact"
    ).fetchall()
    enrich_checked = conn.execute(
        "SELECT id, json_extract(enrich_json,'$.registry_checked') FROM businesses ORDER BY id"
    ).fetchall()
    return (
        [tuple(r) for r in contacts],
        [tuple(r) for r in people],
        [tuple(r) for r in evidence_facts],
        [tuple(r) for r in enrich_checked],
    )


def _run_once(tmp_path, subdir: str, overlap: bool, monkeypatch) -> tuple:
    d = tmp_path / subdir
    d.mkdir()
    monkeypatch.chdir(d)
    cfg = load_config(d)
    cfg.registry.companies_house_key = "k"
    cfg.enrich.overlap_stages = overlap
    conn = db.connect(cfg.db_path)
    _seed_fixture(conn)
    runner.run_enrich(conn, cfg, 10, stage="all")
    snap = _snapshot(conn)
    conn.close()
    return snap


def test_overlapped_stages_match_serial_db_state(tmp_path, monkeypatch):
    """The overlapped 'all' stage must persist EXACTLY the same rows as the serial 'all' stage on an
    identical starting fixture — contacts, people, evidence-fact counts, and the registry_checked flag
    per business (including b4, which is in NO covered jurisdiction and must get no registry write in
    either path — see _lookup_registry_for's any_covered)."""
    monkeypatch.setattr(SiteCrawler, "crawl", _fake_crawl)
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    monkeypatch.setattr(CompaniesHouseRegistry, "lookup_with_profile", _fake_lookup_with_profile)

    snap_serial = _run_once(tmp_path, "serial", overlap=False, monkeypatch=monkeypatch)
    snap_overlap = _run_once(tmp_path, "overlap", overlap=True, monkeypatch=monkeypatch)

    assert snap_overlap == snap_serial
    contacts, people, evidence, checked = snap_serial
    # sanity: this actually exercised something real, not two empty runs trivially agreeing
    assert any(c[1] == "email" and c[2] == "info@alpha.test" for c in contacts)
    assert any(p[1] == "Alice Alpha" for p in people)
    assert any(p[1] == "Sam Siteless" for p in people)
    checked_map = dict(checked)
    assert checked_map["b1"] == 1  # crawled, covered jurisdiction -> checked
    assert checked_map["b3"] == 1  # site-less, covered jurisdiction -> checked
    assert checked_map["b4"] is None  # site-less, NO covering registry -> never written, either path


def test_overlap_single_writer_no_database_locked(tmp_path, monkeypatch):
    """Stress the single-writer discipline: a fake crawler and fake registry that each sleep a little
    (so the crawl driver, registry driver and main writer thread are ALL genuinely in flight at once)
    must never raise 'database is locked' and must persist every business — proving every actual
    conn.execute()/commit() really does happen on one thread only."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.companies_house_key = "k"
    cfg.enrich.overlap_stages = True
    cfg.politeness.workers = 8
    conn = db.connect(cfg.db_path)

    n = 12
    for i in range(n):
        # even ids get a domain (crawl queue), odd ids are site-less (registry backlog) — a healthy mix
        domain = f"site{i}.test" if i % 2 == 0 else None
        website = f"http://site{i}.test" if domain else None
        db.upsert_business(conn, Business(
            id=f"biz{i}", name=f"Business {i}", name_norm=f"business {i}", domain=domain,
            website=website, address_country="GB", source="gosom", dedupe_key=f"dk:biz{i}"))
    conn.commit()

    def slow_crawl(self, website, business_domain=None):
        time.sleep(0.02)
        html = f"<html><body>Contact us at info{website}</body></html>"
        return CrawlResult(ok=True, pages=[Page(website, html, "")], signals={"https": True})

    def slow_lookup(self, biz):
        time.sleep(0.02)
        return [], None

    monkeypatch.setattr(SiteCrawler, "crawl", slow_crawl)
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    monkeypatch.setattr(CompaniesHouseRegistry, "lookup_with_profile", slow_lookup)

    counts = runner.run_enrich(conn, cfg, n, stage="all")  # must not raise (no "database is locked")

    checked = conn.execute(
        "SELECT COUNT(*) c FROM businesses WHERE json_extract(enrich_json,'$.registry_checked')=1"
    ).fetchone()["c"]
    crawled = conn.execute(
        "SELECT COUNT(*) c FROM businesses WHERE json_extract(enrich_json,'$.crawled_at') IS NOT NULL"
    ).fetchone()["c"]
    assert checked == n, f"expected all {n} businesses registry_checked, got {checked}"
    assert crawled == n // 2, f"expected {n // 2} crawled (the ones with a domain), got {crawled}"
    assert counts["sites_crawled"] == n // 2
    conn.close()


def test_single_stage_runs_unaffected_by_overlap_flag(tmp_path, monkeypatch):
    """`enrich --stage site` must behave identically regardless of cfg.enrich.overlap_stages — the
    overlap path only ever engages for stage='all'. A site-less business is the sharpest probe: only
    _registry_stage (stage='registry' or 'all') ever touches it — if the overlap flag leaked into a
    'site'-only run, its registry-backlog driver would process this business's registry_checked flag
    even though nothing asked for a registry stage at all."""
    monkeypatch.setattr(SiteCrawler, "crawl", _fake_crawl)
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    monkeypatch.setattr(CompaniesHouseRegistry, "lookup_with_profile", lambda self, biz: ([], None))

    def _seed_and_run(subdir: str, overlap: bool) -> tuple:
        d = tmp_path / subdir
        d.mkdir()
        monkeypatch.chdir(d)
        cfg = load_config(d)
        cfg.registry.companies_house_key = "k"
        cfg.enrich.overlap_stages = overlap
        conn = db.connect(cfg.db_path)
        db.upsert_business(conn, Business(id="b1", name="Alpha Auto", name_norm="alpha auto",
                                          domain="alpha.test", website="http://alpha.test",
                                          address_country="GB", source="gosom", dedupe_key="dk:b1"))
        db.upsert_business(conn, Business(id="b2", name="Siteless Co", name_norm="siteless co",
                                          address_country="GB", source="gosom", dedupe_key="dk:b2"))
        conn.commit()
        runner.run_enrich(conn, cfg, 10, stage="site")
        contacts = conn.execute("SELECT business_id, kind, value FROM contacts ORDER BY value").fetchall()
        siteless_checked = conn.execute(
            "SELECT json_extract(enrich_json,'$.registry_checked') FROM businesses WHERE id='b2'"
        ).fetchone()[0]
        conn.close()
        return tuple(tuple(r) for r in contacts), siteless_checked

    off_contacts, off_checked = _seed_and_run("off", overlap=False)
    on_contacts, on_checked = _seed_and_run("on", overlap=True)
    assert off_contacts == on_contacts
    assert any(c[1] == "email" for c in off_contacts)
    assert off_checked is None, "stage='site' must never touch a site-less business's registry_checked"
    assert on_checked == off_checked, (
        f"overlap_stages must not change stage='site' behavior: off={off_checked!r} on={on_checked!r}"
    )
