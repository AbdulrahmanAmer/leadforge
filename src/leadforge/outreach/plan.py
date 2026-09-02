"""`outreach plan` (v0.3 unit E, docs/09 Wave 2 E #3) — enrol scored leads as outreach_targets.

One target per (business, campaign). Chain-mates (docs/09: same non-freemail domain or phone across
>= 2 businesses — `db.chain_map`) get at most one enrolled target, so a 3-location chain is never
pitched three separate times; the highest-scoring member of the chain wins because
`db.scores_for_run` already orders by score DESC and we only reserve a chain key once a target from
it is actually enrolled (so a top scorer that fails eligibility for another reason does not burn the
chain's only slot).

`eligibility_json` also carries `_tier` (the lead's score tier at plan time) — `outreach_targets` has
no `run_id`/tier column of its own, and re-deriving it from a live `scores` join at approve/status
time would silently drift if the campaign is re-scored; this is unit E's own field, read only by
unit E's own code (approve.py, status.py).
"""

from __future__ import annotations

import json
import sqlite3

from leadforge import compliance, db
from leadforge.config import Config
from leadforge.enrich.validate import rank_email_contacts
from leadforge.models import ICP
from leadforge.outreach.identity import get_identity
from leadforge.score import fill_email_affinity
from leadforge.util import LeadForgeError, now_iso

EXCLUSION_REASONS = ["suppressed", "no_sendable_email", "entity_gate", "chain_duplicate", "site_dead", "already_enrolled"]


def site_is_dead(enrich: dict) -> bool:
    """docs/09 §E3's own definition — deliberately simpler than export.py's Site Status column: an
    enrich error other than a robots refusal, or an HTTP status >= 400. Public: send.py reuses this
    same definition when it recomputes eligibility at send time."""
    err = enrich.get("error")
    status = (enrich.get("signals") or {}).get("http_status")
    if err == "robots-disallowed":
        return False
    return bool(err) or (isinstance(status, int) and status >= 400)


def plan_targets(conn: sqlite3.Connection, cfg: Config, icp: ICP, *, campaign: str, run_id: str,
                  tiers: list[str], identity_label: str, limit: int | None = None,
                  client_id: str = "") -> dict:
    identity = get_identity(conn, identity_label)
    if identity is None:
        raise LeadForgeError(f"unknown identity '{identity_label}' — add one with `leadforge outreach identity add`")

    scored = db.scores_for_run(conn, run_id)
    chain = db.chain_map(conn)
    seen_chain_keys: set[str] = set()
    counts = dict.fromkeys(EXCLUSION_REASONS, 0)
    counts["enrolled"] = 0
    counts["scanned"] = 0
    enrolled_ids: list[int] = []

    for s in scored:
        if tiers and s["tier"] not in tiers:
            continue
        counts["scanned"] += 1
        business_id = s["business_id"]

        already = conn.execute(
            "SELECT id FROM outreach_targets WHERE business_id=? AND campaign=?", (business_id, campaign)
        ).fetchone()
        if already:
            counts["already_enrolled"] += 1
            continue

        chain_key = chain.get(business_id)
        if chain_key and chain_key in seen_chain_keys:
            counts["chain_duplicate"] += 1
            continue

        people = db.people_for(conn, business_id)
        contacts_filled = fill_email_affinity(db.contacts_for(conn, business_id), s["domain"])
        ranked = rank_email_contacts(contacts_filled)
        candidate_email = ranked[0]["value"] if ranked else None
        suppressed = db.is_suppressed(conn, candidate_email, s["domain"])
        enrich = json.loads(s["enrich_json"] or "{}")
        dead = site_is_dead(enrich)
        entity = compliance.entity_type(s, people)
        elig = compliance.email_eligibility(
            s, contacts_filled, entity, icp.compliance.region_profile,
            freemail_policy=cfg.validation.freemail_policy, require_corporate=cfg.outreach.require_corporate,
            suppressed=suppressed, site_dead=dead,
        )

        if "suppressed" in elig["reasons"]:
            counts["suppressed"] += 1
            continue
        if "no_sendable_email" in elig["reasons"]:
            counts["no_sendable_email"] += 1
            continue
        if elig["basis"] in (compliance.BASIS_CONFIRM, compliance.BASIS_CONSENT):
            counts["entity_gate"] += 1
            continue
        if "site_dead" in elig["reasons"]:
            counts["site_dead"] += 1
            continue
        if not elig["eligible"]:
            counts["no_sendable_email"] += 1  # defensive: any remaining ineligible reason
            continue

        elig_stored = dict(elig)
        elig_stored["_tier"] = s["tier"]
        elig_stored["_region_profile"] = icp.compliance.region_profile
        contact_id = ranked[0]["id"] if ranked else None
        cur = conn.execute(
            """INSERT INTO outreach_targets(business_id,contact_id,campaign,client_id,identity_id,state,
               eligibility_json,touches,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,0,?,?)""",
            (business_id, contact_id, campaign, client_id, identity["id"], "enrolled",
             json.dumps(elig_stored), now_iso(), now_iso()),
        )
        conn.commit()
        enrolled_ids.append(int(cur.lastrowid))
        if chain_key:
            seen_chain_keys.add(chain_key)
        counts["enrolled"] += 1
        if limit is not None and counts["enrolled"] >= limit:
            break

    return {"counts": counts, "target_ids": enrolled_ids}
