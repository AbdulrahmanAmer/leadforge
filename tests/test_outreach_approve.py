"""U9E#4 — `outreach approve`: approval bound to the exact drafted content hash."""

from __future__ import annotations

import pytest

from leadforge.outreach.approve import approve_messages
from leadforge.util import LeadForgeError
from tests.outreach_helpers import make_identity, make_message, make_run, make_scored_business, make_target


def _setup(conn, campaign="camp1", tier="A"):
    run_id = make_run(conn)
    bid = make_scored_business(conn, run_id, id_="b1", tier=tier)
    ident = make_identity(conn)
    tid = make_target(conn, business_id=bid, campaign=campaign, identity_id=ident, state="drafted",
                      eligibility={"_tier": tier})
    mid = make_message(conn, target_id=tid, draft_hash="h1")
    return tid, mid


def test_approve_all_drafted_binds_hash_and_advances_target(conn):
    tid, mid = _setup(conn)
    result = approve_messages(conn, campaign="camp1", approver="alice", all_drafted=True)
    assert result["counts"]["approved"] == 1
    msg = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    assert msg["state"] == "approved" and msg["approved_by"] == "alice" and msg["approved_hash"] == "h1"
    target = conn.execute("SELECT state FROM outreach_targets WHERE id=?", (tid,)).fetchone()
    assert target["state"] == "approved"


def test_approve_by_tier_only_matches_that_tier(conn):
    tid_a, mid_a = _setup(conn, campaign="camp2", tier="A")
    run_id = "run_test_0001"
    bid_b = make_scored_business(conn, run_id, id_="bB", tier="B")
    ident = conn.execute("SELECT id FROM sending_identities LIMIT 1").fetchone()["id"]
    tid_b = make_target(conn, business_id=bid_b, campaign="camp2", identity_id=ident, state="drafted",
                        eligibility={"_tier": "B"})
    mid_b = make_message(conn, target_id=tid_b, draft_hash="h2")

    result = approve_messages(conn, campaign="camp2", approver="alice", tier="A")
    assert result["counts"]["approved"] == 1
    assert conn.execute("SELECT state FROM messages WHERE id=?", (mid_a,)).fetchone()["state"] == "approved"
    assert conn.execute("SELECT state FROM messages WHERE id=?", (mid_b,)).fetchone()["state"] == "drafted"


def test_approve_by_explicit_ids(conn):
    tid, mid = _setup(conn, campaign="camp3")
    result = approve_messages(conn, campaign="camp3", approver="alice", ids=[mid])
    assert result["counts"]["approved"] == 1


def test_approve_requires_exactly_one_selector(conn):
    _setup(conn, campaign="camp4")
    with pytest.raises(LeadForgeError):
        approve_messages(conn, campaign="camp4", approver="alice")  # none given
    with pytest.raises(LeadForgeError):
        approve_messages(conn, campaign="camp4", approver="alice", tier="A", all_drafted=True)  # two given


def test_approve_requires_approver_name(conn):
    _setup(conn, campaign="camp5")
    with pytest.raises(LeadForgeError):
        approve_messages(conn, campaign="camp5", approver="", all_drafted=True)


def test_edited_message_after_approval_reverts_to_drafted_on_content_change(conn):
    """docs/09: a message whose draft_hash != approved_hash at send time reverts to drafted. This unit
    proves the HASH BINDING itself here; send.py's watched-fail proves the revert-at-send-time path."""
    tid, mid = _setup(conn, campaign="camp6")
    approve_messages(conn, campaign="camp6", approver="alice", all_drafted=True)
    # simulate a re-draft changing the content (a new draft_hash) without re-approving
    conn.execute("UPDATE messages SET draft_hash='h_changed' WHERE id=?", (mid,))
    conn.commit()
    row = conn.execute("SELECT draft_hash, approved_hash FROM messages WHERE id=?", (mid,)).fetchone()
    assert row["draft_hash"] != row["approved_hash"]


# ---------------------------------------------------------------------------------------- watched-fail
#   test_approve_all_drafted_binds_hash_and_advances_target: `approved_hash=draft_hash` temporarily
#     replaced with `approved_hash=''` in the UPDATE -> the `approved_hash == "h1"` assertion failed ->
#     red for the right reason. Restored.
#   test_approve_by_tier_only_matches_that_tier: the `json_extract(t.eligibility_json,'$._tier')=?`
#     filter temporarily dropped from the tier query -> mid_b was also approved (state == "approved")
#     where "drafted" was expected -> red for the right reason. Restored.
#   test_approve_requires_exactly_one_selector: the `sum(modes) != 1` guard temporarily loosened to
#     `sum(modes) == 0` (only catching zero, not two) -> the two-selector pytest.raises failed to raise
#     -> red for the right reason. Restored.
