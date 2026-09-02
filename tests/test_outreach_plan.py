"""U9E#3 — `outreach plan`: enrol scored leads, every exclusion counted by reason, chain dedupe."""

from __future__ import annotations

from leadforge import db
from leadforge.outreach.identity import add_identity
from leadforge.outreach.plan import plan_targets
from tests.outreach_helpers import make_run, make_scored_business


def _icp(region_profile="us"):
    from leadforge.models import ICP

    return ICP.model_validate({
        "campaign": "test-outreach", "offer": {"what": "web redesign", "value_prop": "more jobs"},
        "target": {"categories": ["auto repair"], "geography": {"areas": ["Leeds"], "country": "GB"}},
        "compliance": {"region_profile": region_profile},
    })


def test_plan_enrols_eligible_tier_and_counts_exclusions(cfg, conn):
    run_id = make_run(conn)
    add_identity(conn, label="ident1", from_email="sales@acme.com")

    good = make_scored_business(conn, run_id, id_="good1", tier="A", email="owner@abbeyauto.co.uk")
    tier_c = make_scored_business(conn, run_id, id_="tierc", tier="C", email="owner@tierc.co.uk")
    no_email = make_scored_business(conn, run_id, id_="noemail", tier="A", email=None, domain="noemail.co.uk")
    dead = make_scored_business(conn, run_id, id_="dead1", tier="A", domain="deadsite.co.uk", email="x@deadsite.co.uk",
                                enrich_extra={"error": "connection refused", "signals": {"http_status": 502}})

    result = plan_targets(conn, cfg, _icp(), campaign="outreach-camp", run_id=run_id, tiers=["A", "B"],
                          identity_label="ident1")
    counts = result["counts"]

    assert counts["enrolled"] == 1
    assert counts["no_sendable_email"] >= 1  # no_email business
    assert counts["site_dead"] >= 1  # dead business
    row = conn.execute("SELECT * FROM outreach_targets WHERE business_id=?", (good,)).fetchone()
    assert row is not None and row["campaign"] == "outreach-camp" and row["state"] == "enrolled"
    # tier C excluded entirely by the --tier filter, not by an exclusion reason
    assert not conn.execute("SELECT 1 FROM outreach_targets WHERE business_id=?", (tier_c,)).fetchone()
    assert not conn.execute("SELECT 1 FROM outreach_targets WHERE business_id=?", (no_email,)).fetchone()
    assert not conn.execute("SELECT 1 FROM outreach_targets WHERE business_id=?", (dead,)).fetchone()


