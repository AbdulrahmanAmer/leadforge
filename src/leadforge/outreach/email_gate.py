"""Strict, non-SMTP email confirmation gate for outreach."""

from __future__ import annotations

import json
import sqlite3

_OSS_PROVIDERS = {"", "oss", "byol", "human_calling"}
_CONFIRMATION_SOURCES = {"delivery", "reply", "operator_confirmation", "email_confirmation"}


def confirmation_evidence(conn: sqlite3.Connection, business_id: str, email: str) -> dict:
    """Return the strongest non-SMTP confirmation for one address.

    A normal OSS observation is intentionally insufficient: MX proves a domain can receive mail,
    not that this mailbox exists. Licensed-provider verification or an actual delivery/reply/operator
    confirmation is required by strict mode.
    """
    value_json = json.dumps(email)
    rows = conn.execute(
        """SELECT provider, source, verification_state, observed_at
           FROM enrichment_observations
           WHERE business_id=? AND field IN ('email', 'email_confirmation', 'email_delivery')
             AND value_json=? AND verification_state='verified'
           ORDER BY id DESC""",
        (business_id, value_json),
    ).fetchall()
    for row in rows:
        provider = (row["provider"] or "").casefold()
        source = (row["source"] or "").casefold()
        if provider not in _OSS_PROVIDERS or source in _CONFIRMATION_SOURCES:
            return {"confirmed": True, "provider": row["provider"], "source": row["source"],
                    "observed_at": row["observed_at"]}
    return {"confirmed": False, "provider": "", "source": "", "observed_at": ""}
