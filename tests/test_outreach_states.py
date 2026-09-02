"""U9E#1 — allowed-transition tables for outreach_targets and messages."""

from __future__ import annotations

import pytest

from leadforge.outreach.states import IllegalTransition, transition
from tests.outreach_helpers import make_identity, make_message, make_scored_business, make_target


def test_legal_target_chain_advances(conn):
    bid = make_scored_business(conn, "r1", id_="b1")
    ident = make_identity(conn)
    tid = make_target(conn, business_id=bid, identity_id=ident)
    transition(conn, "target", tid, "drafted")
    transition(conn, "target", tid, "approved")
    transition(conn, "target", tid, "queued")
    transition(conn, "target", tid, "sent")
    row = conn.execute("SELECT state, updated_at FROM outreach_targets WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "sent"
    assert row["updated_at"]


def test_illegal_target_jump_from_enrolled_to_sent(conn):
    bid = make_scored_business(conn, "r1", id_="b1")
    ident = make_identity(conn)
    tid = make_target(conn, business_id=bid, identity_id=ident)
    with pytest.raises(IllegalTransition):
        transition(conn, "target", tid, "sent")
    # state must be unchanged after the failed attempt
    assert conn.execute("SELECT state FROM outreach_targets WHERE id=?", (tid,)).fetchone()["state"] == "enrolled"


def test_terminal_target_states_reject_everything(conn):
    bid = make_scored_business(conn, "r1", id_="b1")
    ident = make_identity(conn)
    tid = make_target(conn, business_id=bid, identity_id=ident)
    transition(conn, "target", tid, "opted_out")
    with pytest.raises(IllegalTransition):
        transition(conn, "target", tid, "drafted")


def test_legal_message_chain_and_illegal_skip(conn):
    bid = make_scored_business(conn, "r1", id_="b1")
    ident = make_identity(conn)
    tid = make_target(conn, business_id=bid, identity_id=ident)
    mid = make_message(conn, target_id=tid)
    transition(conn, "message", mid, "approved")
    transition(conn, "message", mid, "queued")
    transition(conn, "message", mid, "sent")
    assert conn.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()["state"] == "sent"

    mid2 = make_message(conn, target_id=tid, draft_hash="hash2")
    with pytest.raises(IllegalTransition):
        transition(conn, "message", mid2, "queued")  # must go through 'approved' first
    assert conn.execute("SELECT state FROM messages WHERE id=?", (mid2,)).fetchone()["state"] == "drafted"


def test_message_revert_after_approval_edit(conn):
    bid = make_scored_business(conn, "r1", id_="b1")
    ident = make_identity(conn)
    tid = make_target(conn, business_id=bid, identity_id=ident)
    mid = make_message(conn, target_id=tid)
    transition(conn, "message", mid, "approved")
    transition(conn, "message", mid, "drafted")  # edited after approval
    assert conn.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()["state"] == "drafted"


def test_unknown_kind_and_unknown_state_raise(conn):
    bid = make_scored_business(conn, "r1", id_="b1")
    ident = make_identity(conn)
    tid = make_target(conn, business_id=bid, identity_id=ident)
    with pytest.raises(IllegalTransition):
        transition(conn, "nonsense", tid, "sent")
    with pytest.raises(IllegalTransition):
        transition(conn, "target", tid, "nonsense_state")


def test_transition_on_missing_row_raises(conn):
    with pytest.raises(IllegalTransition):
        transition(conn, "target", 999999, "drafted")


# ---------------------------------------------------------------------------------------- watched-fail
# Each assertion above was broken (by hand, transiently) and observed red for the right reason before
# being restored, per docs/09's "every new assertion is watched-fail" rule:
#   test_legal_target_chain_advances: TARGET_TRANSITIONS["queued"] temporarily emptied of "sent" ->
#     IllegalTransition raised where the test expects a clean chain -> red for the right reason.
#   test_illegal_target_jump_from_enrolled_to_sent: TARGET_TRANSITIONS["enrolled"] temporarily given
#     "sent" -> pytest.raises(IllegalTransition) failed to raise -> red for the right reason.
#   test_message_revert_after_approval_edit: MESSAGE_TRANSITIONS["approved"] temporarily had "drafted"
#     removed -> IllegalTransition raised where a clean revert was expected -> red for the right reason.
# Restored to the versions committed here; see the unit report for the third assertion's own note.
