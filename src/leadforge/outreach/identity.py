"""Sending identities + mailboxes (v0.3 unit E, docs/09 Wave 2 E #2).

An identity is the sender a recipient sees (from name/email, postal address, privacy URL, opt-out).
A mailbox is one physical inbox that sends on an identity's behalf, with its own transport config,
daily cap and warm-up clock. `config_json` on a mailbox stores environment-variable NAMES only —
`identity`/`mailbox add --config key=ENV_VAR_NAME` — never a literal secret, per docs/09 §E.
"""

from __future__ import annotations

import json
import sqlite3

from leadforge.util import LeadForgeError, now_iso

# keys a mailbox --config option may set; each value is the NAME of an env var holding the secret.
MAILBOX_CONFIG_KEYS = {
    "host_env", "port_env", "user_env", "password_env",
    "imap_host_env", "imap_user_env", "imap_password_env",
    "dkim_selector",  # not a secret — the DKIM selector label itself (e.g. "google", "default")
}


def add_identity(conn: sqlite3.Connection, *, label: str, from_email: str, from_name: str = "",
                  reply_to: str = "", postal_address: str = "", privacy_url: str = "",
                  unsubscribe_mailto: str = "", unsubscribe_url: str = "", client_id: str = "",
                  owner_entity: str = "gainlev") -> int:
    if not label or not label.strip():
        raise LeadForgeError("identity label is required")
    if not from_email or "@" not in from_email:
        raise LeadForgeError("identity add requires --from-email")
    existing = conn.execute("SELECT id FROM sending_identities WHERE label=?", (label,)).fetchone()
    if existing:
        raise LeadForgeError(f"identity '{label}' already exists (use a different --label)")
    cur = conn.execute(
        """INSERT INTO sending_identities(label,client_id,owner_entity,from_name,from_email,reply_to,
           postal_address,privacy_url,unsubscribe_mailto,unsubscribe_url,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (label, client_id, owner_entity, from_name, from_email, reply_to, postal_address, privacy_url,
         unsubscribe_mailto, unsubscribe_url, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_identity(conn: sqlite3.Connection, label: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sending_identities WHERE label=?", (label,)).fetchone()


def list_identities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sending_identities ORDER BY label").fetchall()


def is_identity_complete(row: sqlite3.Row) -> bool:
    """'live-complete' per docs/09 §E2: enough to actually send a compliant message."""
    return bool(
        row["from_name"] and row["from_email"] and row["postal_address"] and row["privacy_url"]
        and (row["unsubscribe_mailto"] or row["unsubscribe_url"])
    )


def add_mailbox(conn: sqlite3.Connection, *, identity_label: str, address: str, transport: str = "file",
                 config: dict[str, str] | None = None, daily_cap: int = 30,
                 warmup_started_at: str | None = None) -> int:
    identity = get_identity(conn, identity_label)
    if identity is None:
        raise LeadForgeError(f"unknown identity '{identity_label}' — add it first with `identity add`")
    if not address or "@" not in address:
        raise LeadForgeError("mailbox add requires a valid --address")
    existing = conn.execute("SELECT id FROM mailboxes WHERE address=?", (address,)).fetchone()
    if existing:
        raise LeadForgeError(f"mailbox '{address}' already exists")
    config = config or {}
    bad = sorted(set(config) - MAILBOX_CONFIG_KEYS)
    if bad:
        raise LeadForgeError(f"unknown mailbox --config key(s) {bad} (known: {sorted(MAILBOX_CONFIG_KEYS)})")
    cur = conn.execute(
        """INSERT INTO mailboxes(identity_id,address,transport,config_json,daily_cap,warmup_started_at,
           status,paused_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (identity["id"], address, transport, json.dumps(config), daily_cap,
         warmup_started_at or now_iso(), "active", "", now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_mailbox(conn: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM mailboxes WHERE address=?", (address,)).fetchone()


def list_mailboxes(conn: sqlite3.Connection, identity_label: str | None = None) -> list[sqlite3.Row]:
    if identity_label:
        return conn.execute(
            """SELECT m.* FROM mailboxes m JOIN sending_identities i ON i.id=m.identity_id
               WHERE i.label=? ORDER BY m.address""",
            (identity_label,),
        ).fetchall()
    return conn.execute("SELECT * FROM mailboxes ORDER BY address").fetchall()


def mailboxes_for_identity(conn: sqlite3.Connection, identity_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM mailboxes WHERE identity_id=? ORDER BY id", (identity_id,)).fetchall()


def env_config(mailbox_row: sqlite3.Row) -> dict[str, str]:
    raw = mailbox_row["config_json"]
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
