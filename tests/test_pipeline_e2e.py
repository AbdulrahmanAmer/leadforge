"""End-to-end pipeline test (offline): mock the discovery provider + doctor + crawler, then drive
the full run() state machine through discover -> enrich -> dm gate -> resume -> score -> export.

This is the scaffold's proof that all stages wire together without network or the gosom binary.
"""
import json

import pytest

from leadforge import db
from leadforge.models import RawListing
from leadforge.util import now_iso


@pytest.fixture
def patched(monkeypatch):
    # 1) doctor: pretend the environment is ready
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)

    # 2) discovery provider: return two fake listings, no binary/network
    def fake_available(self):
        return True, "mock"

    def fake_fetch(self, query, limit=None):
        return [
            RawListing(provider="gosom", fetched_at=now_iso(), data={
                "title": "Alpha Auto Repair", "category": "auto repair shop",
                "address": "1 A St, Houston, TX 77001",
                "complete_address": {"street": "1 A St", "city": "Houston", "state": "TX",
                                     "postal_code": "77001", "country": "United States"},
                "phone": "713-555-0100", "web_site": "http://alpha-auto.test",
                "review_rating": "3.4", "review_count": "90", "place_id": "PID_ALPHA",
            }),
            RawListing(provider="gosom", fetched_at=now_iso(), data={
                "title": "Beta Transmission", "category": "transmission shop",
                "address": "2 B St, Houston, TX 77002", "phone": "713-555-0200",
                "review_rating": "4.8", "review_count": "12", "place_id": "PID_BETA",
            }),
        ]

    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.available", fake_available)
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.fetch", fake_fetch)

    # 3) crawler: Alpha has a reachable site with an owner + email; Beta has no site
    from leadforge.enrich.crawler import CrawlResult, Page, SiteCrawler

    def fake_crawl(self, website, business_domain=None):
        if "alpha" in website:
            html = ('<html><body>Owner Sam Alpha founded the shop. '
                    '<a href="mailto:sam@alpha-auto.test">email</a></body></html>')
            text = "Owner Sam Alpha founded the shop in 2010. Contact sam@alpha-auto.test"
            return CrawlResult(ok=True, pages=[Page(website, html, text)],
                               signals={"https": False, "stale_site": True, "booking_hint": False})
        return CrawlResult(ok=False, error="unreachable")

    monkeypatch.setattr(SiteCrawler, "crawl", fake_crawl)
    # keep email validation deterministic & offline
    monkeypatch.setattr("leadforge.enrich.runner.validate_email",
                        lambda email, label, cfg: ("valid" if label == "personal" else "role", {}))


def test_full_pipeline_offline(cfg, sample_icp, patched, monkeypatch, tmp_path):
    import yaml
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")

    from leadforge.pipeline import run_pipeline

    # First pass: discover -> enrich -> pauses at dm_pending (Alpha has a candidate person).
    # autopilot=False: v0.4 defaults to continuing on its own (ADR-015) — this test exercises the
    # original agent-in-the-loop pause, so it opts out explicitly.
    r1 = run_pipeline(cfg, sample_icp, icp_path, autopilot=False)
    assert r1["stage"] == "dm_pending"
    assert r1["counts"]["dm_pending"] >= 1

    # Agent labels the DM
    conn = db.connect(cfg.db_path)
    pending = db.dm_pending(conn, 10)
    biz_id = pending[0]["id"]
    labels = tmp_path / "dm_labels.ndjson"
    labels.write_text(json.dumps({"biz": biz_id, "pick": 0, "confidence": 0.9}) + "\n", encoding="utf-8")
    from leadforge.enrich.dm import apply_labels
    applied = apply_labels(conn, labels)
    assert applied["applied"] == 1

    # Resume: score + export
    r2 = run_pipeline(cfg, sample_icp, icp_path, resume=True, autopilot=False)
    assert r2["stage"] == "exported"
    assert r2["counts"]["leads"] == 2
    # Alpha (stale site + owner DM + valid email, web-design ICP) should outrank Beta
    xlsx = [a for a in r2["artifacts"] if a.endswith(".xlsx")]
    csv = [a for a in r2["artifacts"] if a.endswith(".csv")]
    assert xlsx and csv
    from pathlib import Path
    assert Path(xlsx[0]).exists() and Path(csv[0]).exists()

    # DM was recorded on Alpha
    people = db.people_for(conn, biz_id)
    assert any(p["is_dm"] == 1 and p["labeled_by"] == "agent" for p in people)


