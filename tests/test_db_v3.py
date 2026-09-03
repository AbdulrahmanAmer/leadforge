"""v0.4 "autopilot" unit B — schema v3: messages.author (agent vs template provenance, ADR-015).
Migration is additive and re-runnable, same pattern as v1->v2 (see tests/test_db_v2.py, whose v1
fixture this file's `_v1_db` copies): a v1 database must run the v2 steps AND the v3 step in one
`migrate()` call, landing straight on schema_version 3."""

from __future__ import annotations

import sqlite3

from leadforge import db


def _v1_db(path) -> None:
    """A database exactly as v0.2 wrote it: no v2 or v3 columns, schema_version 1. Copied from
    tests/test_db_v2.py's fixture."""
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
        CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL, kind TEXT NOT NULL,
          value TEXT NOT NULL, label TEXT DEFAULT 'unknown', tier TEXT DEFAULT 'unknown', verified_at TEXT DEFAULT '',
          meta_json TEXT NOT NULL DEFAULT '{}', UNIQUE(business_id, kind, value));
        """
    )
    conn.commit()
    conn.close()


def _v2_db(path) -> None:
    """A database exactly as v0.3 wrote it: full v2 shape (all outreach-lifecycle tables, v2
    columns), schema_version 2, but `messages` has no `author` column yet -- and already carries a
    drafted row, so the migration's backfill-via-column-default is exercised, not just the DDL."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '2');
        CREATE TABLE suppression (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL UNIQUE,
          reason TEXT DEFAULT '', added_at TEXT, source TEXT DEFAULT 'manual', client_id TEXT DEFAULT '', business_id TEXT);
        CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL, name TEXT NOT NULL,
          title TEXT DEFAULT '', source_url TEXT DEFAULT '', snippet TEXT DEFAULT '', dm_confidence REAL DEFAULT 0,
          is_dm INTEGER DEFAULT 0, labeled_by TEXT DEFAULT 'heuristic', labeled_at TEXT DEFAULT '', origin TEXT DEFAULT '',
          UNIQUE(business_id, name, title));
        CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL, kind TEXT NOT NULL,
          value TEXT NOT NULL, label TEXT DEFAULT 'unknown', tier TEXT DEFAULT 'unknown', verified_at TEXT DEFAULT '',
          meta_json TEXT NOT NULL DEFAULT '{}', affinity TEXT DEFAULT '', UNIQUE(business_id, kind, value));
        CREATE TABLE outreach_targets (
          id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL, contact_id INTEGER, campaign TEXT NOT NULL,
          client_id TEXT DEFAULT '', identity_id INTEGER, state TEXT NOT NULL DEFAULT 'enrolled',
          eligibility_json TEXT NOT NULL DEFAULT '{}', touches INTEGER DEFAULT 0, next_touch_at TEXT,
          created_at TEXT, updated_at TEXT, UNIQUE(business_id, campaign));
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, target_id INTEGER NOT NULL, step INTEGER DEFAULT 1,
          purpose TEXT DEFAULT '', subject TEXT DEFAULT '', body_text TEXT DEFAULT '', draft_hash TEXT DEFAULT '',
          state TEXT NOT NULL DEFAULT 'drafted', gate_json TEXT NOT NULL DEFAULT '{}', grade TEXT DEFAULT '',
          used_fact TEXT DEFAULT '', approved_by TEXT DEFAULT '', approved_at TEXT, approved_hash TEXT DEFAULT '',
          queued_at TEXT, sent_at TEXT, mailbox_id INTEGER, message_id_header TEXT DEFAULT '',
          provider_message_id TEXT DEFAULT '', error TEXT DEFAULT '', created_at TEXT, updated_at TEXT
        );
        INSERT INTO messages(target_id, step, purpose, subject, body_text, state, created_at, updated_at)
          VALUES (1, 1, 'gainlev_leadgen', 'Quick note', 'body text', 'drafted',
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()


def test_v2_database_migrates_to_v3_additively(tmp_path):
    path = tmp_path / "v2.sqlite3"
    _v2_db(path)
    conn = db.connect(path)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    assert "author" in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    # the pre-existing row backfills to the column default, never NULL
    row = conn.execute("SELECT author FROM messages WHERE target_id=1").fetchone()
    assert row["author"] == "agent"
    conn.close()
    # idempotent: a second connect on an already-v3 database is a no-op, never errors
    conn2 = db.connect(path)
    assert conn2.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)


def test_v1_database_migrates_straight_to_v3(tmp_path):
    path = tmp_path / "v1.sqlite3"
    _v1_db(path)
    conn = db.connect(path)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    for table, col in (("suppression", "source"), ("suppression", "client_id"), ("people", "origin"),
                       ("contacts", "affinity"), ("messages", "author")):
        assert col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}, f"{table}.{col}"
    for table in ("sending_identities", "mailboxes", "outreach_targets", "messages", "events", "outcomes"):
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()


def test_fresh_database_has_author_column_from_the_ddl(tmp_path):
    """A brand-new DB never runs the migration branch at all (the meta row is inserted straight at
    SCHEMA_VERSION) -- so `messages.author` must come from the DDL itself, not only the migration."""
    path = tmp_path / "fresh.sqlite3"
    conn = db.connect(path)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    assert "author" in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}


def test_new_message_row_defaults_author_to_agent(conn):
    """`conn` (conftest.py) is a fresh v3 database — insert a message without naming `author` at
    all, the way most of the existing v0.3 code (draft/cli.py before this unit, outreach code) does,
    and confirm the column default fires rather than storing NULL."""
    from leadforge import db
    from leadforge.models import Business

    db.upsert_business(conn, Business(id="biz_1", name="Acme", name_norm="acme", dedupe_key="dk-biz_1"))
    conn.execute(
        "INSERT INTO outreach_targets(business_id,campaign,state,created_at,updated_at) "
        "VALUES('biz_1','camp','enrolled','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    tid = conn.execute("SELECT id FROM outreach_targets WHERE business_id='biz_1'").fetchone()["id"]
    conn.execute(
        "INSERT INTO messages(target_id,step,purpose,subject,body_text,state,created_at,updated_at) "
        "VALUES(?,1,'gainlev_leadgen','x','x','drafted','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
        (tid,),
    )
    conn.commit()
    row = conn.execute("SELECT author FROM messages WHERE target_id=?", (tid,)).fetchone()
    assert row["author"] == "agent"


def test_v1_database_lands_on_schema_v4_with_queries_new_count(tmp_path):
    path = tmp_path / "old.sqlite3"
    _v1_db(path)
    conn = db.connect(path)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    assert "new_count" in {r[1] for r in conn.execute("PRAGMA table_info(queries)")}
    db.connect(path)  # idempotent
