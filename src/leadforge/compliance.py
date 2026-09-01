"""Compliance gate (v0.3, U9.2) — entity type, lawful basis and the phone-first Next Action.

Pure functions over DB rows. Used by export (the sheet's Entity Type / Lawful Basis / Next Action
columns), by the outreach eligibility gate, and by the drafting packet builder. Nothing here does I/O.

Legal framing (UK PECR reg. 22 / UK GDPR, US CAN-SPAM) is a practical posture, not legal advice —
see docs/07-compliance.md. The owner decision of 2026-09-02 (docs/09 §decisions) sets the defaults:
phone-first, freemail addresses emailable once they pass the plausibility check, entity type always
reported so a stricter policy can be switched on per campaign (`outreach.require_corporate`).
"""

from __future__ import annotations

import json
from typing import Any

# --------------------------------------------------------------------------- entity type
ENTITY_CORPORATE_ACTIVE = "corporate_active"        # registry match, company_status == active
ENTITY_CORPORATE_INACTIVE = "corporate_inactive"    # registry match, dissolved/liquidation/other
ENTITY_CORPORATE_UNKNOWN = "corporate_unknown"      # registry officers stored, no profile persisted (pre-v0.3 rows)
ENTITY_UNMATCHED = "unmatched"                      # registry checked, no company found -> likely sole trader/partnership
ENTITY_UNCHECKED = "unchecked"                      # registry never consulted (no key, other country, not run)

_ACTIVE_STATUSES = {"active", "open"}


def _enrich(row: Any) -> dict:
    raw = row["enrich_json"] if isinstance(row, dict) or hasattr(row, "keys") else None
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}


def entity_type(row: Any, people: list[Any] | None = None) -> str:
    """Classify a business row by what the public registry says about it.

    `people` (rows with `labeled_by`) lets pre-v0.3 databases — where the crawl path stored
    officers but never the company profile — still count as corporate (status unknown)."""
    enrich = _enrich(row)
    profile = enrich.get("registry_profile") or {}
    if profile.get("company_number"):
        status = str(profile.get("company_status") or "").strip().casefold()
        if status in _ACTIVE_STATUSES:
            return ENTITY_CORPORATE_ACTIVE
        if status:
            return ENTITY_CORPORATE_INACTIVE
        return ENTITY_CORPORATE_UNKNOWN
    if people and any((p["labeled_by"] if not isinstance(p, dict) else p.get("labeled_by")) == "registry" for p in people):
        return ENTITY_CORPORATE_UNKNOWN
    if enrich.get("registry_checked"):
        return ENTITY_UNMATCHED
    return ENTITY_UNCHECKED


# --------------------------------------------------------------------------- lawful basis (email)
BASIS_B2B = "b2b_legitimate_interest"     # corporate subscriber (UK) / business address (US): opt-out model
BASIS_CONSENT = "consent_required"        # individual subscriber (UK sole trader on a personal mailbox)
BASIS_CONFIRM = "unknown_confirm_first"   # entity type not established; a human confirms before mailing
BASIS_NONE = "none"                       # no usable address

_SENDABLE_TIERS = {"valid", "role"}


def lawful_basis_email(entity: str, email: str | None, tier: str, affinity: str, region_profile: str,
                       freemail_policy: str = "linked", require_corporate: bool = False) -> str:
    """Which basis an unsolicited B2B email to this address would rest on, under the campaign policy.

    freemail_policy: 'linked' (owner default: a freemail box that plausibly belongs to the business is
    mailable), 'any' (every syntactically valid freemail box), 'none' (own-domain only).
    require_corporate: only registry-confirmed active companies are mailable without consent."""
    if not email or tier not in _SENDABLE_TIERS or affinity in ("freemail_unlinked", "foreign"):
        return BASIS_NONE
    if affinity.startswith("freemail"):
        if freemail_policy == "none":
            return BASIS_NONE
        if freemail_policy == "linked" and affinity != "freemail_linked":
            return BASIS_NONE
    if region_profile == "us":
        return BASIS_B2B  # CAN-SPAM: opt-out model for any commercial message; sender + postal address required
    # uk / eu: PECR-style corporate-subscriber rule
    if entity == ENTITY_CORPORATE_ACTIVE:
        return BASIS_B2B
    if entity == ENTITY_CORPORATE_INACTIVE:
        return BASIS_CONFIRM
    if require_corporate:
        return BASIS_CONFIRM if entity == ENTITY_CORPORATE_UNKNOWN else BASIS_CONSENT
    if entity == ENTITY_CORPORATE_UNKNOWN:
        return BASIS_B2B
    if entity == ENTITY_UNMATCHED:
        # owner decision 5: a plausibly-linked address is mailable; the sheet still flags the entity gap
        return BASIS_B2B if affinity in ("own_domain", "freemail_linked") else BASIS_CONFIRM
    return BASIS_CONFIRM


