"""Shared scaffolding for tests/test_outreach_*.py (v0.3 unit E). NOT a test module itself — no
`test_*` functions live here, only builders the outreach test files import.
"""

from __future__ import annotations

import sqlite3

from leadforge import db
from leadforge.models import Business, Contact, Person, Score, ScoreFactor
from leadforge.outreach.identity import add_identity, add_mailbox


def make_run(conn: sqlite3.Connection, run_id: str = "run_test_0001") -> str:
    conn.execute("INSERT INTO runs(id,icp_path,icp_hash,stage,started_at) VALUES(?,?,?,?,?)",
                 (run_id, "icp.yaml", "abc123", "scored", "2026-09-02T00:00:00Z"))
    conn.commit()
    return run_id


def make_scored_business(conn: sqlite3.Connection, run_id: str, *, id_: str, name: str = "Abbey Auto Repair",
                          domain: str = "abbeyauto.co.uk", email: str | None = "info@abbeyauto.co.uk",
                          tier: str = "A", email_tier: str = "valid", affinity: str = "own_domain",
                          phone_e164: str | None = "unique", crawled: bool = True, dm: bool = True,
                          place_id: str | None = "unique", enrich_extra: dict | None = None) -> str:
    """`phone_e164`/`place_id` default to a value DERIVED FROM `id_` (so two calls never collide and
    accidentally trip db.upsert_business's phone-merge path) — pass an explicit shared value (or None)
    when a test deliberately wants two rows to phone-merge or chain-dedupe."""
    if not conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
        make_run(conn, run_id)
    if phone_e164 == "unique":
        phone_e164 = "+4412130" + str(abs(hash(id_)) % 100000).zfill(5)
    if place_id == "unique":
        place_id = f"place_{id_}"
    enrich = {"crawled_at": "2026-09-02T00:00:00Z", "pages": 3} if crawled else {}
    if enrich_extra:
        enrich.update(enrich_extra)
    biz = Business(id=id_, place_id=place_id, name=name, name_norm=name.lower(), dedupe_key=f"pid:{place_id}" if place_id else f"na:{id_}",
                   website=f"https://{domain}" if domain else None, domain=domain, phone_e164=phone_e164,
                   source="gosom", last_seen_at="2026-09-02T00:00:00Z", enrich=enrich)
    bid, _ = db.upsert_business(conn, biz)
    if email:
        db.add_contact(conn, Contact(business_id=bid, kind="email", value=email, tier=email_tier, affinity=affinity))
    if dm:
        db.add_person(conn, Person(business_id=bid, name="Jane Doe", title="Owner", is_dm=1, labeled_by="agent",
                                   origin="heuristic"))
    db.save_score(conn, Score(
        business_id=bid, run_id=run_id, total=90.0 if tier == "A" else 65.0 if tier == "B" else 40.0, tier=tier,
        factors=[ScoreFactor(factor="industry", group="fit", weight=25, score=1.0, points=25.0, why="matched category")],
    ))
    conn.commit()
    return bid


def make_identity(conn: sqlite3.Connection, label: str = "acme-sales", **kw) -> int:
    defaults = dict(
        label=label, from_email="sales@acme-agency.com", from_name="Acme Sales Team",
        postal_address="1 Main Street, Leeds, LS1 1AA, UK", privacy_url="https://acme-agency.com/privacy",
        unsubscribe_mailto="unsubscribe@acme-agency.com",
    )
    defaults.update(kw)
    return add_identity(conn, **defaults)


def make_mailbox(conn: sqlite3.Connection, identity_label: str = "acme-sales", address: str = "sales@acme-agency.com",
                 **kw) -> int:
    return add_mailbox(conn, identity_label=identity_label, address=address, **kw)


def make_target(conn: sqlite3.Connection, *, business_id: str, campaign: str = "test-campaign",
                identity_id: int, contact_id: int | None = None, state: str = "enrolled",
                client_id: str = "", eligibility: dict | None = None) -> int:
    import json

    from leadforge.util import now_iso

    cur = conn.execute(
        """INSERT INTO outreach_targets(business_id,contact_id,campaign,client_id,identity_id,state,
           eligibility_json,touches,created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,?,?)""",
        (business_id, contact_id, campaign, client_id, identity_id, state,
         json.dumps(eligibility or {"_region_profile": "uk"}), now_iso(), now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def make_message(conn: sqlite3.Connection, *, target_id: int, subject: str = "Quick question",
                 body_text: str = "Hi there, thought you might find this useful.", state: str = "drafted",
                 draft_hash: str = "hash1", approved_hash: str = "", approved_by: str = "") -> int:
    from leadforge.util import now_iso

    cur = conn.execute(
        """INSERT INTO messages(target_id,step,purpose,subject,body_text,draft_hash,state,approved_by,
           approved_hash,created_at,updated_at) VALUES(?,1,'gainlev_leadgen',?,?,?,?,?,?,?,?)""",
        (target_id, subject, body_text, draft_hash, state, approved_by, approved_hash, now_iso(), now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)
