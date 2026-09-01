"""U9.1 — schema v2: migration from a v1 database, phone-based merge, outreach/outcome helpers."""

from __future__ import annotations

import sqlite3

from leadforge import db
from leadforge.models import Business, Contact, Person


def _v1_db(path) -> None:
    """A database exactly as v0.2 wrote it: no v2 columns, schema_version 1."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE suppression (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL UNIQUE,
          reason TEXT DEFAULT '', added_at TEXT);
        CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL, name TEXT NOT NULL,
          title TEXT DEFAULT '', source_url TEXT DEFAULT '', snippet TEXT DEFAULT '', dm_confidence REAL DEFAULT 0,
          is_dm INTEGER DEFAULT 0, labeled_by TEXT DEFAULT 'heuristic', labeled_at TEXT DEFAULT '',
          UNIQUE(business_id, name, title));
        INSERT INTO people(business_id,name,labeled_by) VALUES('biz_1','Jane Smith','registry');
        CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL, kind TEXT NOT NULL,
          value TEXT NOT NULL, label TEXT DEFAULT 'unknown', tier TEXT DEFAULT 'unknown', verified_at TEXT DEFAULT '',
          meta_json TEXT NOT NULL DEFAULT '{}', UNIQUE(business_id, kind, value));
        """
    )
    conn.commit()
    conn.close()


def test_v1_database_migrates_additively(tmp_path):
    path = tmp_path / "old.sqlite3"
    _v1_db(path)
    conn = db.connect(path)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    for table, col in (("suppression", "source"), ("suppression", "client_id"), ("people", "origin"), ("contacts", "affinity")):
        assert col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    # origin back-filled from labeled_by so old registry candidates keep their provenance
    assert conn.execute("SELECT origin FROM people WHERE name='Jane Smith'").fetchone()[0] == "registry"
    for table in ("sending_identities", "mailboxes", "outreach_targets", "messages", "events", "outcomes"):
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    # idempotent: a second connect is a no-op
    db.connect(path)


def _biz(**kw) -> Business:
    base = dict(id="biz_x", name="Abbey Service Centre", name_norm="abbey service centre", dedupe_key="na:x",
                last_seen_at="2026-09-02T00:00:00Z", source="gosom")
    base.update(kw)
    return Business(**base)


def test_registry_row_merges_into_maps_row_by_phone(conn):
    maps_id, created = db.upsert_business(conn, _biz(id="biz_maps", place_id="ChIJ1", dedupe_key="pid:ChIJ1",
                                                     phone_e164="+441213777474", website="https://abbey.co.uk",
                                                     domain="abbey.co.uk", address_postal="B1 1AA"))
    assert created
    reg = _biz(id="biz_dvsa", name="ABBEY SERVICE CENTRE LTD", name_norm="abbey service centre", dedupe_key="na:dvsa",
               phone_e164="+441213777474", source="dvsa", address_postal="B1 1AA",
               enrich={"dvsa": {"site_number": "V123", "classes": ["4"]}})
    merged_id, created2 = db.upsert_business(conn, reg)
    assert merged_id == maps_id and not created2
    row = conn.execute("SELECT enrich_json, source FROM businesses WHERE id=?", (maps_id,)).fetchone()
    assert "V123" in row["enrich_json"] and "dvsa" in row["enrich_json"]
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1


def test_shared_phone_with_unrelated_name_does_not_merge(conn):
    db.upsert_business(conn, _biz(id="biz_a", place_id="p1", dedupe_key="pid:p1", phone_e164="+441210000000",
                                  address_postal="B1 1AA"))
    db.upsert_business(conn, _biz(id="biz_b", place_id="p2", dedupe_key="pid:p2", phone_e164="+441210000000",
                                  name="Zed Tyres", name_norm="zed tyres", address_postal="B2 2BB"))
    other = _biz(id="biz_c", name="Quantum Widgets", name_norm="quantum widgets", dedupe_key="na:c",
                 phone_e164="+441210000000", address_postal="B9 9ZZ", source="dvsa")
    _, created = db.upsert_business(conn, other)
    assert created  # two candidates, neither resembles it -> a new row, never a guess


def test_contact_affinity_and_person_origin_roundtrip(conn):
    db.upsert_business(conn, _biz(id="biz_1", dedupe_key="na:1"))
    cid = db.add_contact(conn, Contact(business_id="biz_1", kind="email", value="joe@gmail.com", tier="valid",
                                       affinity="freemail_linked"))
    assert cid > 0
    assert conn.execute("SELECT affinity FROM contacts WHERE id=?", (cid,)).fetchone()[0] == "freemail_linked"
    # re-adding without affinity keeps the stored value
    db.add_contact(conn, Contact(business_id="biz_1", kind="email", value="joe@gmail.com", tier="valid"))
    assert conn.execute("SELECT affinity FROM contacts WHERE id=?", (cid,)).fetchone()[0] == "freemail_linked"
    db.add_person(conn, Person(business_id="biz_1", name="Jo Bloggs", labeled_by="gbp"))
    assert conn.execute("SELECT origin FROM people WHERE name='Jo Bloggs'").fetchone()[0] == "gbp"


def test_chain_map_and_outreach_state_and_outcomes(conn):
    for i in range(3):
        db.upsert_business(conn, _biz(id=f"biz_{i}", place_id=f"p{i}", dedupe_key=f"pid:p{i}", domain="halfords.com",
                                      name=f"Halfords {i}", name_norm=f"halfords {i}"))
    db.upsert_business(conn, _biz(id="biz_solo", place_id="ps", dedupe_key="pid:ps", domain="solo.co.uk"))
    cm = db.chain_map(conn)
    assert cm["biz_0"] == cm["biz_1"] == "domain:halfords.com" and "biz_solo" not in cm
    assert db.outreach_state_for(conn, "biz_0") is None
    conn.execute("INSERT INTO outreach_targets(business_id,campaign,state,updated_at) VALUES('biz_0','c','sent','2026-09-02')")
    assert db.outreach_state_for(conn, "biz_0") == "sent"
    db.add_outcome(conn, "biz_0", "phone", "interested", campaign="c", recorded_by="ops")
    assert db.outcome_counts(conn, "c") == {"interested": 1}
    db.suppress(conn, "email", "Joe@Gmail.com", source="bounce_hard", client_id="acme", business_id="biz_0")
    row = conn.execute("SELECT * FROM suppression").fetchone()
    assert row["value"] == "joe@gmail.com" and row["source"] == "bounce_hard" and row["client_id"] == "acme"
    assert db.is_suppressed(conn, "joe@gmail.com")
