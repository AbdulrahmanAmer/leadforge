"""SQLite layer (U1.2, v0.3 U9.1): schema, migrations, idempotent upserts with richest-field merge, dedupe (docs/03 ERD).

Explicit SQL, no ORM (ADR-004). One connection per CLI invocation; WAL mode; FK on.

Schema v2 (v0.3): outreach lifecycle tables (sending_identities, mailboxes, outreach_targets, messages,
events), an outcomes table for the phone/email feedback loop, `suppression.source/client_id/business_id`,
`people.origin` (where a candidate came from, kept when the agent labels it) and `contacts.affinity`
(own_domain | freemail_linked | freemail_unlinked). Registry rows (DVSA, Companies House) merge into the
Maps row that carries the same E.164 phone.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from leadforge.models import Business, Contact, Evidence, Person, Score
from leadforge.util import now_iso, sha1_hex

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, icp_path TEXT, icp_hash TEXT, stage TEXT NOT NULL DEFAULT 'planned',
  started_at TEXT, finished_at TEXT, stats_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
  query_text TEXT NOT NULL, tile_json TEXT, status TEXT NOT NULL DEFAULT 'pending', result_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS businesses (
  id TEXT PRIMARY KEY, place_id TEXT UNIQUE, cid TEXT, name TEXT NOT NULL, name_norm TEXT NOT NULL,
  category TEXT, categories_json TEXT NOT NULL DEFAULT '[]', website TEXT, domain TEXT,
  phone_e164 TEXT, phone_raw TEXT, address_full TEXT, address_street TEXT, address_city TEXT,
  address_region TEXT, address_postal TEXT, address_country TEXT, lat REAL, lng REAL,
  rating REAL, review_count INTEGER, hours_json TEXT, maps_url TEXT, source TEXT,
  first_run_id TEXT, last_seen_at TEXT, enrich_json TEXT NOT NULL DEFAULT '{}',
  dedupe_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_businesses_domain ON businesses(domain);
CREATE INDEX IF NOT EXISTS idx_businesses_phone ON businesses(phone_e164);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL REFERENCES businesses(id),
  kind TEXT NOT NULL, value TEXT NOT NULL, label TEXT DEFAULT 'unknown', tier TEXT DEFAULT 'unknown',
  verified_at TEXT DEFAULT '', meta_json TEXT NOT NULL DEFAULT '{}', affinity TEXT DEFAULT '',
  UNIQUE(business_id, kind, value)
);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL REFERENCES businesses(id),
  name TEXT NOT NULL, title TEXT DEFAULT '', source_url TEXT DEFAULT '', snippet TEXT DEFAULT '',
  dm_confidence REAL DEFAULT 0, is_dm INTEGER DEFAULT 0, labeled_by TEXT DEFAULT 'heuristic',
  labeled_at TEXT DEFAULT '', origin TEXT DEFAULT '',
  UNIQUE(business_id, name, title)
);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL REFERENCES businesses(id),
  ref_table TEXT DEFAULT 'businesses', ref_id INTEGER, fact TEXT NOT NULL, url TEXT DEFAULT '',
  snippet TEXT DEFAULT '', observed_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL REFERENCES businesses(id),
  run_id TEXT NOT NULL REFERENCES runs(id), total REAL NOT NULL, tier TEXT NOT NULL,
  factors_json TEXT NOT NULL, need_hooks_json TEXT NOT NULL DEFAULT '[]', scored_at TEXT,
  UNIQUE(business_id, run_id)
);

CREATE TABLE IF NOT EXISTS suppression (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL UNIQUE,
  reason TEXT DEFAULT '', added_at TEXT, source TEXT DEFAULT 'manual', client_id TEXT DEFAULT '',
  business_id TEXT
);

-- ---------------------------------------------------------------- v0.3 outreach lifecycle (ADR-011)
CREATE TABLE IF NOT EXISTS sending_identities (
  id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL UNIQUE, client_id TEXT DEFAULT '',
  owner_entity TEXT DEFAULT 'gainlev', from_name TEXT DEFAULT '', from_email TEXT DEFAULT '',
  reply_to TEXT DEFAULT '', postal_address TEXT DEFAULT '', privacy_url TEXT DEFAULT '',
  unsubscribe_mailto TEXT DEFAULT '', unsubscribe_url TEXT DEFAULT '', created_at TEXT
);

CREATE TABLE IF NOT EXISTS mailboxes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, identity_id INTEGER NOT NULL REFERENCES sending_identities(id),
  address TEXT NOT NULL UNIQUE, transport TEXT NOT NULL DEFAULT 'file', config_json TEXT NOT NULL DEFAULT '{}',
  daily_cap INTEGER DEFAULT 30, warmup_started_at TEXT, status TEXT DEFAULT 'active', paused_reason TEXT DEFAULT '',
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS outreach_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL REFERENCES businesses(id),
  contact_id INTEGER, campaign TEXT NOT NULL, client_id TEXT DEFAULT '', identity_id INTEGER,
  state TEXT NOT NULL DEFAULT 'enrolled', eligibility_json TEXT NOT NULL DEFAULT '{}',
  touches INTEGER DEFAULT 0, next_touch_at TEXT, created_at TEXT, updated_at TEXT,
  UNIQUE(business_id, campaign)
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, target_id INTEGER NOT NULL REFERENCES outreach_targets(id),
  step INTEGER DEFAULT 1, purpose TEXT DEFAULT '', subject TEXT DEFAULT '', body_text TEXT DEFAULT '',
  draft_hash TEXT DEFAULT '', state TEXT NOT NULL DEFAULT 'drafted', gate_json TEXT NOT NULL DEFAULT '{}',
  grade TEXT DEFAULT '', used_fact TEXT DEFAULT '', approved_by TEXT DEFAULT '', approved_at TEXT,
  approved_hash TEXT DEFAULT '', queued_at TEXT, sent_at TEXT, mailbox_id INTEGER,
  message_id_header TEXT DEFAULT '', provider_message_id TEXT DEFAULT '', error TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_state ON messages(state);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER, business_id TEXT, kind TEXT NOT NULL,
  classification TEXT DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}', dedupe_key TEXT UNIQUE,
  occurred_at TEXT, ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL REFERENCES businesses(id),
  campaign TEXT DEFAULT '', channel TEXT NOT NULL, result TEXT NOT NULL, notes TEXT DEFAULT '',
  recorded_by TEXT DEFAULT '', contacted_at TEXT, recorded_at TEXT
);
"""