# --------------------------------------------------------------------------- eligibility + next action
def email_eligibility(row: Any, contacts: list[Any], entity: str, region_profile: str, *,
                      freemail_policy: str = "linked", require_corporate: bool = False,
                      suppressed: bool = False, site_dead: bool = False) -> dict:
    """-> {"eligible": bool, "email": str|None, "basis": str, "reasons": [..]} for the best address."""
    from leadforge.enrich.validate import rank_email_contacts

    reasons: list[str] = []
    if suppressed:
        reasons.append("suppressed")
    ranked = rank_email_contacts(list(contacts))
    best = ranked[0] if ranked else None
    email = best["value"] if best else None
    tier = best["tier"] if best else ""
    affinity = (best["affinity"] if best and "affinity" in best.keys() else "") or _infer_affinity(email, row)
    basis = lawful_basis_email(entity, email, tier, affinity, region_profile,
                               freemail_policy=freemail_policy, require_corporate=require_corporate)
    if basis == BASIS_NONE:
        reasons.append("no_sendable_email")
    elif basis != BASIS_B2B:
        reasons.append(basis)
    if site_dead:
        reasons.append("site_dead")
    return {"eligible": not reasons, "email": email, "tier": tier, "affinity": affinity,
            "basis": basis, "reasons": reasons}


def _infer_affinity(email: str | None, row: Any) -> str:
    """Rows written before v0.3 carry no `affinity`; derive the coarse class from the domain."""
    if not email:
        return ""
    from leadforge.enrich.extract import FREEMAIL_DOMAINS

    dom = email.rsplit("@", 1)[-1].casefold()
    biz = (row["domain"] or "") if hasattr(row, "keys") else ""
    if biz and (dom == biz.casefold() or dom.endswith("." + biz.casefold())):
        return "own_domain"
    return "freemail_linked" if dom in FREEMAIL_DOMAINS else "foreign"


NEXT_CALL_NAMED = "CALL - named contact"
NEXT_CALL_SWITCHBOARD = "CALL - ask for the owner"
NEXT_EMAIL = "EMAIL - eligible"
NEXT_EMAIL_CONFIRM = "EMAIL - confirm entity first"
NEXT_RESEARCH = "RESEARCH - no reachable channel"
NEXT_DQ = "SKIP - disqualified"


def next_action(*, phone_validated: bool, has_dm: bool, eligibility: dict, tier: str = "",
                outreach_state: str | None = None) -> str:
    """Phone-first ordering (owner decision 2). An existing outreach state wins so the sheet shows
    where a lead actually is in the sequence."""
    if outreach_state:
        return f"OUTREACH - {outreach_state}"
    if tier == "DQ":
        return NEXT_DQ
    if phone_validated and has_dm:
        return NEXT_CALL_NAMED
    if phone_validated:
        return NEXT_CALL_SWITCHBOARD
    if eligibility.get("eligible"):
        return NEXT_EMAIL
    if eligibility.get("basis") in (BASIS_CONFIRM, BASIS_CONSENT):
        return NEXT_EMAIL_CONFIRM
    return NEXT_RESEARCH


def name_allowed(person: Any, enrich: dict, corroborations: int) -> bool:
    """Owner decision 6: a registry director's name may open a message only when the match passed the
    similarity gate, the company is active, and at least one other source corroborates the name."""
    origin = (person["origin"] if "origin" in person.keys() else "") or person["labeled_by"]
    if origin != "registry":
        return True
    profile = enrich.get("registry_profile") or {}
    status = str(profile.get("company_status") or "").casefold()
    sim = float(profile.get("match_similarity") or 0.0)
    return status in _ACTIVE_STATUSES and sim > 0.0 and corroborations >= 1
