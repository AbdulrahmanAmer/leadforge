"""v0.4 autopilot integration tests (ADR-015): end-to-end wiring of the agent runner, including
failure modes and fallback paths. These tests exercise the real `leadforge.agent_runner` module
(which shells out to `claude -p`), the `leadforge.draft.service` module, and the pipeline's
autopilot state machine — all under controlled conditions via monkeypatching."""

from __future__ import annotations

import json

import pytest
import yaml

from leadforge import db
from leadforge.agent_runner import (
    AgentFailed,
    AgentUnavailable,
    make_ndjson_runner,
    parse_ndjson,
    resolve_command,
    run_agent,
)
from leadforge.enrich import dm
from leadforge.models import RawListing
from leadforge.pipeline import run_pipeline
from leadforge.util import now_iso


def _icp_path(sample_icp, tmp_path):
    icp_path = tmp_path / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(sample_icp.model_dump(mode="json")), encoding="utf-8")
    return icp_path


def _seed_discovery(conn, cfg, sample_icp, run_id):
    """Insert a minimal discovered business that will pass through enrich -> DM -> draft."""
    from leadforge.normalize import to_business
    biz = to_business(
        RawListing(provider="gosom", fetched_at=now_iso(), data={
            "title": "Alpha Auto Repair",
            "category": "auto repair shop",
            "address": "1 A St, Houston, TX 77001",
            "complete_address": {"street": "1 A St", "city": "Houston", "state": "TX",
                                 "postal_code": "77001", "country": "United States"},
            "phone": "713-555-0100",
            "web_site": "http://alpha-auto.test",
            "review_rating": "3.4",
            "review_count": "90",
            "place_id": "PID_ALPHA",
        }),
        run_id, sample_icp, "US"
    )
    assert biz is not None
    return biz.id


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


# ---------------------------------------------------------------------------
# 1. AgentUnavailable path (no claude on PATH, agent.command: [])
# ---------------------------------------------------------------------------
def test_agent_unavailable_when_command_empty_list(cfg, monkeypatch):
    """agent.command: [] means explicitly disabled -> AgentUnavailable."""
    monkeypatch.setattr(cfg.agent, "command", [])
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert resolve_command(cfg) is None
    assert make_ndjson_runner(cfg, "instructions") is None


def test_agent_unavailable_when_claude_not_on_path(cfg, monkeypatch):
    """No command configured AND no `claude` on PATH -> AgentUnavailable."""
    monkeypatch.setattr(cfg.agent, "command", None)
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert resolve_command(cfg) is None
    assert make_ndjson_runner(cfg, "instructions") is None


def test_run_agent_raises_agent_unavailable(cfg, monkeypatch):
    """run_agent raises AgentUnavailable when no command resolves."""
    monkeypatch.setattr(cfg.agent, "command", [])
    with pytest.raises(AgentUnavailable, match="no agent command available"):
        run_agent(cfg, "prompt")


# ---------------------------------------------------------------------------
# 2. AgentFailed path (timeout, non-zero exit, empty stdout)
# ---------------------------------------------------------------------------
def test_agent_failed_on_timeout(cfg, monkeypatch):
    """run_agent raises AgentFailed on subprocess timeout."""
    monkeypatch.setattr(cfg.agent, "command", ["cmd", "/c", "timeout", "/t", "10"])
    with pytest.raises(AgentFailed, match="timed out"):
        run_agent(cfg, "prompt", timeout_s=0)


def test_agent_failed_on_nonzero_exit(cfg, monkeypatch):
    """run_agent raises AgentFailed on non-zero exit code."""
    monkeypatch.setattr(cfg.agent, "command", ["cmd", "/c", "exit /b 1"])
    with pytest.raises(AgentFailed, match="exited 1"):
        run_agent(cfg, "prompt")


def test_agent_failed_on_empty_stdout(cfg, monkeypatch):
    """run_agent raises AgentFailed when stdout is empty."""
    monkeypatch.setattr(cfg.agent, "command", ["cmd", "/c", ""])
    with pytest.raises(AgentFailed, match="empty stdout"):
        run_agent(cfg, "prompt")


