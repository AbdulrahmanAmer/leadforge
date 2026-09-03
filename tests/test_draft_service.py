"""v0.4 "autopilot" unit B — draft.service: the pure functions moved out of draft/cli.py
(identity_for/best_contact/ensure_targets/load_packets/build_packets/apply_drafts, exercised
end-to-end by tests/test_draft_cli.py already), plus the new headless entry point `auto_draft`.

`auto_draft` reads `cfg.agent.batch` and `cfg.draft.auto_*` — v0.4 fields owned by unit A's
config.py, which this worktree's config.py does not carry yet (ownership: B never edits
config.py). `_autopilot_cfg` below is a thin read-through proxy that layers just those fields over
a real `Config`/`DraftCfg` for this test file only; once A's config.py additions land, `auto_draft`
reads the exact same attribute names off the real objects unchanged.
"""

from __future__ import annotations

import json

from leadforge import db
from leadforge.draft import service
from leadforge.models import ICP, Business, Contact, Score, ScoreFactor


class _Proxy:
    """Read-through attribute proxy: `overrides[name]` if present, else delegates to `target`."""

    def __init__(self, target, **overrides):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_target"), name)


def _autopilot_cfg(cfg, *, batch=40, auto_tiers=("A", "B"), auto_max=500,
                   auto_purpose="gainlev_leadgen", template_fallback=True):
    draft_proxy = _Proxy(cfg.draft, auto=True, auto_purpose=auto_purpose, auto_tiers=list(auto_tiers),
                         auto_max=auto_max, template_fallback=template_fallback)
    agent_proxy = _Proxy(object(), batch=batch)
    return _Proxy(cfg, draft=draft_proxy, agent=agent_proxy)


def _icp(**overrides) -> ICP:
    base = {
        "campaign": "test-camp",
        "offer": {"what": "website refresh", "value_prop": "more bookings", "sender": "GainLev"},
        "target": {"categories": ["auto repair shop"], "geography": {"areas": ["Leeds"], "country": "GB"}},
        "compliance": {"region_profile": "uk"},
    }
    base.update(overrides)
    return ICP.model_validate(base)


def _seed_business(conn, biz_id, *, tier, run_id, name=None, website=None, enrich=None, campaign=None):
    base = dict(id=biz_id, name=name or f"Acme {biz_id}", category="Car repair", address_city="Leeds",
               source="gosom", dedupe_key=f"dk-{biz_id}", website=website, enrich=enrich or {})
    db.upsert_business(conn, Business(**base))
    db.save_score(conn, Score(business_id=biz_id, run_id=run_id, total=85, tier=tier,
                              factors=[ScoreFactor(factor="x", group="fit", weight=1, score=1, points=1, why="w")]))
    conn.commit()


_HIRING_ENRICH = {"crawled_at": "2026-01-01T00:00:00Z", "pages": 1, "signals": {"careers": True}}


# ------------------------------------------------------------------------- moved helpers (unit-level)
def test_identity_for_falls_back_id_then_only_identity_then_bare_default(conn):
    assert service.identity_for(conn, None, "Fallback Name") == {
        "from_name": "Fallback Name", "label": "default", "postal_address": "",
        "privacy_url": "", "unsubscribe_mailto": "", "unsubscribe_url": "",
    }
    conn.execute(
        "INSERT INTO sending_identities(label,from_name,created_at) VALUES('only-one','Only One','2026-01-01')"
    )
    conn.commit()
    row = service.identity_for(conn, None, "Fallback Name")
    assert row["from_name"] == "Only One"
    row_by_id = service.identity_for(conn, None, "Fallback Name")
    assert row_by_id["label"] == "only-one"
    # an id that doesn't exist falls through the same way None would
    assert service.identity_for(conn, 999, "Fallback Name")["from_name"] == "Only One"


def test_best_contact_prefers_own_domain_over_unlinked_freemail(cfg, conn):
    db.upsert_business(conn, Business(id="b1", name="Acme", name_norm="acme", dedupe_key="dk-b1", domain="acme.example"))
    db.add_contact(conn, Contact(business_id="b1", kind="email", value="random@gmail.com",
                                 tier="valid", affinity="freemail_unlinked"))
    db.add_contact(conn, Contact(business_id="b1", kind="email", value="hi@acme.example",
                                 tier="valid", affinity="own_domain"))
    b = conn.execute("SELECT * FROM businesses WHERE id='b1'").fetchone()
    contact = service.best_contact(conn, b, cfg)
    assert contact is not None and contact["value"] == "hi@acme.example"