def test_resume_recovers_from_killed_enrich(cfg, sample_icp, patched, tmp_path):
    """v0.1.4: a run killed mid-enrich persisted stage='enriching', which no dispatch block
    handled — every --resume no-opped with ok=true/next=null, permanently wedging the run."""
    import yaml
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")
    from leadforge.pipeline import run_pipeline
    r1 = run_pipeline(cfg, sample_icp, icp_path, autopilot=False)
    assert r1["stage"] == "dm_pending"
    conn = db.connect(cfg.db_path)
    db.set_stage(conn, r1["run"], "enriching")  # simulate the kill
    r2 = run_pipeline(cfg, sample_icp, icp_path, resume=True, autopilot=False)
    assert r2["stage"] == "dm_pending"  # enrich is idempotent; the run moves forward again


def test_degraded_queries_are_retried_on_resume(cfg, sample_icp, patched, monkeypatch, tmp_path):
    """v0.1.4: the digest promised '--resume retries degraded queries' but pending_queries only
    selected status='pending' — the promised recovery was a silent no-op, tiles lost forever."""
    import yaml

    from leadforge.pipeline import run_discover, run_pipeline
    from leadforge.util import ProviderDegraded, now_iso
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")

    def degrading_fetch(self, query, limit=None):
        raise ProviderDegraded("captcha")

    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.fetch", degrading_fetch)
    run_id, counts, warns = run_discover(cfg, sample_icp, icp_path)
    conn = db.connect(cfg.db_path)
    degraded = conn.execute("SELECT COUNT(*) c FROM queries WHERE run_id=? AND status='degraded'",
                            (run_id,)).fetchone()["c"]
    assert counts["tiles_degraded"] >= 1 and degraded >= 1

    def recovered_fetch(self, query, limit=None):
        return [RawListing(provider="gosom", fetched_at=now_iso(), data={
            "title": "Late Arrival Garage", "category": "auto repair shop",
            "phone": "713-555-0300", "review_rating": "4.0", "review_count": "40",
            "place_id": "PID_LATE"})]

    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.fetch", recovered_fetch)
    r = run_pipeline(cfg, sample_icp, icp_path, resume=True)
    conn = db.connect(cfg.db_path)
    left = conn.execute("SELECT COUNT(*) c FROM queries WHERE run_id=? AND status='degraded'",
                        (run_id,)).fetchone()["c"]
    assert left == 0  # the promise is now kept
    assert conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"] >= 1
    assert r["stage"] in ("dm_pending", "exported")


def test_apply_labels_accepts_documented_tsv_variant(cfg, tmp_path):
    """v0.1.4: dm-labeling.md documents 'biz<TAB>pick<TAB>confidence<TAB>title_override' — apply
    must accept it, not just NDJSON (an agent following the docs used to get 'bad label line')."""
    from leadforge.enrich.dm import apply_labels
    from leadforge.models import Business, Person
    conn = db.connect(cfg.db_path)
    rid = db.create_run(conn, "icp.yaml", "h")
    db.upsert_business(conn, Business(id="b1", run_id=rid, name="Acme", source="gosom", dedupe_key="dk1"))
    db.add_person(conn, Person(business_id="b1", name="Jane Smith", title="Owner"))
    db.add_person(conn, Person(business_id="b1", name="Bob Jones", title="Mechanic"))
    # candidate indexes are defined by the same people_for() enumeration the batch export uses
    people = [p for p in db.people_for(conn, "b1") if p["is_dm"] == 0]
    jane = next(i for i, p in enumerate(people) if p["name"] == "Jane Smith")
    labels = tmp_path / "dm_labels.tsv"
    labels.write_text(f"b1\t{jane}\t0.8\tManaging Director\n", encoding="utf-8")
    out = apply_labels(conn, labels)
    assert out["applied"] == 1
    dm = next(p for p in db.people_for(conn, "b1") if p["is_dm"] == 1)
    assert dm["name"] == "Jane Smith" and dm["title"] == "Managing Director"
    assert dm["dm_confidence"] == 0.8


def test_resume_is_idempotent(cfg, sample_icp, patched, tmp_path):
    import yaml
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")
    from leadforge.pipeline import run_pipeline

    run_pipeline(cfg, sample_icp, icp_path)
    # skip DM this time to reach export, then re-run resume: should not duplicate businesses
    run_pipeline(cfg, sample_icp, icp_path, resume=True, skip_dm=True)
    conn = db.connect(cfg.db_path)
    n = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    assert n == 2
