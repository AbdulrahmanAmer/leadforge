"""`outreach send` (v0.3 unit E, docs/09 Wave 2 E #8) — dry-run by default, `--live` guarded.

Every gate re-checks LIVE state at send time, not the state a message was approved against:
suppression, eligibility, mailbox status/cap/window are all re-derived from the current DB, so a
lead suppressed (or a mailbox paused, or the clock moved past the send window) *between* approval
and send is caught here even though the message was approved in good faith earlier.

`NOW_FN` is the injectable clock docs/09 asks for — tests monkeypatch `send.NOW_FN` to pin the daily
cap and send-window checks instead of depending on wall-clock time.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from leadforge import compliance, db
from leadforge.config import Config
from leadforge.enrich.validate import rank_email_contacts
from leadforge.outreach import render
from leadforge.outreach.identity import get_mailbox, mailboxes_for_identity
from leadforge.outreach.plan import site_is_dead
from leadforge.outreach.states import transition
from leadforge.outreach.transport import get_transport
from leadforge.score import fill_email_affinity
from leadforge.util import LeadForgeError, now_iso


def _default_now() -> datetime:
    return datetime.now(UTC)


NOW_FN: Callable[[], datetime] = _default_now


def _approved_messages(conn: sqlite3.Connection, campaign: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT m.* FROM messages m JOIN outreach_targets t ON t.id=m.target_id
           WHERE t.campaign=? AND m.state='approved' ORDER BY m.id""",
        (campaign,),
    ).fetchall()


def _target_email(conn: sqlite3.Connection, business_id: str) -> str | None:
    biz = conn.execute("SELECT domain FROM businesses WHERE id=?", (business_id,)).fetchone()
    domain = biz["domain"] if biz else None
    contacts_filled = fill_email_affinity(db.contacts_for(conn, business_id), domain)
    ranked = [c for c in rank_email_contacts(contacts_filled) if c["tier"] not in ("invalid", "inferred")]
    return ranked[0]["value"] if ranked else None


def _recompute_eligibility(conn: sqlite3.Connection, cfg: Config, target: sqlite3.Row, biz: sqlite3.Row,
                            suppressed: bool) -> dict:
    stored = json.loads(target["eligibility_json"] or "{}")
    region_profile = stored.get("_region_profile", "us")
    people = db.people_for(conn, biz["id"])
    contacts_filled = fill_email_affinity(db.contacts_for(conn, biz["id"]), biz["domain"])
    entity = compliance.entity_type(biz, people)
    enrich = json.loads(biz["enrich_json"] or "{}")
    dead = site_is_dead(enrich)
    return compliance.email_eligibility(
        biz, contacts_filled, entity, region_profile,
        freemail_policy=cfg.validation.freemail_policy, require_corporate=cfg.outreach.require_corporate,
        suppressed=suppressed, site_dead=dead,
    )


def dry_run(conn: sqlite3.Connection, cfg: Config, *, campaign: str, max_n: int | None = None) -> dict:
    """Renders every approved message through FileTransport and marks NOTHING sent."""
    transport = get_transport("file", outbox_dir=cfg.data_path / cfg.outreach.outbox_dir)
    rows = _approved_messages(conn, campaign)
    if max_n is not None:
        rows = rows[:max_n]
    would_send, paths, skipped = 0, [], 0
    for m in rows:
        target = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (m["target_id"],)).fetchone()
        if target is None:
            skipped += 1
            continue
        biz = conn.execute("SELECT * FROM businesses WHERE id=?", (target["business_id"],)).fetchone()
        identity = (conn.execute("SELECT * FROM sending_identities WHERE id=?", (target["identity_id"],)).fetchone()
                    if target["identity_id"] else None)
        to_email = _target_email(conn, target["business_id"])
        if not to_email or identity is None or biz is None:
            skipped += 1
            continue
        rendered = render.render_message(conn, target_id=target["id"], identity=identity, to_email=to_email,
                                          subject=m["subject"] or "", body_text=m["body_text"] or "",
                                          business_name=biz["name"])
        _, path = transport.send(rendered, None)
        would_send += 1
        paths.append(path)
    return {
        "counts": {"would_send": would_send, "candidates": len(rows), "skipped_no_recipient": skipped},
        "outbox_dir": str(transport.outbox_dir), "artifacts": paths,
    }


def _check_breaker(conn: sqlite3.Connection, cfg: Config, mailbox_id: int) -> bool:
    last = conn.execute(
        "SELECT id FROM messages WHERE mailbox_id=? AND state='sent' ORDER BY sent_at DESC LIMIT 100",
        (mailbox_id,),
    ).fetchall()
    ids = [r["id"] for r in last]
    n = len(ids)
    if n == 0:
        return False
    q = ",".join("?" * len(ids))
    hard_bounces = conn.execute(
        f"SELECT COUNT(*) c FROM events WHERE message_id IN ({q}) AND classification='bounce_hard'", ids
    ).fetchone()["c"]
    complaints = conn.execute(
        f"SELECT COUNT(*) c FROM events WHERE message_id IN ({q}) AND classification='complaint'", ids
    ).fetchone()["c"]
    bounce_rate, complaint_rate = hard_bounces / n, complaints / n
    if bounce_rate >= cfg.outreach.bounce_rate_pause or complaint_rate >= cfg.outreach.complaint_rate_pause:
        reason = f"circuit breaker: hard-bounce rate {bounce_rate:.3f} or complaint rate {complaint_rate:.3f} over last {n} sends"
        conn.execute("UPDATE mailboxes SET status='paused', paused_reason=? WHERE id=?", (reason, mailbox_id))
        conn.commit()
        return True
    return False


