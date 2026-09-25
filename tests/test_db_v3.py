"""v0.4 "autopilot" unit B — schema v3: messages.author (agent vs template provenance, ADR-015).
Migration from an older schema was removed in cleanup step 5 (old workspaces are read-only
statistics, not upgraded in place); this file now only covers the DDL and the column default on a
fresh, current-schema database."""

from __future__ import annotations

from leadforge import db


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


