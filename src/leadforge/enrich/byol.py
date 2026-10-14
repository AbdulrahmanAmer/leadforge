"""Conservative bring-your-own-list CSV import primitives."""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from leadforge import db
from leadforge.models import Contact, EnrichmentObservation, Person
from leadforge.util import now_iso


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _phone(value: Any) -> str:
    return re.sub(r"[^+0-9]", "", str(value or ""))


def _domain(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    return (parsed.hostname or "").removeprefix("www.")


def _match(conn: sqlite3.Connection, row: dict[str, str]) -> tuple[str | None, str]:
    business_id = str(row.get("business_id") or row.get("id") or "").strip()
    if business_id and conn.execute("SELECT 1 FROM businesses WHERE id=?", (business_id,)).fetchone():
        return business_id, "business_id"

    domain = _domain(row.get("domain") or row.get("website"))
    if domain:
        hits = conn.execute("SELECT id FROM businesses WHERE lower(replace(domain,'www.',''))=?", (domain,)).fetchall()
        if len(hits) == 1:
            return hits[0]["id"], "domain"
        if len(hits) > 1:
            return None, "ambiguous_domain"

    phone = _phone(row.get("phone"))
    if phone:
        hits = conn.execute("SELECT id FROM businesses WHERE phone_e164=?", (phone,)).fetchall()
        if len(hits) == 1:
            return hits[0]["id"], "phone"
        if len(hits) > 1:
            return None, "ambiguous_phone"

    name = _norm(row.get("business_name") or row.get("company") or row.get("name"))
    locality = _norm(row.get("city") or row.get("locality") or row.get("address_city"))
    if name and locality:
        hits = conn.execute(
            "SELECT id FROM businesses WHERE name_norm=? AND replace(lower(address_city),' ','')=?",
            (name, locality),
        ).fetchall()
        if len(hits) == 1:
            return hits[0]["id"], "name_locality"
        if len(hits) > 1:
            return None, "ambiguous_name_locality"
    return None, "unmatched"


def import_csv(conn: sqlite3.Connection, path: Path, dry_run: bool = True) -> dict[str, Any]:
    """Import public contact rows only when matching is unique and strong.

    Ambiguous/unmatched rows are reported and never attached to a business. The default
    is dry-run; callers must explicitly opt into writes.
    """
    result: dict[str, Any] = {"rows": 0, "matched": 0, "imported": 0, "ambiguous": 0, "unmatched": 0, "reasons": {}}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            row = {str(k).casefold().strip(): str(v or "").strip() for k, v in raw.items()}
            result["rows"] += 1
            business_id, reason = _match(conn, row)
            result["reasons"][reason] = result["reasons"].get(reason, 0) + 1
            if not business_id:
                result["ambiguous" if reason.startswith("ambiguous") else "unmatched"] += 1
                continue
            result["matched"] += 1
            if dry_run:
                continue
            person_name = row.get("person_name") or row.get("contact_name") or ""
            email = row.get("email") or ""
            phone = row.get("phone") or ""
            person_id = None
            if person_name:
                db.add_person(conn, Person(business_id=business_id, name=person_name, title=row.get("title", ""),
                                           labeled_by="byol", origin="byol", labeled_at=now_iso()))
                person_id = conn.execute(
                    "SELECT id FROM people WHERE business_id=? AND name=? AND title=?",
                    (business_id, person_name, row.get("title", "")),
                ).fetchone()[0]
            for kind, value in (("email", email), ("phone", phone)):
                if not value:
                    continue
                contact = Contact(business_id=business_id, kind=kind, value=value, label="byol", tier="unknown")
                contact_id = db.add_contact(conn, contact)
                db.add_observation(conn, EnrichmentObservation(
                    business_id=business_id, field=kind, value=value, source="byol_csv",
                    evidence="Imported from operator-supplied CSV", provider="byol", person_id=person_id,
                    person_name=person_name, observed_at=now_iso(), verification_state="unverified",
                ))
                if kind == "email" and contact_id == 0:  # defensive; normal upsert returns an id
                    continue
            result["imported"] += 1
    if not dry_run:
        conn.commit()
    return result
