"""Scoring & qualification engine (U5.1-5.2) — docs/01 §5, rubric config/scoring.default.yaml.

Pure, deterministic, unit-tested. Each factor returns (score 0..1, why-string); points = score*weight.
Negatives apply a capped penalty; hard qualifiers -> DQ. Need-hooks synthesized from signals + offer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from leadforge import db
from leadforge.models import ICP, Score, ScoreFactor
from leadforge.util import now_iso

_DEFAULT_RUBRIC = Path(__file__).resolve().parents[2] / "config" / "scoring.default.yaml"


def load_rubric(icp: ICP) -> dict:
    rubric = yaml.safe_load(_DEFAULT_RUBRIC.read_text(encoding="utf-8"))
    for factor, weight in icp.scoring.weights_override.items():
        if factor in rubric["factors"]:
            rubric["factors"][factor]["weight"] = weight
    return rubric


class Scorer:
    def __init__(self, conn: sqlite3.Connection, icp: ICP, run_id: str):
        self.conn = conn
        self.icp = icp
        self.run_id = run_id
        self.rubric = load_rubric(icp)
        self.wanted_cats = [c.casefold() for c in icp.target.categories]
        self.soft = set(icp.qualify.soft)

    # ---- individual factors ---------------------------------------------------------
    # EVERY factor function takes the SAME (self, business_row, ctx) signature and returns
    # (score 0..1, why-string), so `_FACTOR_FNS` can dispatch them uniformly from the rubric config.
    # Some factors legitimately ignore `b` or `ctx` — do NOT "tidy" those parameters away or the
    # table dispatch below breaks. (ruff ARG002 is intentionally not enabled for this reason.)
    def _f_industry_match(self, b, ctx) -> tuple[float, str]:
        cats = [c.casefold() for c in (json.loads(b["categories_json"]) or [])]
        primary = (b["category"] or "").casefold()
        if any(w == primary for w in self.wanted_cats):
            return 1.0, f"category '{b['category']}' is an exact ICP match"
        if any(w in c or c in w for w in self.wanted_cats for c in cats):
            return 0.6, "category adjacent to ICP targets"
        return 0.1, "category weakly related to ICP"

    def _f_size_match(self, b, ctx) -> tuple[float, str]:
        rc = b["review_count"]
        size = self.icp.target.size
        if rc is None:
            return 0.4, "no review count to size the business"
        if size.min_reviews and rc < size.min_reviews:
            return 0.2, f"{rc} reviews below min {size.min_reviews}"
        if size.max_reviews and rc > size.max_reviews:
            return 0.3, f"{rc} reviews above max {size.max_reviews}"
        return 1.0, f"{rc} reviews within target band"

    def _f_geography_match(self, b, ctx) -> tuple[float, str]:
        areas = [a.strip() for a in (self.icp.target.geography.areas or []) if a.strip()]
        if not areas:  # bbox/grid campaign: discovery itself is the geographic filter
            return 1.0, "discovered inside the campaign search area"
        hay = " ".join(str(b[k] or "") for k in
                       ("address_city", "address_region", "address_postal", "address_full")).casefold()
        if not hay.strip():
            return 0.5, "location present but unstructured"
        for area in areas:
            locality = area.split(",")[0].strip()  # "Houston, TX, United States" -> "Houston"
            if locality and locality.casefold() in hay:
                return 1.0, f"inside target area ({locality})"
        where = b["address_city"] or b["address_region"] or "unlisted town"
        # Maps results spill into surrounding towns; that's a soft miss, not a disqualifier —
        # the out_of_area penalty only fires on a country-level mismatch (see _negatives).
        return 0.3, f"address doesn't name a target area (listed: {where})"

    def _f_business_model(self, b, ctx) -> tuple[float, str]:
        name = (b["name_norm"] or "")
        chain_hits = [w for w in ("franchise", "group", "inc", "corporation") if w in name]
        if chain_hits:
            return 0.3, "name suggests a chain/franchise"
        return 0.8, "looks like an independent business"

    def _f_dm_identified(self, b, ctx) -> tuple[float, str]:
        dm = ctx["dm"]
        if dm and dm["is_dm"] == 1:
            return 1.0, f"decision maker: {dm['name']} ({dm['title']})"
        if ctx["people"]:
            return 0.3, "people found but no confirmed DM"
        return 0.0, "no named decision maker"

    def _f_verified_direct_contact(self, b, ctx) -> tuple[float, str]:
        has_phone = bool(b["phone_e164"])
        best = ctx["best_email_tier"]
        if best in ("valid",) and has_phone:
            return 1.0, "valid personal email + direct phone"
        if best in ("valid", "role"):
            return 0.6, f"{best} email on file"
        if has_phone:
            return 0.4, "phone only, no verified email"
        return 0.1, "no verified direct contact"

    def _f_need_signals(self, b, ctx) -> tuple[float, str]:
        hits = ctx["need_hits"]
        if not hits:
            return 0.2, "no strong need signal detected"
        score = min(1.0, 0.4 + 0.3 * len(hits))
        return score, "need signals: " + ", ".join(hits)

    def _f_data_confidence(self, b, ctx) -> tuple[float, str]:
        score = 0.3
        why = []
        if ctx["registry"]:
            score += 0.4
            why.append("registry-corroborated")
        if b["enrich_json"] and json.loads(b["enrich_json"]).get("crawled_at"):
            score += 0.2
            why.append("site crawled")
        if b["place_id"]:
            score += 0.1
            why.append("place_id")
        return min(1.0, score), ", ".join(why) or "single-source"

    _FACTOR_FNS = {
        "industry_match": _f_industry_match, "size_match": _f_size_match,
        "geography_match": _f_geography_match, "business_model": _f_business_model,
        "dm_identified": _f_dm_identified, "verified_direct_contact": _f_verified_direct_contact,
        "need_signals": _f_need_signals, "data_confidence": _f_data_confidence,
    }

    # ---- context assembly + hard/negative rules -------------------------------------
    def _context(self, b) -> dict:
        people = db.people_for(self.conn, b["id"])
        dm = next((p for p in people if p["is_dm"] == 1), None)
        contacts = db.contacts_for(self.conn, b["id"])
        email_tiers = [c["tier"] for c in contacts if c["kind"] == "email"]
        from leadforge.enrich.validate import best_email_tier
        enrich = json.loads(b["enrich_json"]) if b["enrich_json"] else {}
        need_hits = self._need_hits(b, enrich)
        registry = any(p["labeled_by"] == "registry" for p in people)
        return {
            "people": people, "dm": dm, "best_email_tier": best_email_tier(email_tiers),
            "need_hits": need_hits, "enrich": enrich, "registry": registry,
        }

    def _need_hits(self, b, enrich: dict) -> list[str]:
        hits = []
        signals = enrich.get("signals", {})
        if "website_missing" in self.soft and not b["website"]:
            hits.append("website_missing")
        if "website_no_ssl" in self.soft and b["website"] and signals.get("https") is False:
            hits.append("website_no_ssl")
        if "stale_site" in self.soft and signals.get("stale_site"):
            hits.append("stale_site")
        if "low_rating_high_volume" in self.soft and (b["rating"] or 5) <= 3.9 and (b["review_count"] or 0) >= 50:
            hits.append("low_rating_high_volume")
        if "few_reviews" in self.soft and (b["review_count"] or 0) < 10:
            hits.append("few_reviews")
        if "weak_social_presence" in self.soft and not enrich.get("socials"):
            hits.append("weak_social_presence")
        if "phone_only_booking" in self.soft and enrich.get("crawled_at") and not signals.get("booking_hint"):
            hits.append("phone_only_booking")
        if "hiring" in self.soft and signals.get("careers"):
            hits.append("hiring")
        # social/video presence signals (set by the optional Agent-Reach unit U4.8; absent until enabled)
        for sig in ("stale_social", "no_social_presence", "no_video_presence"):
            if sig in self.soft and signals.get(sig):
                hits.append(sig)
        return hits

    def _hard_dq(self, b, ctx) -> str | None:
        for q in self.icp.qualify.hard:
            if q == "no_phone" and not b["phone_e164"]:
                return "no_phone"
            if q == "franchise_or_chain" and any(w in (b["name_norm"] or "") for w in ("franchise", "group")):
                return "franchise_or_chain"
            if q == "no_website_hard" and not b["website"]:
                return "no_website_hard"
            if q.startswith("competitor:") and q.split(":", 1)[1].casefold() in (b["name_norm"] or ""):
                return q
            if q.startswith("existing_client:") and q.split(":", 1)[1].casefold() in (b["name_norm"] or ""):
                return q
        return None

    _COUNTRY_ALIASES = {"uk": "gb", "united kingdom": "gb", "united states": "us", "usa": "us"}

    def _negatives(self, b, ctx) -> list[tuple[str, float]]:
        neg = self.rubric.get("negatives", {})
        out = []
        name = b["name_norm"] or ""
        if "franchise" in name or "group" in name:
            out.append(("franchise_or_chain", neg.get("franchise_or_chain", -25)))
        # out_of_area only on unambiguous evidence: the listing's own country differs from the
        # campaign's. Same-country suburb spillover is graded softly by geography_match instead.
        want = (self.icp.target.geography.country or "").strip().casefold()
        got = (b["address_country"] or "").strip().casefold()
        want, got = self._COUNTRY_ALIASES.get(want, want), self._COUNTRY_ALIASES.get(got, got)
        if want and got and want != got:
            out.append(("out_of_area", neg.get("out_of_area", -20)))
        return out

    def _hooks(self, b, ctx) -> list[str]:
        templates = self.rubric.get("hooks", {})
        offer = self.icp.offer.what
        out = []
        for sig in ctx["need_hits"]:
            tmpl = templates.get(sig)
            if tmpl:
                out.append(tmpl.replace("{offer}", offer))
        return out

    # ---- main -----------------------------------------------------------------------
    def score_business(self, b) -> Score:
        ctx = self._context(b)
        dq = self._hard_dq(b, ctx)
        factors: list[ScoreFactor] = []
        total = 0.0
        for fname, fcfg in self.rubric["factors"].items():
            fn = self._FACTOR_FNS[fname]
            s, why = fn(self, b, ctx)
            pts = round(s * fcfg["weight"], 2)
            total += pts
            factors.append(ScoreFactor(factor=fname, group=fcfg["group"], weight=fcfg["weight"],
                                       score=round(s, 3), points=pts, why=why))
        neg_total = 0.0
        for label, pts in self._negatives(b, ctx):
            neg_total += pts
            factors.append(ScoreFactor(factor=f"negative:{label}", group="negative", weight=pts,
                                       score=1.0, points=pts, why=f"penalty: {label}"))
        neg_total = max(neg_total, self.rubric.get("negatives_cap", -40))
        total = max(0.0, min(100.0, total + neg_total))

        if dq:
            tier = "DQ"
        elif total >= self.rubric["tiers"]["a_min"]:
            tier = "A"
        elif total >= self.rubric["tiers"]["b_min"]:
            tier = "B"
        else:
            tier = "C"

        return Score(business_id=b["id"], run_id=self.run_id, total=round(total, 1), tier=tier,
                     factors=factors, need_hooks=self._hooks(b, ctx), scored_at=now_iso())


def score_run(conn: sqlite3.Connection, icp: ICP, run_id: str) -> dict:
    if icp.scoring.profile == "account_fit":
        return score_run_account_fit(conn, icp, run_id)
    scorer = Scorer(conn, icp, run_id)
    counts = {"scored": 0, "tier_a": 0, "tier_b": 0, "tier_c": 0, "dq": 0}
    for b in db.all_businesses(conn):
        s = scorer.score_business(b)
        db.save_score(conn, s)
        counts["scored"] += 1
        counts[{"A": "tier_a", "B": "tier_b", "C": "tier_c", "DQ": "dq"}[s.tier]] += 1
    conn.commit()
    return counts


# ====================================================================================== account_fit
# WE SCORE profile (v0.1.1): a fixed 0-100 account-fit rubric with A-D grades, plus separate
# contactability and data-confidence scores (stored as meta factors so export can surface them).
# Hard rule: UNKNOWN never scores as NO - unknown inputs earn 0 points but lower data confidence,
# never a disqualification.

def _grade(total: float, dq: bool) -> str:
    if dq:
        return "DQ"
    if total >= 80:
        return "A"
    if total >= 65:
        return "B"
    if total >= 50:
        return "C"
    return "D"


def _industry_fit(b, icp: ICP) -> tuple[float, str, bool]:
    """(points/15, why, known). Aliases from ICP categories; no match => manual review, not zero-known."""
    cats = [c.casefold() for c in ([b["category"] or ""] + json.loads(b["categories_json"] or "[]")) if c]
    targets = [t.casefold() for t in icp.target.categories]
    for c in cats:
        for t in targets:
            if t in c or c in t:
                return 15.0, f"industry '{c}' matches target '{t}'", True
    return 0.0, "industry outside target list -> manual review", bool(cats)


def score_account_fit(conn: sqlite3.Connection, icp: ICP, run_id: str, b) -> Score:
    from leadforge.enrich.profile import trigger_freshness
    from leadforge.enrich.validate import best_email_tier

    enrich = json.loads(b["enrich_json"]) if b["enrich_json"] else {}
    prof = enrich.get("profile") or {}
    people = db.people_for(conn, b["id"])
    contacts = db.contacts_for(conn, b["id"])
    dm = next((p for p in people if p["is_dm"] == 1), None)
    factors: list[ScoreFactor] = []
    known = 0
    considered = 0

    def add(name: str, pts: float, cap: float, why: str, is_known: bool) -> None:
        nonlocal known, considered
        considered += 1
        known += int(is_known)
        factors.append(ScoreFactor(factor=name, group="account_fit", weight=cap,
                                   score=round(pts / cap, 3) if cap else 0.0, points=pts, why=why))

    # A. Industry fit (15)
    pts, why, is_known = _industry_fit(b, icp)
    industry_match = pts > 0
    add("industry_fit", pts, 15, why, is_known)

    # B. Employee size (15)
    emp = (prof.get("employee_count") or {}).get("value")
    rng = prof.get("employee_range", "unknown")
    if rng == "50-500":
        add("employee_size", 15, 15, f"{emp} employees (target band)", True)
    elif rng == "20-49":
        add("employee_size", 5, 15, f"{emp} employees (secondary band)", True)
    elif rng in ("<20", ">500"):
        add("employee_size", 0, 15, f"{emp} employees (outside ICP)", True)
    else:
        add("employee_size", 0, 15, "employee count unknown", False)

    # C. Revenue (10) - publicly underivable in most cases; UNKNOWN != NO_MATCH
    rev = (prof.get("revenue") or {}).get("value")
    if rev is None:
        add("revenue", 0, 10, "revenue unknown", False)
    else:
        in_band = 10_000_000 <= rev <= 150_000_000
        add("revenue", 10 if in_band else 0, 10,
            f"revenue {rev} ({'in' if in_band else 'outside'} band)", True)

    # D. Growth / expansion (15) - from detected triggers
    triggers = prof.get("triggers") or []
    strong_growth = [t for t in triggers if t["strength"] == "strong"]
    if strong_growth:
        add("growth", 15, 15, f"expansion evidence: {strong_growth[0]['text'][:80]}", True)
    elif triggers:
        add("growth", 8, 15, f"moderate evidence: {triggers[0]['text'][:80]}", True)
    else:
        add("growth", 0, 15, "no growth evidence found", False)

    # E. Organisational complexity (10)
    depts = prof.get("departments") or []
    c_pts = (5 if len(depts) >= 2 else 0) + (5 if any(d in ("operations", "it") for d in depts) else 0)
    add("org_complexity", c_pts, 10, f"departments: {', '.join(depts) or 'none detected'}", bool(depts))

    # F. Technology maturity (15)
    tech = prof.get("tech") or {}
    t_pts, t_known, t_why = 0, False, []
    for key, label, w in (("microsoft_365", "Microsoft 365", 5), ("crm", "CRM", 5), ("erp", "ERP", 5)):
        fct = tech.get(key) or {}
        if fct.get("value") == "yes":
            t_pts += w
            t_why.append(f"{label}: yes ({fct.get('name', fct.get('source', ''))})")
            t_known = True
        elif fct.get("state") == "CONFIRMED":
            t_known = True
            t_why.append(f"{label}: no")
        else:
            t_why.append(f"{label}: unknown")
    add("technology", t_pts, 15, "; ".join(t_why), t_known)

    # G. Buying trigger (20) with freshness banding
    if triggers:
        fresh = trigger_freshness(triggers[0].get("date"), now_iso())
        strength = triggers[0]["strength"]
        if strength == "strong" and fresh in ("VERY_STRONG", "STRONG", "UNKNOWN"):
            g = 20.0
        elif strength == "strong" or fresh == "MEDIUM":
            g = 10.0
        else:
            g = 5.0
        add("buying_trigger", g, 20, f"[{fresh}] {triggers[0]['text'][:100]}", True)
    else:
        add("buying_trigger", 0, 20, "no trigger detected", False)

    total = sum(f.points for f in factors)

    # DQ per spec s11 - only on CONFIRMED negatives. A self-published headcount is ESTIMATED,
    # so it heavily downgrades (0 size points + MANUAL_REVIEW) but never disqualifies.
    emp_state = (prof.get("employee_count") or {}).get("state")
    dq = emp is not None and emp < 20 and emp_state == "CONFIRMED"
    dq_reason = f"under 20 employees ({emp}, confirmed)" if dq else ""
    tiny_estimated = emp is not None and emp < 20 and not dq

    # Contactability (separate 0-100; never affects fit)
    email_tier = best_email_tier([c["tier"] for c in contacts if c["kind"] == "email"])
    phones = [c["value"] for c in contacts if c["kind"] == "phone"]
    has_direct = _any_mobile(phones)
    linkedin = any(c["kind"] == "social" and c["label"] == "linkedin" for c in contacts)
    contactability = ((30 if dm else 0)
                      + (30 if email_tier == "valid" else 15 if email_tier == "role" else 0)
                      + (25 if has_direct else 0) + (10 if linkedin else 0)
                      + (5 if b["phone_e164"] else 0))
    data_confidence = round(100 * known / considered) if considered else 0

    factors.append(ScoreFactor(factor="contactability", group="meta", weight=100,
                               score=contactability / 100, points=contactability,
                               why="verified-DM/email/phone/linkedin/switchboard breakdown"))
    factors.append(ScoreFactor(factor="data_confidence", group="meta", weight=100,
                               score=data_confidence / 100, points=data_confidence,
                               why=f"{known}/{considered} rubric inputs known"))
    factors.append(ScoreFactor(factor="status", group="meta", weight=0, score=0, points=0,
                               why=_account_status(_grade(total, dq), contactability, industry_match,
                                                   rng, dq_reason, tiny_estimated)))

    hooks = [t["text"][:120] for t in triggers[:1]]
    return Score(business_id=b["id"], run_id=run_id, total=round(total, 1),
                 tier=_grade(total, dq), factors=factors, need_hooks=hooks, scored_at=now_iso())


def _any_mobile(phones: list[str]) -> bool:
    """Country-agnostic direct/mobile detection via phonenumbers metadata."""
    import phonenumbers
    for p in phones:
        try:
            n = phonenumbers.parse(p, None)
            if phonenumbers.number_type(n) in (phonenumbers.PhoneNumberType.MOBILE,
                                               phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE):
                return True
        except phonenumbers.NumberParseException:
            continue
    return False


def _account_status(grade: str, contactability: int, industry_match: bool, emp_range: str,
                    dq_reason: str, tiny_estimated: bool = False) -> str:
    if grade == "DQ":
        return f"DISQUALIFIED: {dq_reason}"
    if tiny_estimated or not industry_match or emp_range in ("20-49", ">500") or grade == "C":
        return "MANUAL_REVIEW"
    if grade in ("A", "B") and contactability >= 60:
        return "READY_FOR_OUTREACH"
    return "NEW"


def score_run_account_fit(conn: sqlite3.Connection, icp: ICP, run_id: str) -> dict:
    counts = {"scored": 0, "tier_a": 0, "tier_b": 0, "tier_c": 0, "tier_d": 0, "dq": 0}
    for b in db.all_businesses(conn):
        s = score_account_fit(conn, icp, run_id, b)
        db.save_score(conn, s)
        counts["scored"] += 1
        counts[{"A": "tier_a", "B": "tier_b", "C": "tier_c", "D": "tier_d", "DQ": "dq"}[s.tier]] += 1
    conn.commit()
    return counts
