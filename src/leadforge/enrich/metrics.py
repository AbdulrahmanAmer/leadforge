"""Read-only enrichment baseline and attribution metrics."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from leadforge import db
from leadforge.models import EnrichmentObservation
from leadforge.util import now_iso


def coverage_metrics(conn: sqlite3.Connection) -> dict[str, int]:
    """Return current business/contact/person/observation coverage counts."""
    q = lambda sql: int(conn.execute(sql).fetchone()[0])  # noqa: E731
    metrics = {
        "businesses": q("SELECT COUNT(*) FROM businesses"),
        "businesses_with_website": q("SELECT COUNT(*) FROM businesses WHERE website IS NOT NULL AND website != ''"),
        "businesses_with_people": q("SELECT COUNT(DISTINCT business_id) FROM people"),
        "businesses_with_contacts": q("SELECT COUNT(DISTINCT business_id) FROM contacts"),
        "verified_emails": q("SELECT COUNT(*) FROM contacts WHERE kind='email' AND tier='valid'"),
        "verified_phones": q("SELECT COUNT(*) FROM contacts WHERE kind='phone' AND tier='valid'"),
        "observations": q("SELECT COUNT(*) FROM enrichment_observations"),
    }
    # DM-route coverage is intentionally person-owned. A business-level phone does not count here,
    # because it cannot prove that the caller will reach the selected decision maker.
    metrics.update({
        "businesses_with_accepted_dm": q(
            "SELECT COUNT(DISTINCT business_id) FROM people WHERE is_dm=1"
        ),
        "accepted_dms_with_person_phone": q(
            """SELECT COUNT(DISTINCT p.business_id) FROM people p
               JOIN enrichment_observations o ON o.business_id=p.business_id AND o.person_id=p.id
               WHERE p.is_dm=1 AND o.field='phone' AND o.verification_state NOT IN ('rejected','stale')"""
        ),
        "accepted_dms_with_direct_or_mobile": q(
            """SELECT COUNT(DISTINCT p.business_id) FROM people p
               JOIN enrichment_observations o ON o.business_id=p.business_id AND o.person_id=p.id
               JOIN contacts c ON c.business_id=p.business_id AND c.kind='phone'
                 AND c.value=json_extract(o.value_json, '$')
               WHERE p.is_dm=1 AND o.field='phone' AND o.verification_state NOT IN ('rejected','stale')
                 AND c.phone_type IN ('direct','mobile')"""
        ),
        "accepted_dms_with_person_email": q(
            """SELECT COUNT(DISTINCT p.business_id) FROM people p
               JOIN enrichment_observations o ON o.business_id=p.business_id AND o.person_id=p.id
               WHERE p.is_dm=1 AND o.field='email' AND o.verification_state NOT IN ('rejected','stale')"""
        ),
    })
    return metrics


def source_hit_rates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Group observation yield by provider/source, including cost and verified rate."""
    rows = conn.execute(
        """SELECT provider, source, COUNT(*) total,
                  SUM(CASE WHEN verification_state='verified' THEN 1 ELSE 0 END) verified,
                  COALESCE(SUM(cost), 0) cost
           FROM enrichment_observations GROUP BY provider, source ORDER BY provider, source"""
    ).fetchall()
    return [
        {"provider": r["provider"], "source": r["source"], "observations": r["total"],
         "verified": r["verified"], "hit_rate": (r["verified"] / r["total"] if r["total"] else 0.0),
         "cost": r["cost"], "cost_per_verified": (r["cost"] / r["verified"] if r["verified"] else None)}
        for r in rows
    ]


def outreach_attribution(conn: sqlite3.Connection, campaign: str | None = None) -> list[dict[str, Any]]:
    """Attribute outcomes to source/provider/contact type when callers supplied them."""
    where, params = ("WHERE campaign=?", (campaign,)) if campaign else ("", ())
    rows = conn.execute(
        f"""SELECT source, provider, contact_type, result, COUNT(*) count
            FROM outcomes {where}
            GROUP BY source, provider, contact_type, result
            ORDER BY source, provider, contact_type, result""", params
    ).fetchall()
    return [dict(r) for r in rows]


