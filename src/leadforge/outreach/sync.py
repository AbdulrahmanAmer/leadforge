"""`outreach sync` (v0.3 unit E, docs/09 Wave 2 E #9) — ingest bounces/complaints/unsubscribes/replies.

Two sources, one pipeline:
  1. A webhook spool: JSON files under `cfg.data_path / cfg.outreach.inbox_dir / *.json`, each
     `{"kind": ..., "email": ..., "message_id": ..., "occurred_at": ..., "body": ...}` (a list of
     those, or one such object per file).
  2. IMAP, for any mailbox whose config carries `imap_*_env` names — raw replies are classified from
     their own content (a bounce/complaint webhook never fires for a plain reply-to-inbox flow).

Every event dedupes on `events.dedupe_key` so re-running sync against the same spool file (or the
same still-unread IMAP message) is a no-op the second time. Message content is never printed —
digest counts only; raw bodies stay on disk under `inbox_dir`, never copied into the DB.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import json
import os
import re
import sqlite3
from email.message import Message

from leadforge import db
from leadforge.config import Config
from leadforge.outreach.identity import env_config, list_mailboxes
from leadforge.outreach.states import IllegalTransition, transition
from leadforge.util import LOG, now_iso, sha1_hex

_REPLY_KEYWORDS = {
    "not_interested": ("not interested", "no thanks", "not for us", "please remove", "no thank you"),
    "wrong_person": ("wrong person", "not the right person", "reach out to", "forward this to", "try contacting"),
    "ooo": ("out of office", "on annual leave", "on vacation", "auto-reply", "automatic reply", "ooo"),
    "interested": ("interested", "sounds good", "tell me more", "yes please", "let's talk", "book a call"),
}
_OPTOUT_KEYWORDS = ("unsubscribe", "stop", "remove me", "opt out", "opt-out", "take me off")

_RAW_KIND_KEYWORDS = {
    "bounce_hard": ("undeliverable", "delivery status notification", "550", "no such user", "does not exist",
                    "permanent failure"),
    "bounce_soft": ("mailbox full", "quota exceeded", "temporarily", "try again later", "451", "452"),
    "complaint": ("abuse report", "spam complaint", "this is spam", "arf report", "feedback loop"),
}

_SUPPRESSION_SOURCE = {"bounce_hard": "bounce_hard", "complaint": "complaint", "unsubscribe": "unsubscribe"}


def classify_reply(body: str) -> tuple[str, bool]:
    """-> (classification, is_optout). classification is one of interested/not_interested/
    wrong_person/ooo/other; is_optout is checked separately so an opt-out phrase inside an
    otherwise-ordinary reply still suppresses (source 'reply_optout') without inventing a sixth
    classification value outside the documented five."""
    text = (body or "").casefold()
    is_optout = any(k in text for k in _OPTOUT_KEYWORDS)
    for label, keywords in _REPLY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return label, is_optout
    return "other", is_optout


def classify_raw_kind(subject: str, body: str) -> str:
    """For IMAP-polled raw mail, which arrives with no pre-labeled `kind` (unlike the JSON spool)."""
    text = f"{subject} {body}".casefold()
    for kind in ("bounce_hard", "bounce_soft", "complaint"):
        if any(k in text for k in _RAW_KIND_KEYWORDS[kind]):
            return kind
    if re.search(r"\b5\d\d\b", text) and ("bounce" in text or "delivery" in text or "undeliver" in text):
        return "bounce_hard"
    if any(k in text for k in _OPTOUT_KEYWORDS):
        return "unsubscribe"
    return "reply"


def _find_message(conn: sqlite3.Connection, message_id_header: str | None, email_addr: str | None) -> sqlite3.Row | None:
    if message_id_header:
        row = conn.execute("SELECT * FROM messages WHERE message_id_header=?", (message_id_header,)).fetchone()
        if row:
            return row
    if email_addr:
        return conn.execute(
            """SELECT m.* FROM messages m JOIN outreach_targets t ON t.id=m.target_id
               JOIN contacts c ON c.business_id=t.business_id
               WHERE c.kind='email' AND c.value=? AND m.state='sent'
               ORDER BY m.sent_at DESC LIMIT 1""",
            (email_addr,),
        ).fetchone()
    return None


def _advance_target(conn: sqlite3.Connection, target_id: int, kind: str, is_optout: bool) -> None:
    new_state = {"bounce_hard": "bounced", "bounce_soft": None, "complaint": "opted_out",
                 "unsubscribe": "opted_out"}.get(kind, "replied" if not is_optout else "opted_out")
    if new_state is None:
        return
    try:
        transition(conn, "target", target_id, new_state)
    except IllegalTransition:
        LOG.info("outreach sync: target %s already past a state that allows -> %s; event still recorded",
                 target_id, new_state)


def _ingest_event(conn: sqlite3.Connection, item: dict, counts: dict) -> None:
    kind = item.get("kind", "")
    email_addr = (item.get("email") or "").strip().lower()
    message_id_header = (item.get("message_id") or "").strip() or None
    occurred_at = item.get("occurred_at") or now_iso()
    body = item.get("body", "")

    classification = ""
    is_optout = False
    if kind == "reply":
        classification, is_optout = classify_reply(body)

    dedupe_key = sha1_hex(f"{kind}|{email_addr}|{message_id_header}|{occurred_at}", 20)
    if conn.execute("SELECT 1 FROM events WHERE dedupe_key=?", (dedupe_key,)).fetchone():
        counts["duplicate"] = counts.get("duplicate", 0) + 1
        return

    msg = _find_message(conn, message_id_header, email_addr or None)
    target = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (msg["target_id"],)).fetchone() if msg else None
    business_id = target["business_id"] if target else None

    conn.execute(
        """INSERT INTO events(message_id,business_id,kind,classification,payload_json,dedupe_key,occurred_at,ingested_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (msg["id"] if msg else None, business_id, kind, classification,
         json.dumps({"email": email_addr, "optout": is_optout}), dedupe_key, occurred_at, now_iso()),
    )
    conn.commit()
    counts[kind] = counts.get(kind, 0) + 1
    if msg is None:
        counts["unmatched"] = counts.get("unmatched", 0) + 1

    source = _SUPPRESSION_SOURCE.get(kind)
    if kind == "reply" and is_optout:
        source = "reply_optout"
    if source and email_addr:
        db.suppress(conn, "email", email_addr, reason=source, source=source,
                    client_id=(target["client_id"] if target else ""), business_id=business_id)
        counts["suppressed"] = counts.get("suppressed", 0) + 1

    if target is not None:
        _advance_target(conn, target["id"], kind, is_optout)