# ---------------------------------------------------------------------------
# 3. NDJSON parsing with MCP noise lines
# ---------------------------------------------------------------------------
def test_parse_ndjson_survives_mcp_noise_and_fences():
    """parse_ndjson keeps only valid JSON object lines; drops fences, prose, MCP noise."""
    text = """```json
{"target": 1, "pick": 0, "confidence": 0.9}
```
Client.listTools() called but server does not advertise tools capability - returning empty list
{"target": 2, "pick": 1, "confidence": 0.8}
Some prose that is not JSON
{"target": 3, "abstain": true}
[1, 2, 3]
"scalar string"
"""
    result = parse_ndjson(text)
    assert len(result) == 3
    assert result[0] == {"target": 1, "pick": 0, "confidence": 0.9}
    assert result[1] == {"target": 2, "pick": 1, "confidence": 0.8}
    assert result[2] == {"target": 3, "abstain": True}


def test_parse_ndjson_empty_and_whitespace_only():
    assert parse_ndjson("") == []
    assert parse_ndjson("   \n\n  ") == []


# ---------------------------------------------------------------------------
# 4. Retry logic with rejected_attempt context
# ---------------------------------------------------------------------------
def test_draft_retry_includes_rejected_attempt_context(cfg, sample_icp, tmp_path):
    """When a draft is rejected, the retry batch includes `rejected_attempt` with subject + reasons."""
    from leadforge.util import now_iso
    conn = db.connect(cfg.db_path)

    # Seed a scored business with a DM candidate
    from leadforge.models import Business, Person
    biz_id = "retry_biz"
    db.upsert_business(conn, Business(id=biz_id, name="Retry Biz", name_norm="retry biz",
                                      dedupe_key="dk:retry_biz", source="gosom"))
    db.add_person(conn, Person(business_id=biz_id, name="Jane Smith", title="Owner",
                               snippet="Jane Smith founded the shop."))

    # Create a run row for FK
    run_id = "run_retry"
    conn.execute(
        "INSERT INTO runs(id, icp_path, icp_hash, stage, started_at) VALUES(?,?,?,?,?)",
        (run_id, "icp.yaml", "test_hash", "scored", now_iso()),
    )

    # Add a score so ensure_targets will pick it up
    conn.execute(
        "INSERT INTO scores(business_id,run_id,total,tier,factors_json,need_hooks_json,scored_at) VALUES(?,?,?,?,?,?,?)",
        (biz_id, run_id, 85.0, "A", "{}", "[]", now_iso()),
    )
    conn.commit()

    # Create outreach target
    from leadforge.draft.service import _rejected_lines, build_packets, ensure_targets
    target_ids = ensure_targets(conn, run_id, {"A", "B", "C", "D"}, "retry_campaign")
    assert len(target_ids) == 1

    lines, _ = build_packets(conn, cfg, sample_icp, target_ids, "gainlev_leadgen")
    packet_lines = lines[1:]

    # First, create a rejected message to simulate a prior failure
    tid = target_ids[0]
    step = 1
    conn.execute(
        "INSERT INTO messages(target_id,step,purpose,subject,body_text,draft_hash,state,gate_json,"
        "grade,used_fact,author,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, step, "gainlev_leadgen", "Bad subject", "", "hash1", "rejected",
         json.dumps({"ok": False, "reasons": ["USED_FACT missing", "PROPER_NOUN missing"]}),
         "A", "", "agent", now_iso(), now_iso()),
    )
    conn.commit()

    # Get the rejected lines for retry
    retry_batch = _rejected_lines(conn, packet_lines)
    assert len(retry_batch) == 1
    assert "rejected_attempt" in retry_batch[0]
    assert retry_batch[0]["rejected_attempt"]["subject"] == "Bad subject"
    assert "USED_FACT missing" in retry_batch[0]["rejected_attempt"]["reasons"]
    assert "PROPER_NOUN missing" in retry_batch[0]["rejected_attempt"]["reasons"]