def test_plan_never_enrols_risky_inferred_unknown_invalid_tiers(cfg, conn):
    run_id = make_run(conn)
    add_identity(conn, label="ident1", from_email="sales@acme.com")
    for tier_label in ("risky", "inferred", "unknown", "invalid"):
        make_scored_business(conn, run_id, id_=f"biz_{tier_label}", tier="A",
                             email=f"x@{tier_label}.co.uk", email_tier=tier_label)
    result = plan_targets(conn, cfg, _icp(), campaign="camp2", run_id=run_id, tiers=["A"], identity_label="ident1")
    assert result["counts"]["enrolled"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM outreach_targets").fetchone()["c"] == 0


def test_plan_chain_dedupe_keeps_highest_score(cfg, conn):
    run_id = make_run(conn)
    add_identity(conn, label="ident1", from_email="sales@acme.com")
    # two locations of the same chain share a domain -> db.chain_map keys them together
    make_scored_business(conn, run_id, id_="chain_low", name="Chain Shop Leeds", tier="B",
                              domain="bigchain.co.uk", email="leeds@bigchain.co.uk")
    b2 = make_scored_business(conn, run_id, id_="chain_high", name="Chain Shop York", tier="A",
                              domain="bigchain.co.uk", email="york@bigchain.co.uk")

    result = plan_targets(conn, cfg, _icp(), campaign="camp3", run_id=run_id, tiers=["A", "B"],
                          identity_label="ident1")
    assert result["counts"]["enrolled"] == 1
    assert result["counts"]["chain_duplicate"] == 1
    enrolled_rows = conn.execute("SELECT business_id FROM outreach_targets WHERE campaign='camp3'").fetchall()
    assert {r["business_id"] for r in enrolled_rows} == {b2}  # tier A (higher score) wins, not b1


def test_plan_suppressed_and_already_enrolled_excluded(cfg, conn):
    run_id = make_run(conn)
    add_identity(conn, label="ident1", from_email="sales@acme.com")
    make_scored_business(conn, run_id, id_="supp1", tier="A", domain="suppressed.co.uk",
                              email="a@suppressed.co.uk")
    # suppress the EMAIL, not the domain: db.scores_for_run() itself already filters out any row whose
    # domain/place_id is suppressed (docs/09's shared query, export.py relies on the same behavior) —
    # an email-level suppression is what actually exercises plan.py's own re-check.
    db.suppress(conn, "email", "a@suppressed.co.uk", reason="test")

    make_scored_business(conn, run_id, id_="dupe1", tier="A", domain="dupe.co.uk", email="a@dupe.co.uk")

    result1 = plan_targets(conn, cfg, _icp(), campaign="camp4", run_id=run_id, tiers=["A"], identity_label="ident1")
    assert result1["counts"]["suppressed"] == 1
    assert result1["counts"]["enrolled"] == 1

    result2 = plan_targets(conn, cfg, _icp(), campaign="camp4", run_id=run_id, tiers=["A"], identity_label="ident1")
    assert result2["counts"]["already_enrolled"] == 1
    assert result2["counts"]["enrolled"] == 0


def test_plan_entity_gate_excludes_dissolved_company(cfg, conn):

    run_id = make_run(conn)
    add_identity(conn, label="ident1", from_email="sales@acme.com")
    bid = make_scored_business(
        conn, run_id, id_="entitygate1", tier="A", domain="someshop.co.uk", email="info@someshop.co.uk",
        email_tier="valid", affinity="own_domain",
        enrich_extra={"registry_checked": True,
                     "registry_profile": {"company_number": "999", "company_status": "dissolved"}},
    )
    # UK region + a dissolved-company registry match -> BASIS_CONFIRM regardless of email affinity
    result = plan_targets(conn, cfg, _icp(region_profile="uk"), campaign="camp5", run_id=run_id, tiers=["A"],
                          identity_label="ident1")
    assert result["counts"]["enrolled"] == 0
    assert result["counts"]["entity_gate"] == 1
    assert not conn.execute("SELECT 1 FROM outreach_targets WHERE business_id=?", (bid,)).fetchone()


def test_plan_unknown_identity_raises(cfg, conn):
    import pytest

    from leadforge.util import LeadForgeError

    run_id = make_run(conn)
    make_scored_business(conn, run_id, id_="b1", tier="A")
    with pytest.raises(LeadForgeError):
        plan_targets(conn, cfg, _icp(), campaign="camp6", run_id=run_id, tiers=["A"], identity_label="ghost")


def test_plan_respects_limit(cfg, conn):
    run_id = make_run(conn)
    add_identity(conn, label="ident1", from_email="sales@acme.com")
    for i in range(3):
        make_scored_business(conn, run_id, id_=f"lim{i}", tier="A", domain=f"lim{i}.co.uk", email=f"a@lim{i}.co.uk")
    result = plan_targets(conn, cfg, _icp(), campaign="camp7", run_id=run_id, tiers=["A"], identity_label="ident1",
                          limit=2)
    assert result["counts"]["enrolled"] == 2


# ---------------------------------------------------------------------------------------- watched-fail
#   test_plan_never_enrols_risky_inferred_unknown_invalid_tiers: compliance._SENDABLE_TIERS temporarily
#     widened to include "risky" -> counts["enrolled"] became 1 where 0 was expected -> red for the
#     right reason. Restored (compliance.py is not mine to edit; verified by reading, not patching).
#   test_plan_chain_dedupe_keeps_highest_score: plan.py's `seen_chain_keys.add` line temporarily moved
#     to fire on every candidate (not only enrolled ones) -> b2 stopped being enrolled (its chain key
#     falsely looked already-claimed) -> the `enrolled == 1` assertion failed -> red for the right
#     reason. Restored.
#   test_plan_suppressed_and_already_enrolled_excluded: the `already` early-continue temporarily
#     removed -> a second plan() call raised a UNIQUE constraint error instead of counting
#     already_enrolled -> red (a crash, not a silent pass) for the right reason. Restored.
#   test_plan_entity_gate_excludes_dissolved_company: the `elig["basis"] in (BASIS_CONFIRM, ...)` check
#     temporarily removed from plan.py -> the dissolved-company row was silently enrolled (enrolled==1)
#     -> red for the right reason. Restored.
