"""`outreach approve` (v0.3 unit E, docs/09 Wave 2 E #4) — approval bound to exact drafted content.

`approved_hash` is stamped from the message's CURRENT `draft_hash` at the moment of approval. If the
message text later changes (a re-run of `draft apply` on the same row, or any other update to
`body_text`/`draft_hash`) the two hashes stop matching, and `send.py` reverts that message to
`drafted` instead of queuing it — the approver approved specific words, not a row id.
"""

from __future__ import annotations

import sqlite3

from leadforge.outreach.states import transition
from leadforge.util import LeadForgeError, now_iso


def approve_messages(conn: sqlite3.Connection, *, campaign: str, approver: str, tier: str | None = None,
                      ids: list[int] | None = None, all_drafted: bool = False) -> dict:
    if not approver or not approver.strip():
        raise LeadForgeError("approve requires --approver NAME")
    modes = [bool(tier), bool(ids), all_drafted]
    if sum(modes) != 1:
        raise LeadForgeError("approve requires exactly one of --tier, --ids, or --all-drafted")

    if ids:
        rows = conn.execute(
            f"""SELECT m.* FROM messages m JOIN outreach_targets t ON t.id=m.target_id
               WHERE t.campaign=? AND m.state='drafted' AND m.id IN ({','.join('?' * len(ids))})""",
            (campaign, *ids),
        ).fetchall()
    elif tier:
        rows = conn.execute(
            """SELECT m.* FROM messages m JOIN outreach_targets t ON t.id=m.target_id
               WHERE t.campaign=? AND m.state='drafted' AND json_extract(t.eligibility_json,'$._tier')=?""",
            (campaign, tier),
        ).fetchall()
    else:  # all_drafted
        rows = conn.execute(
            """SELECT m.* FROM messages m JOIN outreach_targets t ON t.id=m.target_id
               WHERE t.campaign=? AND m.state='drafted'""",
            (campaign,),
        ).fetchall()

    approved_ids: list[int] = []
    for row in rows:
        transition(conn, "message", row["id"], "approved")
        conn.execute(
            "UPDATE messages SET approved_by=?, approved_at=?, approved_hash=draft_hash, updated_at=? WHERE id=?",
            (approver, now_iso(), now_iso(), row["id"]),
        )
        target = conn.execute("SELECT id, state FROM outreach_targets WHERE id=?", (row["target_id"],)).fetchone()
        if target and target["state"] == "drafted":
            transition(conn, "target", target["id"], "approved")
        approved_ids.append(row["id"])
    conn.commit()

    return {"counts": {"approved": len(approved_ids), "candidates": len(rows)}, "message_ids": approved_ids}