# ---------------------------------------------------------------------------
# 5. Fallback to heuristic_labels when runner fails mid-batch
# ---------------------------------------------------------------------------
def test_auto_label_fallback_to_heuristics_on_runner_failure(cfg, sample_icp):
    """dm.auto_label falls back to heuristic_labels when runner raises mid-batch."""
    conn = db.connect(cfg.db_path)

    # Seed: one that heuristic CAN resolve, one it CAN'T
    from leadforge.models import Business, Person
    db.upsert_business(conn, Business(id="biz_heuristic", name="Heuristic Biz", name_norm="heuristic biz",
                                      dedupe_key="dk:heuristic", source="gosom"))
    db.add_person(conn, Person(business_id="biz_heuristic", name="Jane Smith", title="Owner",
                               snippet="Jane Smith founded the shop."))
    db.upsert_business(conn, Business(id="biz_unlabeled", name="Unlabeled Biz", name_norm="unlabeled biz",
                                      dedupe_key="dk:unlabeled", source="gosom"))
    db.add_person(conn, Person(business_id="biz_unlabeled", name="X", title="Mechanic",
                               snippet="Just a mechanic."))

    def boom_runner(lines):
        raise RuntimeError("agent crashed")

    out = dm.auto_label(conn, sample_icp, cfg, runner=boom_runner)
    assert out["runner"] == "none"
    assert out["batches"] == 0
    assert out["labeled"] == 1
    assert out["unlabeled"] == 1


# ---------------------------------------------------------------------------
# 6. Fallback to template_drafts when runner fails
# ---------------------------------------------------------------------------
def test_auto_draft_fallback_to_templates_on_runner_failure(cfg, sample_icp, monkeypatch):
    """draft.service.auto_draft falls back to template_drafts when runner fails."""
    from leadforge.util import now_iso
    conn = db.connect(cfg.db_path)

    # Enable template fallback
    monkeypatch.setattr(cfg.draft, "template_fallback", True)
    monkeypatch.setattr(cfg.draft, "auto_tiers", ["A", "B", "C", "D"])

    run_id = "run_template"

    # Create a run row for FK
    conn.execute(
        "INSERT INTO runs(id, icp_path, icp_hash, stage, started_at) VALUES(?,?,?,?,?)",
        (run_id, "icp.yaml", "test_hash", "scored", now_iso()),
    )

    # Seed a business with an outreach target
    from leadforge.models import Business
    bid, created = db.upsert_business(conn, Business(id="template_fallback_biz", name="Template Biz",
                                          name_norm="template biz", dedupe_key="dk:template",
                                          source="gosom", domain="template-biz.test",
                                          website="http://template-biz.test"))

    # Add a score so ensure_targets will pick it up
    conn.execute(
        "INSERT INTO scores(business_id,run_id,total,tier,factors_json,need_hooks_json,scored_at) VALUES(?,?,?,?,?,?,?)",
        (bid, run_id, 85.0, "A", "{}", "[]", now_iso()),
    )
    # Add a scored contact
    from leadforge.models import Contact
    contacts = db.contacts_for(conn, bid)
    if not contacts:
        db.add_contact(conn, Contact(business_id=bid, kind="email",
                              value="owner@template-biz.test", label="personal",
                              tier="valid", affinity="own_domain"))

    conn.commit()

    # Runner that always fails
    def failing_runner(lines):
        raise RuntimeError("agent failed")

    from leadforge.draft.service import auto_draft
    out = auto_draft(conn, cfg, sample_icp, run_id, runner=failing_runner)

    # Should have fallen back to templates (author == template)
    # Template drafts may abstain if packet has insufficient evidence; the key assertion
    # is that the fallback path was taken, not that a draft was necessarily applied
    assert out["author"] == "template"
    assert out["batches"] >= 1
    assert out["drafted"] + out["abstained"] >= 1


