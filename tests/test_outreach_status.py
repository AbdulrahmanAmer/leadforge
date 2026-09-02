"""U9E#10 — `outreach status` counts and `outreach outcome add`."""

from __future__ import annotations

import pytest

from leadforge import db
from leadforge.outreach import status as status_mod
from leadforge.util import LeadForgeError
from tests.outreach_helpers import make_identity, make_mailbox, make_run, make_scored_business, make_target


def test_status_counts_by_state(conn):
    run_id = make_run(conn)
    ident = make_identity(conn)
    make_mailbox(conn)
    b1 = make_scored_business(conn, run_id, id_="s1")
    b2 = make_scored_business(conn, run_id, id_="s2")
    make_target(conn, business_id=b1, campaign="camp1", identity_id=ident, state="enrolled")
    make_target(conn, business_id=b2, campaign="camp1", identity_id=ident, state="drafted")

    rep = status_mod.status_report(conn, campaign="camp1")
    assert rep["target_states"] == {"enrolled": 1, "drafted": 1}
    assert rep["mailboxes"][0]["address"] == "sales@acme-agency.com"
    assert rep["mailboxes"][0]["daily_cap"] == 30
    assert rep["unknown_sends"] == 0


def test_status_reports_mailbox_cap_and_paused_reason(conn):
    make_identity(conn)
    make_mailbox(conn)
    conn.execute("UPDATE mailboxes SET status='paused', paused_reason='too many bounces' WHERE address=?",
                ("sales@acme-agency.com",))
    conn.commit()
    rep = status_mod.status_report(conn)
    mb = rep["mailboxes"][0]
    assert mb["status"] == "paused" and mb["paused_reason"] == "too many bounces"


def test_outcome_add_writes_row(conn):
    run_id = make_run(conn)
    bid = make_scored_business(conn, run_id, id_="o1")
    oid = status_mod.outcome_add(conn, business_id=bid, channel="phone", result="interested", notes="called, keen")
    assert oid
    row = conn.execute("SELECT * FROM outcomes WHERE id=?", (oid,)).fetchone()
    assert row["business_id"] == bid and row["channel"] == "phone" and row["result"] == "interested"


def test_outcome_add_opt_out_writes_suppression(conn):
    run_id = make_run(conn)
    bid = make_scored_business(conn, run_id, id_="o2", email="owner@optout.co.uk", domain="optout.co.uk")
    status_mod.outcome_add(conn, business_id=bid, channel="phone", result="opt_out")
    assert db.is_suppressed(conn, "owner@optout.co.uk")
    assert db.is_suppressed(conn, "optout.co.uk")


def test_outcome_add_rejects_unknown_channel_or_result(conn):
    run_id = make_run(conn)
    bid = make_scored_business(conn, run_id, id_="o3")
    with pytest.raises(LeadForgeError):
        status_mod.outcome_add(conn, business_id=bid, channel="carrier_pigeon", result="interested")
    with pytest.raises(LeadForgeError):
        status_mod.outcome_add(conn, business_id=bid, channel="phone", result="maybe_later")


def test_outcome_add_rejects_unknown_business(conn):
    with pytest.raises(LeadForgeError):
        status_mod.outcome_add(conn, business_id="ghost", channel="phone", result="interested")


# ---------------------------------------------------------------------------------------- watched-fail
#   test_outcome_add_opt_out_writes_suppression: the `if result == "opt_out":` block temporarily
#     commented out -> `db.is_suppressed` returned False -> red for the right reason. Restored.
#   test_status_reports_mailbox_cap_and_paused_reason: the mailboxes-list comprehension in
#     status_report temporarily dropped `paused_reason` from the dict -> KeyError on `mb["paused_reason"]`
#     -> red for the right reason. Restored.
