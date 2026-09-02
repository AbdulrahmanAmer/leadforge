"""Message rendering (v0.3 unit E, docs/09 Wave 2 E #6).

Headers: From, Reply-To (when set), To, Subject, Date, Message-ID (`<lf-<token>@<from-domain>>`
where `token` is an HMAC-SHA256 of the target id, keyed by a workspace secret created on first use
and stored in `meta.outreach_secret` — never derivable without the DB), List-Unsubscribe (mailto
and/or https) and List-Unsubscribe-Post (RFC 8058 one-click, only when an https unsubscribe URL
exists). Body = the drafted text + a deterministic footer: sender name, postal address, the
first-contact privacy line, and an opt-out instruction. Plain text only — no HTML, no tracking
pixels, no wrapped/redirected links.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from email.utils import formatdate

from leadforge.outreach.transport.base import RenderedMessage

_SECRET_KEY = "outreach_secret"


def get_or_create_secret(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (_SECRET_KEY,)).fetchone()
    if row is not None:
        return row["value"]
    secret = secrets.token_hex(32)
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (_SECRET_KEY, secret))
    conn.commit()
    row = conn.execute("SELECT value FROM meta WHERE key=?", (_SECRET_KEY,)).fetchone()
    return row["value"]


def message_id_token(conn: sqlite3.Connection, target_id: int) -> str:
    secret = get_or_create_secret(conn)
    return hmac.new(secret.encode("utf-8"), str(target_id).encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def build_footer(identity: sqlite3.Row, business_name: str) -> str:
    sender = identity["from_name"] or identity["from_email"]
    lines = ["", "--", sender]
    if identity["postal_address"]:
        lines.append(identity["postal_address"])
    privacy = identity["privacy_url"] or ""
    lines.append(f"We found {business_name} in public business listings; see {privacy}. Reply STOP to opt out.")
    opt_bits = []
    if identity["unsubscribe_mailto"]:
        opt_bits.append(f"mailto:{identity['unsubscribe_mailto']}")
    if identity["unsubscribe_url"]:
        opt_bits.append(identity["unsubscribe_url"])
    lines.append("To opt out: " + " or ".join(opt_bits) if opt_bits else "To opt out, reply STOP.")
    return "\n".join(lines)


def _from_domain(from_email: str) -> str:
    return from_email.rsplit("@", 1)[-1] if "@" in from_email else "invalid.local"


def render_message(conn: sqlite3.Connection, *, target_id: int, identity: sqlite3.Row,
                    to_email: str, subject: str, body_text: str, business_name: str) -> RenderedMessage:
    token = message_id_token(conn, target_id)
    msgid = f"<lf-{token}@{_from_domain(identity['from_email'])}>"

    headers: list[tuple[str, str]] = []
    from_name = identity["from_name"] or ""
    from_hdr = f"{from_name} <{identity['from_email']}>" if from_name else identity["from_email"]
    headers.append(("From", from_hdr))
    if identity["reply_to"]:
        headers.append(("Reply-To", identity["reply_to"]))
    headers.append(("To", to_email))
    headers.append(("Subject", subject))
    headers.append(("Date", formatdate(localtime=False)))
    headers.append(("Message-ID", msgid))

    lu_parts = []
    if identity["unsubscribe_mailto"]:
        lu_parts.append(f"<mailto:{identity['unsubscribe_mailto']}>")
    if identity["unsubscribe_url"]:
        lu_parts.append(f"<{identity['unsubscribe_url']}>")
    if lu_parts:
        headers.append(("List-Unsubscribe", ", ".join(lu_parts)))
    if identity["unsubscribe_url"]:
        headers.append(("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"))

    body = body_text.rstrip("\n") + "\n" + build_footer(identity, business_name) + "\n"

    return RenderedMessage(
        message_id_header=msgid, from_name=from_name, from_email=identity["from_email"],
        reply_to=identity["reply_to"] or "", to_email=to_email, subject=subject, body_text=body,
        headers=headers,
    )