# columns added by v2 on tables that existed in v1 (CREATE IF NOT EXISTS cannot add them)
_V2_COLUMNS = {
    "suppression": [("source", "TEXT DEFAULT 'manual'"), ("client_id", "TEXT DEFAULT ''"), ("business_id", "TEXT")],
    "people": [("origin", "TEXT DEFAULT ''")],
    "contacts": [("affinity", "TEXT DEFAULT ''")],
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    elif int(row[0]) < 2:
        # v1 -> v2: additive columns only (the new tables came from executescript above). A column that
        # already exists (partial earlier migration) is skipped, so this is safe to re-run.
        for table, cols in _V2_COLUMNS.items():
            have = _columns(conn, table)
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.execute("UPDATE people SET origin=labeled_by WHERE origin='' OR origin IS NULL")
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
    conn.commit()


# ------------------------------------------------------------------ runs & queries
def create_run(conn: sqlite3.Connection, icp_path: str, icp_hash: str) -> str:
    run_id = f"run_{now_iso().replace('-', '').replace(':', '').replace('T', '_')[:14]}_{sha1_hex(icp_hash + now_iso(), 4)}"
    conn.execute(
        "INSERT INTO runs(id,icp_path,icp_hash,stage,started_at) VALUES(?,?,?,?,?)",
        (run_id, icp_path, icp_hash, "planned", now_iso()),
    )
    conn.commit()
    return run_id


def latest_run(conn: sqlite3.Connection, icp_hash: str | None = None) -> sqlite3.Row | None:
    if icp_hash:
        cur = conn.execute("SELECT * FROM runs WHERE icp_hash=? ORDER BY started_at DESC LIMIT 1", (icp_hash,))
    else:
        cur = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    return cur.fetchone()


def set_stage(conn: sqlite3.Connection, run_id: str, stage: str, **stats) -> None:
    row = conn.execute("SELECT stats_json FROM runs WHERE id=?", (run_id,)).fetchone()
    merged = json.loads(row["stats_json"]) if row else {}
    merged.update(stats)
    done = stage in ("exported", "failed")
    conn.execute(
        "UPDATE runs SET stage=?, stats_json=?, finished_at=CASE WHEN ? THEN ? ELSE finished_at END WHERE id=?",
        (stage, json.dumps(merged), int(done), now_iso(), run_id),
    )
    conn.commit()


def add_queries(conn: sqlite3.Connection, run_id: str, queries: list[tuple[str, dict | None]]) -> None:
    conn.executemany(
        "INSERT INTO queries(run_id,query_text,tile_json) VALUES(?,?,?)",
        [(run_id, q, json.dumps(t) if t else None) for q, t in queries],
    )
    conn.commit()


def pending_queries(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM queries WHERE run_id=? AND status='pending' ORDER BY id", (run_id,)).fetchall()


def finish_query(conn: sqlite3.Connection, query_id: int, status: str, count: int) -> None:
    conn.execute("UPDATE queries SET status=?, result_count=? WHERE id=?", (status, count, query_id))
    conn.commit()


# ------------------------------------------------------------------ businesses (merge upsert)
_RICH_FIELDS = [
    "place_id", "cid", "category", "website", "domain", "phone_e164", "phone_raw", "address_full",
    "address_street", "address_city", "address_region", "address_postal", "address_country",
    "lat", "lng", "rating", "review_count", "maps_url",
]


def _phone_match(conn: sqlite3.Connection, biz: Business) -> sqlite3.Row | None:
    """A registry row (no place_id) merges into the row that already carries its phone number, when the
    two look like the same business: shared name token or same postcode district. A shared switchboard
    across genuinely different businesses (a chain, a serviced office) is left alone."""
    if not biz.phone_e164:
        return None
    rows = conn.execute("SELECT * FROM businesses WHERE phone_e164=?", (biz.phone_e164,)).fetchall()
    if not rows:
        return None
    tokens = {t for t in biz.name_norm.split() if len(t) >= 3}
    postal = (biz.address_postal or "").split(" ")[0].casefold()
    for r in rows:
        r_tokens = {t for t in (r["name_norm"] or "").split() if len(t) >= 3}
        r_postal = (r["address_postal"] or "").split(" ")[0].casefold()
        if (tokens & r_tokens) or (postal and postal == r_postal):
            return r
    return rows[0] if len(rows) == 1 else None


def upsert_business(conn: sqlite3.Connection, biz: Business) -> tuple[str, bool]:
    """Insert or merge by dedupe_key (place_id preferred), then by place_id, then (v0.3) by phone for
    registry rows that carry no place_id. Merge keeps the richest value per column.

    Returns (business_id, created).
    """
    existing = conn.execute("SELECT * FROM businesses WHERE dedupe_key=?", (biz.dedupe_key,)).fetchone()
    if existing is None and biz.place_id:
        existing = conn.execute("SELECT * FROM businesses WHERE place_id=?", (biz.place_id,)).fetchone()
    if existing is None and not biz.place_id:
        existing = _phone_match(conn, biz)

    if existing is None:
        conn.execute(
            """INSERT INTO businesses(id,place_id,cid,name,name_norm,category,categories_json,website,domain,
               phone_e164,phone_raw,address_full,address_street,address_city,address_region,address_postal,
               address_country,lat,lng,rating,review_count,hours_json,maps_url,source,first_run_id,last_seen_at,
               enrich_json,dedupe_key)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                biz.id, biz.place_id, biz.cid, biz.name, biz.name_norm, biz.category,
                json.dumps(biz.categories), biz.website, biz.domain, biz.phone_e164, biz.phone_raw,
                biz.address_full, biz.address_street, biz.address_city, biz.address_region,
                biz.address_postal, biz.address_country, biz.lat, biz.lng, biz.rating, biz.review_count,
                json.dumps(biz.hours) if biz.hours else None, biz.maps_url, biz.source,
                biz.first_run_id, biz.last_seen_at, json.dumps(biz.enrich), biz.dedupe_key,
            ),
        )
        conn.commit()
        return biz.id, True

    # merge: fill NULL/empty columns from the new record; ratings/counts take the freshest non-null
    updates, params = [], []
    new = biz.model_dump()
    for col in _RICH_FIELDS:
        old_val = existing[col]
        new_val = new.get(col)
        if (old_val in (None, "")) and new_val not in (None, ""):
            updates.append(f"{col}=?")
            params.append(new_val)
        elif col in ("rating", "review_count") and new_val is not None:
            updates.append(f"{col}=?")
            params.append(new_val)
    cats = set(json.loads(existing["categories_json"])) | set(biz.categories)
    updates += ["categories_json=?", "last_seen_at=?"]
    params += [json.dumps(sorted(cats)), now_iso()]
    if biz.enrich:  # provider-side facts (gbp fields, registry ids) merge into the stored enrich dict
        merged = json.loads(existing["enrich_json"] or "{}")
        for k, v in biz.enrich.items():
            merged.setdefault(k, v)
        sources = set(str(merged.get("sources") or "").split(",")) - {""}
        sources |= {existing["source"] or "", biz.source} - {""}
        merged["sources"] = ",".join(sorted(sources))
        updates.append("enrich_json=?")
        params.append(json.dumps(merged))
    params.append(existing["id"])
    conn.execute(f"UPDATE businesses SET {', '.join(updates)} WHERE id=?", params)
    conn.commit()
    return existing["id"], False


def businesses_for_enrich(conn: sqlite3.Connection, limit: int,
                          retry_needs_browser: bool = False) -> list[sqlite3.Row]:
    """Uncrawled sites; with retry_needs_browser (browser extra now available), also the sites an
    earlier pass marked needs_browser — otherwise 'pip install .[browser] and re-run' is a no-op."""
    browser_clause = " OR json_extract(enrich_json,'$.needs_browser') = 1" if retry_needs_browser else ""
    return conn.execute(
        f"""SELECT * FROM businesses WHERE domain IS NOT NULL
           AND (json_extract(enrich_json,'$.crawled_at') IS NULL{browser_clause})
           AND json_extract(enrich_json,'$.attempted_at') IS NULL
           AND domain NOT IN (SELECT value FROM suppression WHERE kind='domain')
           ORDER BY (category IS NULL), review_count DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def all_businesses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM businesses ORDER BY name").fetchall()


def update_enrich(conn: sqlite3.Connection, business_id: str, enrich: dict) -> None:
    row = conn.execute("SELECT enrich_json FROM businesses WHERE id=?", (business_id,)).fetchone()
    merged = json.loads(row["enrich_json"]) if row else {}
    merged.update(enrich)
    conn.execute("UPDATE businesses SET enrich_json=? WHERE id=?", (json.dumps(merged), business_id))
    conn.commit()


def chain_map(conn: sqlite3.Connection) -> dict[str, str]:
    """business_id -> chain key for rows that share a (non-freemail) domain or a phone with >= 2 rows.
    Used for the sheet's Chain column, the chain penalty and one-contact-per-chain outreach."""
    out: dict[str, str] = {}
    for key_col in ("domain", "phone_e164"):
        rows = conn.execute(
            f"""SELECT {key_col} k, id FROM businesses WHERE {key_col} IS NOT NULL AND {key_col} != ''
                AND {key_col} IN (SELECT {key_col} FROM businesses GROUP BY {key_col} HAVING COUNT(*) >= 2)"""
        ).fetchall()
        for r in rows:
            out.setdefault(r["id"], f"{key_col}:{r['k']}")
    return out


def merge_business_into(conn: sqlite3.Connection, keep_id: str, drop_id: str) -> None:
    """Fold a duplicate row into another: contacts/people/evidence/scores re-pointed, the duplicate deleted."""
    if keep_id == drop_id:
        return
    for table in ("contacts", "people", "evidence", "outcomes", "outreach_targets"):
        conn.execute(f"UPDATE OR IGNORE {table} SET business_id=? WHERE business_id=?", (keep_id, drop_id))
        conn.execute(f"DELETE FROM {table} WHERE business_id=?", (drop_id,))
    conn.execute("UPDATE OR IGNORE scores SET business_id=? WHERE business_id=?", (keep_id, drop_id))
    conn.execute("DELETE FROM scores WHERE business_id=?", (drop_id,))
    conn.execute("DELETE FROM businesses WHERE id=?", (drop_id,))
    conn.commit()


# ------------------------------------------------------------------ contacts / people / evidence / scores
def add_contact(conn: sqlite3.Connection, c: Contact) -> int:
    """Insert or refresh; returns the contact row id (so evidence can reference it)."""
    conn.execute(
        """INSERT INTO contacts(business_id,kind,value,label,tier,verified_at,meta_json,affinity)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(business_id,kind,value) DO UPDATE SET
             label=excluded.label, tier=excluded.tier, verified_at=excluded.verified_at, meta_json=excluded.meta_json,
             affinity=CASE WHEN excluded.affinity != '' THEN excluded.affinity ELSE contacts.affinity END""",
        (c.business_id, c.kind, c.value, c.label, c.tier, c.verified_at, json.dumps(c.meta), c.affinity),
    )
    row = conn.execute("SELECT id FROM contacts WHERE business_id=? AND kind=? AND value=?",
                       (c.business_id, c.kind, c.value)).fetchone()
    return int(row["id"]) if row else 0


def contacts_for(conn: sqlite3.Connection, business_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM contacts WHERE business_id=?", (business_id,)).fetchall()


def add_person(conn: sqlite3.Connection, p: Person) -> None:
    conn.execute(
        """INSERT INTO people(business_id,name,title,source_url,snippet,dm_confidence,is_dm,labeled_by,labeled_at,origin)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(business_id,name,title) DO NOTHING""",
        (p.business_id, p.name, p.title, p.source_url, p.snippet, p.dm_confidence, p.is_dm, p.labeled_by, p.labeled_at,
         p.origin or p.labeled_by),
    )


def people_for(conn: sqlite3.Connection, business_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM people WHERE business_id=? ORDER BY is_dm DESC, dm_confidence DESC", (business_id,)).fetchall()


def dm_pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT DISTINCT b.* FROM businesses b JOIN people p ON p.business_id=b.id
           WHERE NOT EXISTS (SELECT 1 FROM people x WHERE x.business_id=b.id AND x.is_dm!=0)
           ORDER BY b.review_count DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def add_evidence(conn: sqlite3.Connection, e: Evidence) -> None:
    conn.execute(
        "INSERT INTO evidence(business_id,ref_table,ref_id,fact,url,snippet,observed_at) VALUES(?,?,?,?,?,?,?)",
        (e.business_id, e.ref_table, e.ref_id, e.fact, e.url, e.snippet[:300], e.observed_at or now_iso()),
    )


def evidence_for(conn: sqlite3.Connection, business_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM evidence WHERE business_id=? ORDER BY id", (business_id,)).fetchall()


def save_score(conn: sqlite3.Connection, s: Score) -> None:
    conn.execute(
        """INSERT INTO scores(business_id,run_id,total,tier,factors_json,need_hooks_json,scored_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(business_id,run_id) DO UPDATE SET total=excluded.total, tier=excluded.tier,
             factors_json=excluded.factors_json, need_hooks_json=excluded.need_hooks_json, scored_at=excluded.scored_at""",
        (
            s.business_id, s.run_id, s.total, s.tier,
            json.dumps([f.model_dump() for f in s.factors]), json.dumps(s.need_hooks), s.scored_at or now_iso(),
        ),
    )


def scores_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.*, b.* FROM scores s JOIN businesses b ON b.id=s.business_id
           WHERE s.run_id=?
             AND (b.domain IS NULL OR b.domain NOT IN (SELECT value FROM suppression))
             AND (b.place_id IS NULL OR b.place_id NOT IN (SELECT value FROM suppression))
           ORDER BY s.total DESC""",
        (run_id,),
    ).fetchall()


# ------------------------------------------------------------------ suppression
def suppress(conn: sqlite3.Connection, kind: str, value: str, reason: str = "", source: str = "manual",
             client_id: str = "", business_id: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO suppression(kind,value,reason,added_at,source,client_id,business_id) VALUES(?,?,?,?,?,?,?)",
        (kind, value.strip().lower(), reason, now_iso(), source, client_id, business_id),
    )
    conn.commit()


def is_suppressed(conn: sqlite3.Connection, *values: str | None) -> bool:
    vals = [v.strip().lower() for v in values if v]
    if not vals:
        return False
    q = ",".join("?" * len(vals))
    return conn.execute(f"SELECT 1 FROM suppression WHERE value IN ({q}) LIMIT 1", vals).fetchone() is not None


# ------------------------------------------------------------------ v0.3: outreach state + outcomes
def outreach_state_for(conn: sqlite3.Connection, business_id: str, campaign: str | None = None) -> str | None:
    """The lead's current outreach state (enrolled/drafted/approved/sent/replied/...), or None when it
    was never enrolled. The sheet's Next Action column shows it in place of the phone-first default."""
    if campaign:
        row = conn.execute("SELECT state FROM outreach_targets WHERE business_id=? AND campaign=?",
                           (business_id, campaign)).fetchone()
    else:
        row = conn.execute("SELECT state FROM outreach_targets WHERE business_id=? ORDER BY updated_at DESC LIMIT 1",
                           (business_id,)).fetchone()
    return row["state"] if row else None


def add_outcome(conn: sqlite3.Connection, business_id: str, channel: str, result: str, campaign: str = "",
                notes: str = "", recorded_by: str = "", contacted_at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO outcomes(business_id,campaign,channel,result,notes,recorded_by,contacted_at,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
        (business_id, campaign, channel, result, notes, recorded_by, contacted_at or now_iso(), now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def outcomes_for(conn: sqlite3.Connection, business_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM outcomes WHERE business_id=? ORDER BY recorded_at", (business_id,)).fetchall()


def outcome_counts(conn: sqlite3.Connection, campaign: str | None = None) -> dict[str, int]:
    if campaign:
        rows = conn.execute("SELECT result, COUNT(*) c FROM outcomes WHERE campaign=? GROUP BY result", (campaign,)).fetchall()
    else:
        rows = conn.execute("SELECT result, COUNT(*) c FROM outcomes GROUP BY result").fetchall()
    return {r["result"]: r["c"] for r in rows}