def sync_spool(conn: sqlite3.Connection, cfg: Config, counts: dict) -> None:
    inbox_dir = cfg.data_path / cfg.outreach.inbox_dir
    inbox_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(inbox_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            LOG.warning("outreach sync: skipping unreadable spool file %s: %s", path.name, e)
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                _ingest_event(conn, item, counts)


def _extract_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True)
        return payload.decode(msg.get_content_charset() or "utf-8", "replace") if payload else str(msg.get_payload())
    except Exception:  # noqa: BLE001
        return str(msg.get_payload())


def _poll_imap(conn: sqlite3.Connection, mailbox_row: sqlite3.Row, counts: dict,
               imap_client_cls: type | None = None) -> None:
    cfg_kv = env_config(mailbox_row)
    host = os.environ.get(cfg_kv.get("imap_host_env", ""), "")
    user = os.environ.get(cfg_kv.get("imap_user_env", ""), "")
    password = os.environ.get(cfg_kv.get("imap_password_env", ""), "")
    if not (host and user and password):
        return
    client_cls = imap_client_cls or imaplib.IMAP4_SSL
    client = client_cls(host)
    try:
        client.login(user, password)
        client.select("INBOX")
        _typ, data = client.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        for msg_num in ids:
            _typ, msg_data = client.fetch(msg_num, "(RFC822)")
            raw = msg_data[0][1] if msg_data and msg_data[0] else b""
            if not raw:
                continue
            parsed = email.message_from_bytes(raw)
            from_addr = email.utils.parseaddr(parsed.get("From", ""))[1].strip().lower()
            in_reply_to = (parsed.get("In-Reply-To") or parsed.get("References") or "").split()[-1] \
                if (parsed.get("In-Reply-To") or parsed.get("References")) else ""
            subject = parsed.get("Subject", "") or ""
            body = _extract_text(parsed)
            item = {"kind": classify_raw_kind(subject, body), "email": from_addr, "message_id": in_reply_to,
                    "occurred_at": now_iso(), "body": body}
            _ingest_event(conn, item, counts)
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass


def sync_inbox(conn: sqlite3.Connection, cfg: Config, *, imap_client_cls: type | None = None) -> dict:
    counts: dict[str, int] = {}
    sync_spool(conn, cfg, counts)
    for mb in list_mailboxes(conn):
        cfg_kv = env_config(mb)
        if cfg_kv.get("imap_host_env"):
            _poll_imap(conn, mb, counts, imap_client_cls)
    return counts
