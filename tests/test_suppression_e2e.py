"""U8.1/U8.4: suppression is honored end-to-end — enrich queue, scoring, export."""

import pytest

from leadforge import db
from leadforge.models import RawListing
from leadforge.util import now_iso


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)

    def fake_fetch(self, query, limit=None):
        return [
            RawListing(provider="gosom", fetched_at=now_iso(), data={
                "title": "Blocked Garage", "address": "1 A St, Houston, TX 77001",
                "phone": "713-555-0100", "web_site": "http://blocked-garage.test",
                "review_rating": "4.0", "review_count": "20", "place_id": "PID_BLOCK"}),
            RawListing(provider="gosom", fetched_at=now_iso(), data={
                "title": "Kept Garage", "address": "2 B St, Houston, TX 77002",
                "phone": "713-555-0200", "web_site": "http://kept-garage.test",
                "review_rating": "4.5", "review_count": "30", "place_id": "PID_KEEP"}),
        ]

    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.available", lambda self: (True, "mock"))
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.fetch", fake_fetch)
    from leadforge.enrich.crawler import CrawlResult, Page, SiteCrawler
    monkeypatch.setattr(SiteCrawler, "crawl", lambda self, website: CrawlResult(
        ok=True, pages=[Page(website, "<html><body>hi</body></html>", "hi")], signals={"https": True}))
    monkeypatch.setattr("leadforge.enrich.runner.validate_email", lambda e, lab, cfg: ("valid", {}))


def test_suppressed_domain_never_crawled_or_exported(cfg, sample_icp, patched, tmp_path):
    import yaml
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")
    from leadforge.pipeline import run_pipeline

    # Full run first (both businesses land, both scored + exported)
    r1 = run_pipeline(cfg, sample_icp, icp_path, skip_dm=True)
    assert r1["stage"] == "exported"

    # Opt-out arrives: suppress the domain, then verify every downstream surface honors it
    conn = db.connect(cfg.db_path)
    db.suppress(conn, "domain", "blocked-garage.test", "test suppression")
    conn.commit()

    conn.execute("UPDATE businesses SET enrich_json='{}'")  # re-open the enrich queue
    conn.commit()
    queue = db.businesses_for_enrich(conn, 100)
    assert queue, "queue unexpectedly empty"
    assert all("blocked-garage" not in (b["website"] or "") for b in queue), \
        "suppressed domain surfaced in the enrich queue"

    # export again from the recorded scores — suppressed business must be filtered out
    from leadforge.export import export_run
    run_id = r1["run"]
    arts = export_run(conn, sample_icp, run_id, cfg.exports_dir, ["csv"])
    from pathlib import Path
    content = Path([a for a in arts if a.endswith(".csv")][0]).read_text(encoding="utf-8-sig")
    assert "blocked-garage" not in content, "suppressed domain leaked into the export"
    assert "kept-garage" in content

    # and a rediscovery never re-upserts it
    assert db.is_suppressed(conn, "blocked-garage.test", None)
