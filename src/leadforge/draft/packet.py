"""Evidence packet builder (v0.3 unit F, ADR-012, docs/09 Wave 2 F).

`build_packet` turns one business (+ optionally its best contact) into a compact, ONLY-evidenced JSON
object the agent drafts a subject + one observation sentence from. Every fact carries its source
(`src`) and when it was observed (`at`) so the mechanical gate (`draft/gate.py`) — and a human
reviewer — can trace every claim the agent is allowed to make back to real data. Nothing here writes
to the DB; pure read + assemble.

Fact value convention: booleans are written as short human-readable strings ("has online booking",
not the Python literal True) so the USED_FACT gate check can find them quoted back in prose; a fact's
mere PRESENCE still means "true" for the NEGATION gate check (see gate.py).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from leadforge import compliance, db
from leadforge.config import Config
from leadforge.models import ICP
from leadforge.util import natural_name, now_iso

# Facts that carry real, business-specific personalisation value (drive grade A/B and are the last
# ones dropped when a packet must be trimmed to fit cfg.draft.packet_max_tokens).
DISTINCTIVE_KEYS = {
    "legal_name", "incorporated_year", "company_status", "booking", "site_stale",
    "hiring", "phone_confirmed", "gbp_appointments", "review_name",
}
# Facts every business in a dead/quiet segment shares — never enough alone for a personal-sounding
# line (grade C per docs/09 §F: "segment-only facts such as no_website/no_social_link").
SEGMENT_KEYS = {"no_website", "no_social_link"}
# Baseline identity facts: useful context, but not "personalisation" for grading purposes, and the
# first to go when a packet is over budget.
_BASELINE_KEYS = ("category", "rating", "city")
# Lowest value first: this is the order build_packet drops facts in to fit packet_max_tokens.
# dm_name is never in this list — the docs/08 name-gate result is exactly the thing a trimmed packet
# must not silently drop (that would either invent a name with no evidence, or waste the gate check).
_DROP_ORDER = [
    "category", "rating", "city", "no_social_link", "no_website", "review_name",
    "gbp_appointments", "hiring", "phone_confirmed", "site_stale", "company_status",
    "incorporated_year", "legal_name", "booking",
]

_NAME_TOKEN_RE = re.compile(r"[A-Za-z']+")


def _tokens(name: str) -> set[str]:
    return {t.casefold() for t in _NAME_TOKEN_RE.findall(name or "") if len(t) >= 2}


def _load_enrich(business_row: Any) -> dict:
    raw = business_row["enrich_json"] if "enrich_json" in business_row.keys() else None
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}


def _corroborations(dm: Any, people: list[Any], contact_row: Any, gbp: dict) -> int:
    """How many independent sources back the registry-derived name `dm` (docs/09 §F): the name
    appears among the site's own people (heuristic/gbp origin), the chosen contact's email local
    part contains a name token, or a GBP owner-reply signature matches. Each channel counts once."""
    dm_tokens = _tokens(dm["name"])
    if not dm_tokens:
        return 0
    n = 0
    if any(
        (p["origin"] if "origin" in p.keys() else p["labeled_by"]) in ("heuristic", "gbp")
        and p["name"] != dm["name"]
        and (_tokens(p["name"]) & dm_tokens)
        for p in people
    ):
        n += 1
    if contact_row is not None and "kind" in contact_row.keys() and contact_row["kind"] == "email":
        local = re.sub(r"[^a-z]", "", contact_row["value"].split("@", 1)[0].casefold())
        if any(len(t) >= 3 and t in local for t in dm_tokens):
            n += 1
    if any(_tokens(str(sig)) & dm_tokens for sig in (gbp.get("reply_signatures") or [])):
        n += 1
    return n


def _grade(fact_keys: set[str], has_name: bool) -> str:
    distinctive = bool(fact_keys & DISTINCTIVE_KEYS)
    if has_name and distinctive:
        return "A"
    if distinctive:
        return "B"
    return "C"


def tokens_est(packet: dict) -> int:
    """Rough token estimate (docs/09 §F: ceil(len(json)/4)) — used both to enforce packet_max_tokens
    and to report tokens_est in the export digest/NDJSON line."""
    return math.ceil(len(json.dumps(packet, ensure_ascii=False)) / 4)


def _enforce_token_budget(packet: dict, max_tokens: int) -> None:
    """Drop the lowest-value facts first (description-like, then segment) until the packet fits —
    never drops offer/sender/purpose, which live outside `facts` entirely. Mutates packet in place."""
    facts = packet["facts"]
    by_key = {f["k"]: f for f in facts}
    order = iter(_DROP_ORDER)
    while tokens_est(packet) > max_tokens and facts:
        key = next(order, None)
        if key is not None and key in by_key:
            facts.remove(by_key.pop(key))
            continue
        if key is None:
            # every named key already gone (or never present) but still over budget — drop from
            # the end rather than loop forever; dm_name (added last) survives longest either way.
            facts.pop()


def build_packet(
    conn,
    cfg: Config,
    icp: ICP,
    business_row: Any,
    contact_row: Any,
    purpose: str,
    identity_dict: dict,
) -> dict:
    enrich = _load_enrich(business_row)
    signals = enrich.get("signals") or {}
    gbp = enrich.get("gbp") or {}
    crawled = bool(enrich.get("crawled_at")) and (enrich.get("pages") or 0) > 0
    crawl_at = enrich.get("crawled_at") or now_iso()
    seen_at = (business_row["last_seen_at"] if "last_seen_at" in business_row.keys() else "") or now_iso()

    people = db.people_for(conn, business_row["id"])
    dm = next((p for p in people if p["is_dm"] == 1), None)

    facts: list[dict] = []

    def add(k: str, v: Any, src: str, at: str) -> None:
        facts.append({"k": k, "v": v, "src": src, "at": at or now_iso()})

    category = business_row["category"] if "category" in business_row.keys() else None
    if category:
        add("category", category, "maps", seen_at)
    city = (business_row["address_city"] if "address_city" in business_row.keys() else "") or ""
    if city:
        add("city", city, "maps", seen_at)
    rating = business_row["rating"] if "rating" in business_row.keys() else None
    review_count = business_row["review_count"] if "review_count" in business_row.keys() else None
    if rating is not None and review_count is not None:
        add("rating", f"{rating} stars ({review_count} reviews)", "maps", seen_at)

    regp = enrich.get("registry_profile") or {}
    sim = float(regp.get("match_similarity") or 0)
    status = str(regp.get("company_status") or "").casefold()
    if sim >= cfg.registry.min_name_similarity and status == "active":
        if regp.get("legal_name"):
            add("legal_name", regp["legal_name"], "registry", seen_at)
        year = str(regp.get("incorporated") or "")[:4]
        if year.isdigit():
            add("incorporated_year", year, "registry", seen_at)
        if regp.get("company_status"):
            add("company_status", regp["company_status"], "registry", seen_at)

    # booking: a GBP order-online/booking link is evidence regardless of whether the site was
    # crawled; a site-derived hint (regex/known-platform match) counts only on a real, non-phantom
    # crawl. runner.py's _apply_gbp already folds a GBP booking link into signals.booking_hint with
    # booking_source="gbp" for site-less businesses too — check the GBP facts directly here so this
    # module never needs to special-case that merge.
    if gbp.get("booking_links"):
        add("booking", "has online booking (Google Business)", "gbp", seen_at)
    elif crawled and signals.get("booking_hint") and signals.get("booking_source") != "gbp":
        add("booking", "shows an online-booking option on its site", "site", crawl_at)

    website = business_row["website"] if "website" in business_row.keys() else None
    if not website:
        add("no_website", "no business website found", "maps", seen_at)

    if crawled and signals.get("stale_site") and signals.get("copyright_year"):
        add("site_stale", signals["copyright_year"], "site", crawl_at)

    if crawled and "socials" in enrich and not enrich.get("socials"):
        add("no_social_link", "no linked social profile found on the site", "site", crawl_at)

    if crawled and signals.get("careers"):
        add("hiring", "has a live careers/jobs page", "site", crawl_at)

    if crawled and signals.get("phone_confirmed"):
        add("phone_confirmed", "site phone matches the Google Business phone", "site", crawl_at)

    appt = gbp.get("appointments") or "none"
    if appt != "none":
        add("gbp_appointments", appt, "gbp", seen_at)

    if gbp.get("review_names"):
        add("review_name", gbp["review_names"][0], "gbp", seen_at)

    greeting = "Hello,"
    if dm is not None:
        policy = cfg.draft.name_policy
        if policy == "never":
            allowed = False
        elif policy == "always":
            allowed = True
        else:
            corrob = _corroborations(dm, people, contact_row, gbp)
            allowed = compliance.name_allowed(dm, enrich, corrob)
        if allowed:
            first = natural_name(dm["name"]).split()[0] if dm["name"] else ""
            if first:
                origin = (dm["origin"] if "origin" in dm.keys() else "") or dm["labeled_by"]
                add("dm_name", natural_name(dm["name"]), origin, seen_at)
                greeting = f"Hi {first},"

    packet = {
        "co": business_row["name"],
        "city": city,
        "facts": facts,
        "offer": {"what": icp.offer.what, "value_prop": icp.offer.value_prop},
        "sender": {
            "from_name": identity_dict.get("from_name") or identity_dict.get("label") or "",
            "label": identity_dict.get("label", ""),
        },
        "purpose": purpose,
        "greeting": greeting,
        "constraints": {
            "max_observation_words": cfg.draft.max_observation_words,
            "max_subject_chars": cfg.draft.max_subject_chars,
            "template_numbers": [],
            "literals": [],
        },
    }
    _enforce_token_budget(packet, cfg.draft.packet_max_tokens)
    # grade reflects what SURVIVED trimming, not the pre-trim evidence — a packet squeezed to fit
    # packet_max_tokens must never claim a grade it can no longer back.
    fact_keys = {f["k"] for f in packet["facts"]}
    packet["grade"] = _grade(fact_keys, has_name="dm_name" in fact_keys)
    return packet
