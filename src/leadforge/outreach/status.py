"""`outreach status` and `outreach outcome add` (v0.3 unit E, docs/09 Wave 2 E #10)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from leadforge import db
from leadforge.util import LeadForgeError

_OUTCOME_RESULTS = {"no_answer", "not_interested", "interested", "meeting", "won", "wrong_number", "opt_out"}
_OUTCOME_CHANNELS = {"phone", "email"}


def status_report(conn: sqlite3.Connection, *, campaign: str | None = None) -> dict:
    target_q = "SELECT state, COUNT(*) c FROM outreach_targets"
    msg_q = "SELECT m.state AS state, COUNT(*) c FROM messages m JOIN outreach_targets t ON t.id=m.target_id"
    params: tuple = ()
    if campaign:
        target_q += " WHERE campaign=?"
        msg_q += " WHERE t.campaign=?"
        params = (campaign,)
    target_states = {r["state"]: r["c"] for r in conn.execute(f"{target_q} GROUP BY state", params).fetchall()}
    message_states = {r["state"]: r["c"] for r in conn.execute(f"{msg_q} GROUP BY m.state", params).fetchall()}

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    mailboxes = []
    for mb in conn.execute("SELECT * FROM mailboxes ORDER BY address").fetchall():
        sent_today = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE mailbox_id=? AND state='sent' AND substr(sent_at,1,10)=?",
            (mb["id"], today),
        ).fetchone()["c"]
        last100 = conn.execute(
            "SELECT id FROM messages WHERE mailbox_id=? AND state='sent' ORDER BY sent_at DESC LIMIT 100",
            (mb["id"],),
        ).fetchall()
        ids = [r["id"] for r in last100]
        bounce_rate = complaint_rate = 0.0
        if ids:
            q = ",".join("?" * len(ids))
            hb = conn.execute(f"SELECT COUNT(*) c FROM events WHERE message_id IN ({q}) AND classification='bounce_hard'",
                              ids).fetchone()["c"]
            cp = conn.execute(f"SELECT COUNT(*) c FROM events WHERE message_id IN ({q}) AND classification='complaint'",
                              ids).fetchone()["c"]
            bounce_rate, complaint_rate = hb / len(ids), cp / len(ids)
        mailboxes.append({
            "address": mb["address"], "status": mb["status"], "paused_reason": mb["paused_reason"] or "",
            "sent_today": sent_today, "daily_cap": mb["daily_cap"],
            "bounce_rate": round(bounce_rate, 4), "complaint_rate": round(complaint_rate, 4),
        })

    unknown_q = "SELECT COUNT(*) c FROM messages m JOIN outreach_targets t ON t.id=m.target_id WHERE m.state='unknown'"
    if campaign:
        unknown_q += " AND t.campaign=?"
    unknown_sends = conn.execute(unknown_q, params).fetchone()["c"]

    return {
        "target_states": target_states, "message_states": message_states,
        "mailboxes": mailboxes, "unknown_sends": unknown_sends,
    }


def outcome_add(conn: sqlite3.Connection, *, business_id: str, channel: str, result: str,
                notes: str = "", campaign: str = "", recorded_by: str = "") -> int:
    if channel not in _OUTCOME_CHANNELS:
        raise LeadForgeError(f"unknown outcome --channel '{channel}' (expected one of {sorted(_OUTCOME_CHANNELS)})")
    if result not in _OUTCOME_RESULTS:
        raise LeadForgeError(f"unknown outcome --result '{result}' (expected one of {sorted(_OUTCOME_RESULTS)})")
    biz = conn.execute("SELECT id FROM businesses WHERE id=?", (business_id,)).fetchone()
    if biz is None:
        raise LeadForgeError(f"unknown business id '{business_id}'")
    outcome_id = db.add_outcome(conn, business_id, channel, result, campaign=campaign, notes=notes,
                                recorded_by=recorded_by)
    if result == "opt_out":
        target = conn.execute(
            "SELECT * FROM outreach_targets WHERE business_id=? ORDER BY updated_at DESC LIMIT 1", (business_id,)
        ).fetchone()
        client_id = target["client_id"] if target else ""
        biz_row = conn.execute("SELECT domain FROM businesses WHERE id=?", (business_id,)).fetchone()
        if biz_row and biz_row["domain"]:
            db.suppress(conn, "domain", biz_row["domain"], reason="opt_out", source="reply_optout",
                       client_id=client_id, business_id=business_id)
        for c in db.contacts_for(conn, business_id):
            if c["kind"] == "email":
                db.suppress(conn, "email", c["value"], reason="opt_out", source="reply_optout",
                           client_id=client_id, business_id=business_id)
    return outcome_id