def test_best_contact_returns_none_when_only_unlinked_freemail_available(cfg, conn):
    db.upsert_business(conn, Business(id="b1", name="Acme", name_norm="acme", dedupe_key="dk-b1"))
    db.add_contact(conn, Contact(business_id="b1", kind="email", value="random@gmail.com",
                                 tier="valid", affinity="freemail_unlinked"))
    b = conn.execute("SELECT * FROM businesses WHERE id='b1'").fetchone()
    assert service.best_contact(conn, b, cfg) is None


def test_ensure_targets_creates_enrolled_rows_only_for_matching_tiers(conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid)
    _seed_business(conn, "b2", tier="B", run_id=rid)
    _seed_business(conn, "b3", tier="D", run_id=rid)
    ids = service.ensure_targets(conn, rid, {"A", "B"}, "test-camp")
    assert len(ids) == 2
    rows = conn.execute("SELECT business_id, state FROM outreach_targets WHERE campaign='test-camp'").fetchall()
    assert {r["business_id"] for r in rows} == {"b1", "b2"}
    assert all(r["state"] == "enrolled" for r in rows)
    # idempotent: calling again does not duplicate rows (UNIQUE(business_id, campaign))
    ids2 = service.ensure_targets(conn, rid, {"A", "B"}, "test-camp")
    assert sorted(ids2) == sorted(ids)
    assert conn.execute("SELECT COUNT(*) c FROM outreach_targets WHERE campaign='test-camp'").fetchone()["c"] == 2


def test_build_packets_header_shape_and_grade_counts(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, website=None)  # no_website -> segment-only -> grade C
    icp = _icp()
    ids = service.ensure_targets(conn, rid, {"A"}, "test-camp")
    lines, counts = service.build_packets(conn, cfg, icp, ids, "gainlev_leadgen")
    assert len(lines) == 1 + len(ids)
    header = lines[0]
    assert header["purpose"] == "gainlev_leadgen"
    assert header["offer"] == {"what": "website refresh", "value_prop": "more bookings"}
    assert "instructions" in header
    assert counts["targets"] == 1 and counts["grade_c"] == 1
    assert lines[1]["packet"]["co"] == "Acme b1"


def test_load_packets_roundtrips_build_packets_output(cfg, conn, tmp_path):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, website=None)
    icp = _icp()
    ids = service.ensure_targets(conn, rid, {"A"}, "test-camp")
    lines, _ = service.build_packets(conn, cfg, icp, ids, "gainlev_leadgen")
    path = tmp_path / "packets.ndjson"
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    loaded = service.load_packets(path)
    assert loaded[ids[0]] == lines[1]["packet"]


def test_apply_drafts_stores_author_on_both_drafted_and_rejected_rows(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, website=None)
    icp = _icp()
    ids = service.ensure_targets(conn, rid, {"A"}, "test-camp")
    lines, _ = service.build_packets(conn, cfg, icp, ids, "gainlev_leadgen")
    packet_by_target = {ln["target"]: ln["packet"] for ln in lines[1:]}
    tid = ids[0]

    clean = {"target": tid, "subject": "Quick note about Acme b1",
             "observation": "Noticed you don't have a business website live yet.", "used_fact": "no_website"}
    counts = service.apply_drafts(conn, cfg, packet_by_target, [clean], author="template")
    assert counts == {"applied": 1, "rejected": 0, "insufficient_evidence": 0, "skipped": 0}
    row = conn.execute("SELECT * FROM messages WHERE target_id=?", (tid,)).fetchone()
    assert row["state"] == "drafted" and row["author"] == "template"

    fabricated = {"target": tid, "subject": "x", "observation": "We've helped 47 garages this year.",
                 "used_fact": "no_website"}
    counts2 = service.apply_drafts(conn, cfg, packet_by_target, [fabricated], author="agent")
    assert counts2 == {"applied": 0, "rejected": 1, "insufficient_evidence": 0, "skipped": 0}
    rejected_row = conn.execute(
        "SELECT * FROM messages WHERE target_id=? AND state='rejected'", (tid,)
    ).fetchone()
    assert rejected_row["author"] == "agent"


def test_apply_drafts_skips_unknown_target_and_wrong_campaign(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, website=None)
    icp = _icp()
    ids = service.ensure_targets(conn, rid, {"A"}, "test-camp")
    lines, _ = service.build_packets(conn, cfg, icp, ids, "gainlev_leadgen")
    packet_by_target = {ln["target"]: ln["packet"] for ln in lines[1:]}

    counts = service.apply_drafts(conn, cfg, packet_by_target, [{"target": 999999, "subject": "x"}])
    assert counts["skipped"] == 1
    counts2 = service.apply_drafts(conn, cfg, packet_by_target, [{"target": ids[0], "subject": "x"}],
                                   campaign="some-other-campaign")
    assert counts2["skipped"] == 1


