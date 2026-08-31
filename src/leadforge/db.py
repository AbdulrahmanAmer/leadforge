"""SQLite layer (U1.2): schema, migrations, idempotent upserts with richest-field merge, dedupe (docs/03 ERD).

Explicit SQL, no ORM (ADR-004). One connection per CLI invocation; WAL mode; FK on.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from leadforge.models import Business, Contact, Evidence, Person, Score
from leadforge.util import now_iso, sha1_hex

SCHEMA_VERSION = 1

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

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL REFERENCES businesses(id),
  kind TEXT NOT NULL, value TEXT NOT NULL, label TEXT DEFAULT 'unknown', tier TEXT DEFAULT 'unknown',
  verified_at TEXT DEFAULT '', meta_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(business_id, kind, value)
);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL REFERENCES businesses(id),
  name TEXT NOT NULL, title TEXT DEFAULT '', source_url TEXT DEFAULT '', snippet TEXT DEFAULT '',
  dm_confidence REAL DEFAULT 0, is_dm INTEGER DEFAULT 0, labeled_by TEXT DEFAULT 'heuristic',
  labeled_at TEXT DEFAULT '',
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
  reason TEXT DEFAULT '', added_at TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    # future migrations: elif int(row[0]) < N: ALTER ... then update meta
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


def upsert_business(conn: sqlite3.Connection, biz: Business) -> tuple[str, bool]:
    """Insert or merge by dedupe_key (place_id preferred). Merge keeps the richest value per column.

    Returns (business_id, created).
    """
    existing = conn.execute("SELECT * FROM businesses WHERE dedupe_key=?", (biz.dedupe_key,)).fetchone()
    if existing is None and biz.place_id:
        existing = conn.execute("SELECT * FROM businesses WHERE place_id=?", (biz.place_id,)).fetchone()

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
    params.append(existing["id"])
    conn.execute(f"UPDATE businesses SET {', '.join(updates)} WHERE id=?", params)
    conn.commit()
    return existing["id"], False


def businesses_for_enrich(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM businesses WHERE domain IS NOT NULL
           AND json_extract(enrich_json,'$.crawled_at') IS NULL
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


# ------------------------------------------------------------------ contacts / people / evidence / scores
def add_contact(conn: sqlite3.Connection, c: Contact) -> None:
    conn.execute(
        """INSERT INTO contacts(business_id,kind,value,label,tier,verified_at,meta_json)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(business_id,kind,value) DO UPDATE SET
             label=excluded.label, tier=excluded.tier, verified_at=excluded.verified_at, meta_json=excluded.meta_json""",
        (c.business_id, c.kind, c.value, c.label, c.tier, c.verified_at, json.dumps(c.meta)),
    )


def contacts_for(conn: sqlite3.Connection, business_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM contacts WHERE business_id=?", (business_id,)).fetchall()


def add_person(conn: sqlite3.Connection, p: Person) -> None:
    conn.execute(
        """INSERT INTO people(business_id,name,title,source_url,snippet,dm_confidence,is_dm,labeled_by,labeled_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(business_id,name,title) DO NOTHING""",
        (p.business_id, p.name, p.title, p.source_url, p.snippet, p.dm_confidence, p.is_dm, p.labeled_by, p.labeled_at),
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
def suppress(conn: sqlite3.Connection, kind: str, value: str, reason: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO suppression(kind,value,reason,added_at) VALUES(?,?,?,?)",
        (kind, value.strip().lower(), reason, now_iso()),
    )
    conn.commit()


def is_suppressed(conn: sqlite3.Connection, *values: str | None) -> bool:
    vals = [v.strip().lower() for v in values if v]
    if not vals:
        return False
    q = ",".join("?" * len(vals))
    return conn.execute(f"SELECT 1 FROM suppression WHERE value IN ({q}) LIMIT 1", vals).fetchone() is not None
