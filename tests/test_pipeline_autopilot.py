"""v0.4 autopilot pipeline wiring (ADR-015): a `run` with autopilot on continues past enrichment through
labeling -> scoring -> drafting -> export without pausing, using the operator's own headless Claude Code
where a stub says it's available and deterministic fallbacks (heuristics; a skip warning for drafting)
everywhere else. `leadforge.agent_runner` and `leadforge.draft.service` are OTHER builders' new modules —
every test here patches the real modules' entry points (`make_ndjson_runner`, `auto_draft`) — never the
real `claude` CLI, which tests/conftest.py also blocks from auto-detection. Modeled on `tests/test_pipeline_e2e.py`'s
`patched` fixture."""

from __future__ import annotations

import json

import pytest
import yaml

from leadforge import db
from leadforge.models import RawListing
from leadforge.util import now_iso


@pytest.fixture
def patched(monkeypatch):
    # 1) doctor: pretend the environment is ready
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)

    # 2) discovery provider: one fake listing with a reachable site (-> a DM candidate)
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
        ]

    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.available", fake_available)
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.fetch", fake_fetch)

    # 3) crawler: a reachable site with an owner + email
    from leadforge.enrich.crawler import CrawlResult, Page, SiteCrawler

    def fake_crawl(self, website, business_domain=None):
        html = ('<html><body>Owner Sam Alpha founded the shop. '
                '<a href="mailto:sam@alpha-auto.test">email</a></body></html>')
        text = "Owner Sam Alpha founded the shop in 2010. Contact sam@alpha-auto.test"
        return CrawlResult(ok=True, pages=[Page(website, html, text)],
                           signals={"https": False, "stale_site": True, "booking_hint": False})

    monkeypatch.setattr(SiteCrawler, "crawl", fake_crawl)
    monkeypatch.setattr("leadforge.enrich.runner.validate_email",
                        lambda email, label, cfg: ("valid" if label == "personal" else "role", {}))


def _icp_path(sample_icp, tmp_path):
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")
    return icp_path


def _stub_agent_runner(monkeypatch, *, label_pick: int = 0):
    """A generic NDJSON runner: labels pick=<label_pick> for a DM candidate line, drafts a one-line
    subject/observation from the packet's `co` for a drafting line — the same callable regardless of
    which instructions it was built from, matching how a real headless model would just reply to
    whatever it's handed."""

    def runner(lines: list[str]):
        out = []
        for ln in lines:
            rec = json.loads(ln)
            if "biz" in rec and "candidates" in rec:
                out.append({"biz": rec["biz"], "pick": label_pick, "confidence": 0.9})
            elif "packet" in rec:
                # a gate-valid draft: quote the first fact's value verbatim and cite it
                facts = rec["packet"].get("facts") or []
                if facts:
                    out.append({"target": rec["target"], "subject": "Quick note",
                                "observation": f"Noticed {facts[0]['v']}.", "used_fact": facts[0]["k"]})
                else:
                    out.append({"target": rec["target"], "abstain": True})
        return out

    calls: list[str] = []

    def make_ndjson_runner(cfg, instructions):
        calls.append(instructions)
        return runner

    monkeypatch.setattr("leadforge.agent_runner.make_ndjson_runner", make_ndjson_runner)
    return calls


def _stub_agent_runner_returns_none(monkeypatch):
    """Simulates `agent.command: []` / no `claude` on PATH: `make_ndjson_runner` itself returns None,
    per the real module's documented contract — never a raise."""
    monkeypatch.setattr("leadforge.agent_runner.make_ndjson_runner", lambda cfg, instructions: None)


def _stub_draft_service(monkeypatch, *, drafted=2, rejected=0, abstained=0, author="agent"):
    def auto_draft(conn, cfg, icp, run_id, *, runner=None, purpose=None, campaign=None):
        return {"targets": drafted + rejected + abstained, "drafted": drafted, "rejected": rejected,
                "abstained": abstained, "author": author, "batches": 1}

    monkeypatch.setattr("leadforge.draft.service.auto_draft", auto_draft)