def test_draft_instructions_covers_the_no_fabrication_rules():
    text = service.DRAFT_INSTRUCTIONS
    for token in ("used_fact", "abstain", "Do not use any tools", "No prose"):
        assert token in text


# ------------------------------------------------------------------------- auto_draft
def test_auto_draft_returns_zeroed_result_when_no_targets_match(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    icp = _icp()
    acfg = _autopilot_cfg(cfg)
    result = service.auto_draft(conn, acfg, icp, rid, runner=None)
    assert result == {"targets": 0, "drafted": 0, "rejected": 0, "abstained": 0,
                      "skipped": 0, "author": "none", "batches": 0}


def test_auto_draft_with_a_working_runner_marks_author_agent(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, website=None)
    icp = _icp()
    acfg = _autopilot_cfg(cfg, batch=40)
    calls: list[list[str]] = []

    def runner(lines: list[str]) -> list[dict]:
        calls.append(lines)
        out = []
        for ln in lines[1:]:
            row = json.loads(ln)
            co = row["packet"]["co"]
            out.append({"target": row["target"], "subject": f"Quick note about {co}",
                       "observation": "Noticed you don't have a business website live yet.",
                       "used_fact": "no_website"})
        return out

    result = service.auto_draft(conn, acfg, icp, rid, runner=runner)
    assert result["author"] == "agent"
    assert result["drafted"] == 1 and result["rejected"] == 0
    assert result["batches"] == 1 and len(calls) == 1
    assert len(calls[0]) == 2  # header + 1 packet line
    m = conn.execute("SELECT * FROM messages").fetchone()
    assert m["author"] == "agent" and m["state"] == "drafted"


def test_auto_draft_falls_back_to_template_when_the_runner_raises(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg)

    def broken_runner(lines: list[str]) -> list[dict]:
        raise RuntimeError("subprocess exploded")

    result = service.auto_draft(conn, acfg, icp, rid, runner=broken_runner)
    assert result["author"] == "template"
    assert result["drafted"] == 1
    m = conn.execute("SELECT * FROM messages").fetchone()
    assert m["author"] == "template" and m["used_fact"] == "hiring"
    assert json.loads(m["gate_json"])["ok"] is True


def test_auto_draft_produces_no_drafts_when_runner_fails_and_template_fallback_disabled(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg, template_fallback=False)

    def broken_runner(lines: list[str]) -> list[dict]:
        raise RuntimeError("boom")

    result = service.auto_draft(conn, acfg, icp, rid, runner=broken_runner)
    assert result["drafted"] == 0 and result["author"] == "none"
    assert conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 0
    t = conn.execute("SELECT state FROM outreach_targets").fetchone()
    assert t["state"] == "enrolled"


def test_auto_draft_uses_template_directly_when_no_runner_is_available(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg)
    result = service.auto_draft(conn, acfg, icp, rid, runner=None)
    assert result["author"] == "template"
    assert result["drafted"] == 1
    assert result["batches"] == 1  # unbatched: all packets in one template pass


def test_auto_draft_no_runner_and_template_fallback_disabled_drafts_nothing(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg, template_fallback=False)
    result = service.auto_draft(conn, acfg, icp, rid, runner=None)
    assert result == {"targets": 1, "drafted": 0, "rejected": 0, "abstained": 0,
                      "skipped": 0, "author": "none", "batches": 0}


def test_auto_draft_is_idempotent_on_resume(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg)

    result1 = service.auto_draft(conn, acfg, icp, rid, runner=None)
    assert result1["drafted"] == 1
    result2 = service.auto_draft(conn, acfg, icp, rid, runner=None)
    assert result2 == {"targets": 0, "drafted": 0, "rejected": 0, "abstained": 0,
                       "skipped": 0, "author": "none", "batches": 0}
    assert conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 1


def test_auto_draft_respects_auto_max_cap(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    for i in range(3):
        _seed_business(conn, f"b{i}", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg, auto_max=2)
    result = service.auto_draft(conn, acfg, icp, rid, runner=None)
    assert result["targets"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 2


def test_auto_draft_batches_runner_calls_by_agent_batch_size(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    for i in range(3):
        _seed_business(conn, f"b{i}", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg, batch=1)
    calls: list[list[str]] = []

    def runner(lines: list[str]) -> list[dict]:
        calls.append(lines)
        row = json.loads(lines[1])
        return [{"target": row["target"], "subject": f"Quick note for {row['packet']['co']}",
                "observation": f"Noticed {row['packet']['co']} has a live careers/jobs page.",
                "used_fact": "hiring"}]

    result = service.auto_draft(conn, acfg, icp, rid, runner=runner)
    assert result["batches"] == 3 and len(calls) == 3
    assert all(len(c) == 2 for c in calls)  # header + exactly one packet line per call
    assert result["drafted"] == 3


def test_auto_draft_uses_explicit_purpose_and_campaign_over_config_and_icp_defaults(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg, auto_purpose="re_engagement")
    result = service.auto_draft(conn, acfg, icp, rid, runner=None, purpose="follow_up", campaign="other-camp")
    assert result["drafted"] == 1
    m = conn.execute("SELECT purpose FROM messages").fetchone()
    assert m["purpose"] == "follow_up"
    t = conn.execute("SELECT campaign FROM outreach_targets").fetchone()
    assert t["campaign"] == "other-camp"


def test_auto_draft_retries_a_rejected_draft_with_the_gate_reasons_then_templates_the_rest(cfg, conn):
    """Live 2026-09-03: 9 of 10 first drafts failed the gate on fixable reasons. One retry carries the
    reasons back to the runner; what still fails gets the deterministic template so the row is never empty."""
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    _seed_business(conn, "b2", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg, batch=40)
    calls: list[list[dict]] = []

    def runner(lines: list[str]) -> list[dict]:
        rows = [json.loads(ln) for ln in lines[1:]]
        calls.append(rows)
        out = []
        for row in rows:
            tid, co = row["target"], row["packet"]["co"]
            if "rejected_attempt" in row and tid == min(r["target"] for r in rows):
                # the retry fixes the first target (quotes the fact value verbatim); the other stays broken
                # (invented number) on every attempt -> template
                out.append({"target": tid, "subject": "Quick note", "observation": "Saw the site has a live careers/jobs page.",
                            "used_fact": "hiring"})
            else:
                out.append({"target": tid, "subject": "Quick note",
                            "observation": "We helped 40 garages like yours.", "used_fact": "hiring"})
        return out

    result = service.auto_draft(conn, acfg, icp, rid, runner=runner)
    assert len(calls) == 2 and result["batches"] == 2
    assert all("rejected_attempt" in r for r in calls[1]) and len(calls[1]) == 2
    assert calls[1][0]["rejected_attempt"]["reasons"]  # the gate's reasons travel with the retry
    assert result["drafted"] == 2 and result["author"] == "mixed"
    by_target = {}
    for m in conn.execute("SELECT target_id, state, author FROM messages WHERE state='drafted'"):
        by_target[m["target_id"]] = m["author"]
    assert sorted(by_target.values()) == ["agent", "template"]


def test_apply_drafts_infers_the_cited_fact_from_a_verbatim_quote_but_never_from_nothing(cfg, conn):
    rid = db.create_run(conn, "icp.yaml", "h")
    _seed_business(conn, "b1", tier="A", run_id=rid, enrich=_HIRING_ENRICH)
    icp = _icp()
    acfg = _autopilot_cfg(cfg)
    tids = service.ensure_targets(conn, rid, {"A"}, icp.campaign)
    lines, _ = service.build_packets(conn, acfg, icp, tids, "gainlev_leadgen")
    packet_by_target = {ln["target"]: ln["packet"] for ln in lines[1:]}
    tid = tids[0]
    quoted = {"target": tid, "subject": "Quick note", "observation": "Noticed the site has a live careers/jobs page."}
    counts = service.apply_drafts(conn, acfg, packet_by_target, [quoted], author="agent")
    assert counts["applied"] == 1 and counts["rejected"] == 0
    assert conn.execute("SELECT used_fact FROM messages WHERE target_id=?", (tid,)).fetchone()[0] == "hiring"
    conn.execute("DELETE FROM messages")
    conn.execute("UPDATE outreach_targets SET state='enrolled'")
    bare = {"target": tid, "subject": "Quick note", "observation": "Hope business is going well this year."}
    counts = service.apply_drafts(conn, acfg, packet_by_target, [bare], author="agent")
    assert counts["applied"] == 0 and counts["rejected"] == 1
    reasons = json.loads(conn.execute("SELECT gate_json FROM messages").fetchone()[0])["reasons"]
    assert any("cites no used_fact" in r for r in reasons)