# ---------------------------------------------------------------------------
# 7. run --resume with autopilot=True continues from dm_pending
# ---------------------------------------------------------------------------
# This test requires full pipeline execution which is slow and marked as such.
# It is tested separately in test_pipeline_autopilot.py::test_run_parked_at_dm_pending_resumes_into_autopilot_labeling
# def test_run_resume_autopilot_continues_from_dm_pending(...): ...


# ---------------------------------------------------------------------------
# 8. run --no-autopilot pauses at dm_pending correctly
# ---------------------------------------------------------------------------
def test_run_no_autopilot_pauses_at_dm_pending(cfg, sample_icp, patched, monkeypatch, tmp_path):
    """With autopilot=False, run pauses at dm_pending regardless of agent availability."""
    icp_path = _icp_path(sample_icp, tmp_path)

    # Even with a working runner, autopilot=False must pause
    def stub_runner(lines):
        out = []
        for ln in lines:
            rec = json.loads(ln)
            if "biz" in rec and "candidates" in rec:
                out.append({"biz": rec["biz"], "pick": 0, "confidence": 0.9})
            elif "packet" in rec:
                facts = rec["packet"].get("facts") or []
                if facts:
                    out.append({"target": rec["target"], "subject": "Quick note",
                                "observation": f"Noticed {facts[0]['v']}.", "used_fact": facts[0]["k"]})
                else:
                    out.append({"target": rec["target"], "abstain": True})
        return out

    monkeypatch.setattr("leadforge.agent_runner.make_ndjson_runner", lambda cfg, inst: stub_runner)

    from leadforge.pipeline import run_pipeline
    result = run_pipeline(cfg, sample_icp, icp_path, autopilot=False)

    assert result["stage"] == "dm_pending"
    assert result["counts"]["dm_pending"] >= 1
    assert result["next"] == "leadforge dm export --max 60"

    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT stage FROM runs WHERE id=?", (result["run"],)).fetchone()
    assert row["stage"] == "dm_pending"


# ---------------------------------------------------------------------------
# Additional: full pipeline autopilot with AgentUnavailable -> heuristics/templates
# ---------------------------------------------------------------------------
def test_full_pipeline_autopilot_no_agent_still_exports(cfg, sample_icp, patched, monkeypatch, tmp_path):
    """Full autopilot pipeline with no agent available uses heuristics + templates and reaches exported."""
    # Disable agent runner
    monkeypatch.setattr(cfg.agent, "command", [])
    monkeypatch.setattr(cfg.draft, "template_fallback", True)
    monkeypatch.setattr(cfg.draft, "auto_tiers", ["A", "B", "C", "D"])

    icp_path = _icp_path(sample_icp, tmp_path)
    result = run_pipeline(cfg, sample_icp, icp_path, autopilot=True)

    assert result["stage"] == "exported"
    assert result["counts"]["runner"] == "none"
    assert result["counts"]["dm_labeled"] + result["counts"]["dm_unlabeled"] >= 1
    assert "drafted" in result["counts"]
    assert not any("drafting unavailable" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Additional: AgentFailed during labeling is caught and falls back to heuristics
# ---------------------------------------------------------------------------
def test_auto_label_agent_failed_caught_and_fallback(cfg, sample_icp):
    """AgentFailed exception from runner is caught and auto_label falls back to heuristics."""
    from leadforge.agent_runner import AgentFailed
    conn = db.connect(cfg.db_path)

    from leadforge.models import Business, Person
    db.upsert_business(conn, Business(id="biz_fail", name="Fail Biz", name_norm="fail biz",
                                      dedupe_key="dk:fail", source="gosom"))
    db.add_person(conn, Person(business_id="biz_fail", name="Jane Smith", title="Owner",
                               snippet="Jane Smith founded the shop."))

    def failing_runner(lines):
        raise AgentFailed("agent command exited 1: claude -p ...")

    out = dm.auto_label(conn, sample_icp, cfg, runner=failing_runner)
    assert out["runner"] == "none"
    assert out["labeled"] == 1
    assert out["unlabeled"] == 0