def test_autopilot_reaches_exported_with_agent_labeling_and_drafting(cfg, sample_icp, patched, monkeypatch, tmp_path):
    calls = _stub_agent_runner(monkeypatch)
    # the REAL draft service runs (the digest's `drafted` is the export's live count of drafted rows,
    # so a stub that stores nothing would report 0); every tier is draft-eligible for this tiny run
    monkeypatch.setattr(cfg.draft, "auto_tiers", ["A", "B", "C", "D"])

    from leadforge.pipeline import run_pipeline
    icp_path = _icp_path(sample_icp, tmp_path)

    result = run_pipeline(cfg, sample_icp, icp_path, autopilot=True)

    assert result["stage"] == "exported"
    assert result["counts"]["dm_labeled"] >= 1
    assert result["counts"]["dm_unlabeled"] == 0
    assert result["counts"]["drafted"] >= 1
    assert result["counts"]["runner"] == "agent"
    # the runner was built twice: once for labeling, once for drafting
    assert len(calls) == 2

    # never paused at dm_pending — autopilot is on by default
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT stage FROM runs WHERE id=?", (result["run"],)).fetchone()
    assert row["stage"] == "exported"
    # the DM candidate really was labeled by the (stubbed) agent, not left for a human
    biz_id = conn.execute("SELECT id FROM businesses LIMIT 1").fetchone()["id"]
    people = db.people_for(conn, biz_id)
    assert any(p["is_dm"] == 1 and p["labeled_by"] == "agent" for p in people)


def test_autopilot_with_no_runner_still_reaches_exported_via_heuristics(cfg, sample_icp, patched, monkeypatch, tmp_path):
    _stub_agent_runner_returns_none(monkeypatch)
    # draft.service is deliberately left unstubbed: the real module doesn't exist in this worktree, so
    # pipeline.py must degrade drafting to a skip warning rather than block the run
    from leadforge.pipeline import run_pipeline
    icp_path = _icp_path(sample_icp, tmp_path)

    result = run_pipeline(cfg, sample_icp, icp_path, autopilot=True)

    # never blocks: leftovers export as unlabeled rather than pausing (docs/06). Whether the
    # extraction pipeline's title happens to match the ICP's priority list (and so gets caught by
    # `heuristic_labels`) is exercised deterministically at the unit level in
    # tests/test_dm_auto_label.py — this test's job is just the WIRING: no runner -> "none", no pause.
    assert result["stage"] == "exported"
    assert result["counts"]["runner"] == "none"
    assert result["counts"]["dm_labeled"] + result["counts"]["dm_unlabeled"] >= 1
    # no runner: the real draft service still runs (template fallback), never a "drafting unavailable" skip
    assert "drafted" in result["counts"] and not any("drafting unavailable" in w for w in result["warnings"])


def test_autopilot_false_pauses_at_dm_pending_like_before(cfg, sample_icp, patched, monkeypatch, tmp_path):
    # even with a working agent runner available, autopilot=False must behave exactly like pre-v0.4
    _stub_agent_runner(monkeypatch)
    _stub_draft_service(monkeypatch)
    from leadforge.pipeline import run_pipeline
    icp_path = _icp_path(sample_icp, tmp_path)

    result = run_pipeline(cfg, sample_icp, icp_path, autopilot=False)

    assert result["stage"] == "dm_pending"
    assert result["counts"]["dm_pending"] >= 1
    assert result["next"] == "leadforge dm export --max 60"
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT stage FROM runs WHERE id=?", (result["run"],)).fetchone()
    assert row["stage"] == "dm_pending"


def test_autopilot_defaults_to_config_pipeline_autopilot(cfg, sample_icp, patched, monkeypatch, tmp_path):
    """No explicit `autopilot=` kwarg: falls back to `cfg.pipeline.autopilot` (default True via
    getattr fallback in this worktree, since builder A's PipelineCfg may not exist yet)."""
    _stub_agent_runner(monkeypatch)
    _stub_draft_service(monkeypatch)
    from leadforge.pipeline import run_pipeline
    icp_path = _icp_path(sample_icp, tmp_path)

    result = run_pipeline(cfg, sample_icp, icp_path)  # no autopilot kwarg at all

    assert result["stage"] == "exported"


def test_resume_reenters_labeling_and_drafting_idempotently(cfg, sample_icp, patched, monkeypatch, tmp_path):
    """A run killed mid-'labeling' or mid-'drafting' resumes exactly there (docs contract: both stages
    are re-entered idempotently) rather than being stuck or re-running earlier stages."""
    _stub_agent_runner(monkeypatch)
    _stub_draft_service(monkeypatch, drafted=1)
    from leadforge.pipeline import run_pipeline
    icp_path = _icp_path(sample_icp, tmp_path)

    r1 = run_pipeline(cfg, sample_icp, icp_path, autopilot=True)
    assert r1["stage"] == "exported"

    # simulate a kill mid-drafting: resume must not error and must still finish exported
    conn = db.connect(cfg.db_path)
    db.set_stage(conn, r1["run"], "drafting")
    r2 = run_pipeline(cfg, sample_icp, icp_path, resume=True, autopilot=True)
    assert r2["stage"] == "exported"

    # and mid-labeling
    db.set_stage(conn, r1["run"], "labeling")
    r3 = run_pipeline(cfg, sample_icp, icp_path, resume=True, autopilot=True)
    assert r3["stage"] == "exported"