def cost_per_verified_dm(conn: sqlite3.Connection) -> float | None:
    """Return observation cost per distinct verified DM, or None when absent."""
    row = conn.execute(
        """SELECT COALESCE(SUM(cost), 0) cost,
                  COUNT(DISTINCT COALESCE(CAST(person_id AS TEXT), business_id || ':' || field)) verified
           FROM enrichment_observations
           WHERE field IN ('person', 'dm', 'person.name') AND verification_state='verified'"""
    ).fetchone()
    return (row["cost"] / row["verified"]) if row["verified"] else None


def decode_observation_value(row: sqlite3.Row) -> Any:
    return json.loads(row["value_json"])


def backfill_legacy_observations(conn: sqlite3.Connection) -> dict[str, int]:
    """Project existing legacy rows into observations without changing pipeline behavior.

    The operation is deliberately idempotent: rerunning it finds the same business/field/value/source
    tuple and does not create another observation. Legacy rows remain authoritative for current scoring.
    """
    counts = {"contacts": 0, "people": 0, "evidence": 0, "inserted": 0}

    def add(observation: EnrichmentObservation) -> None:
        value_json = json.dumps(observation.value)
        exists = conn.execute(
            """SELECT 1 FROM enrichment_observations
               WHERE business_id=? AND field=? AND value_json=? AND source=? AND source_url=?
                 AND COALESCE(person_id, 0)=COALESCE(?, 0) LIMIT 1""",
            (observation.business_id, observation.field, value_json, observation.source,
             observation.source_url, observation.person_id),
        ).fetchone()
        if exists:
            return
        db.add_observation(conn, observation)
        counts["inserted"] += 1

    for row in conn.execute("SELECT * FROM contacts").fetchall():
        meta = json.loads(row["meta_json"] or "{}")
        state = {"valid": "verified", "invalid": "rejected"}.get(
            row["tier"], "observed" if row["verified_at"] else "unverified"
        )
        add(EnrichmentObservation(
            business_id=row["business_id"], field=row["kind"], value=row["value"],
            source=f"legacy:contact:{row['email_stage'] if row['kind'] == 'email' else row['phone_type']}",
            source_url=meta.get("url", ""), evidence=meta.get("reason", ""),
            verification_state=state, provider=meta.get("provider", "oss"),
            cost=float(meta.get("cost", 0) or 0), jurisdiction=meta.get("jurisdiction", ""),
            observed_at=row["verified_at"] or now_iso(), verified_at=row["verified_at"] or "",
        ))
        counts["contacts"] += 1

    for row in conn.execute("SELECT * FROM people").fetchall():
        add(EnrichmentObservation(
            business_id=row["business_id"], field="person.name", value=row["name"],
            source=f"legacy:person:{row['origin'] or row['labeled_by'] or 'unknown'}",
            source_url=row["source_url"], evidence=row["snippet"], confidence=row["dm_confidence"],
            verification_state="verified" if row["is_dm"] == 1 else "observed",
            person_id=row["id"], person_name=row["name"], observed_at=row["labeled_at"] or now_iso(),
        ))
        counts["people"] += 1

    for row in conn.execute("SELECT * FROM evidence").fetchall():
        field = "person.name" if row["ref_table"] == "people" else row["fact"]
        person_id = row["ref_id"] if row["ref_table"] == "people" else None
        add(EnrichmentObservation(
            business_id=row["business_id"], field=field, value=row["fact"], source=f"legacy:evidence:{row['fact']}",
            source_url=row["url"], evidence=row["snippet"], person_id=person_id,
            observed_at=row["observed_at"] or now_iso(), verification_state="observed",
        ))
        counts["evidence"] += 1

    conn.commit()
    return counts