def live_send(conn: sqlite3.Connection, cfg: Config, *, campaign: str, i_am: str,
              mailbox_addr: str | None = None, max_n: int | None = None) -> dict:
    if not cfg.outreach.armed:
        raise LeadForgeError("outreach.armed is false in leadforge.yaml — arm it before --live")
    if not i_am or not i_am.strip():
        raise LeadForgeError("--live requires --i-am NAME")

    counts = {
        "sent": 0, "unknown": 0, "skipped_not_approver": 0, "skipped_hash_mismatch": 0,
        "skipped_no_recipient": 0, "skipped_suppressed": 0, "skipped_ineligible": 0,
        "skipped_mailbox_inactive": 0, "skipped_cap": 0, "skipped_window": 0, "breaker_paused": 0,
    }
    rows = _approved_messages(conn, campaign)
    if max_n is not None:
        rows = rows[:max_n]

    for m in rows:
        if m["approved_by"] != i_am:
            counts["skipped_not_approver"] += 1
            continue
        if m["approved_hash"] != m["draft_hash"]:
            transition(conn, "message", m["id"], "drafted")
            conn.execute(
                "UPDATE messages SET approved_by='', approved_at=NULL, approved_hash='', updated_at=? WHERE id=?",
                (now_iso(), m["id"]),
            )
            conn.commit()
            counts["skipped_hash_mismatch"] += 1
            continue

        target = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (m["target_id"],)).fetchone()
        biz = conn.execute("SELECT * FROM businesses WHERE id=?", (target["business_id"],)).fetchone()
        identity = (conn.execute("SELECT * FROM sending_identities WHERE id=?", (target["identity_id"],)).fetchone()
                    if target["identity_id"] else None)
        to_email = _target_email(conn, target["business_id"])
        if not to_email or identity is None or biz is None:
            counts["skipped_no_recipient"] += 1
            continue

        suppressed = db.is_suppressed(conn, to_email, biz["domain"])
        if suppressed:
            counts["skipped_suppressed"] += 1
            continue

        elig = _recompute_eligibility(conn, cfg, target, biz, suppressed)
        if not elig["eligible"]:
            counts["skipped_ineligible"] += 1
            continue

        mailbox = None
        if mailbox_addr:
            candidate = get_mailbox(conn, mailbox_addr)
            if candidate is not None and candidate["identity_id"] == identity["id"] and candidate["status"] == "active":
                mailbox = candidate
        else:
            actives = [mb for mb in mailboxes_for_identity(conn, identity["id"]) if mb["status"] == "active"]
            mailbox = actives[0] if actives else None
        if mailbox is None:
            counts["skipped_mailbox_inactive"] += 1
            continue

        today = NOW_FN().strftime("%Y-%m-%d")
        sent_today = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE mailbox_id=? AND state='sent' AND substr(sent_at,1,10)=?",
            (mailbox["id"], today),
        ).fetchone()["c"]
        if sent_today >= mailbox["daily_cap"]:
            counts["skipped_cap"] += 1
            continue

        tz = ZoneInfo(cfg.outreach.timezone)
        local_now = NOW_FN().astimezone(tz).time()
        start_s, end_s = cfg.outreach.send_window.split("-")
        start_t, end_t = dtime.fromisoformat(start_s), dtime.fromisoformat(end_s)
        if not (start_t <= local_now <= end_t):
            counts["skipped_window"] += 1
            continue

        # claim: approved -> queued (all pre-conditions above already passed)
        transition(conn, "message", m["id"], "queued")
        conn.execute("UPDATE messages SET queued_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), m["id"]))
        if target["state"] == "approved":
            transition(conn, "target", target["id"], "queued")
        conn.commit()

        rendered = render.render_message(conn, target_id=target["id"], identity=identity, to_email=to_email,
                                          subject=m["subject"] or "", body_text=m["body_text"] or "",
                                          business_name=biz["name"])
        transport = get_transport(mailbox["transport"], outbox_dir=cfg.data_path / cfg.outreach.outbox_dir)
        try:
            provider_message_id, _resp = transport.send(rendered, mailbox)
        except Exception as e:  # noqa: BLE001 — any transport failure: unknown, never auto-requeued
            transition(conn, "message", m["id"], "unknown")
            conn.execute("UPDATE messages SET error=?, updated_at=? WHERE id=?", (str(e)[:200], now_iso(), m["id"]))
            target = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (target["id"],)).fetchone()
            if target["state"] == "queued":
                transition(conn, "target", target["id"], "unknown")
            conn.commit()
            counts["unknown"] += 1
            continue

        transition(conn, "message", m["id"], "sent")
        conn.execute(
            "UPDATE messages SET sent_at=?, provider_message_id=?, mailbox_id=?, message_id_header=?, updated_at=? WHERE id=?",
            (now_iso(), provider_message_id, mailbox["id"], rendered.message_id_header, now_iso(), m["id"]),
        )
        target = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (target["id"],)).fetchone()
        if target["state"] == "queued":
            transition(conn, "target", target["id"], "sent")
        conn.commit()
        counts["sent"] += 1

        if _check_breaker(conn, cfg, mailbox["id"]):
            counts["breaker_paused"] += 1

    return {"counts": counts}
