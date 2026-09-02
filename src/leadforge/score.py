"""Scoring & qualification engine (U5.1-5.2, v0.3 U9.D) — docs/01 §5, docs/09 "D — Scoring and export
truth", rubric leadforge/data/scoring.default.yaml, category aliases leadforge/data/category_aliases.yaml.

Pure, deterministic, unit-tested. Each fit factor returns (score 0..1, why-string); points = score*weight.
Negatives apply a capped penalty; hard qualifiers -> DQ. Need-hooks synthesized from signals + offer.

v0.3 split: `total`/tier come ONLY from `fit` factors now. Reachability (DM/email/phone/registry) is
graded separately as `contactability` (0-100, meta factor, never affects total) plus a `status` meta
factor (READY/CALL_ONLY/RESEARCH/DQ) that blends tier + contactability + email eligibility. A profile
registry (`register_profile`) lets other units add scoring profiles without touching this dispatch.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from importlib.resources import files as _pkg_files

import yaml

from leadforge import compliance, db
from leadforge.config import Config
from leadforge.models import ICP, Score, ScoreFactor
from leadforge.util import now_iso

# Policy inputs that live in Config, not ICP (freemail_policy, require_corporate). score_run's call
# sites (cli.py, pipeline.py) pass no Config, so `status`/eligibility here use the built-in defaults —
# the same defaults export.py falls back to when it isn't handed a live `cfg` either (see export.py).
# A campaign that overrides these in leadforge.yaml will see it reflected at export time, not here.
_DEFAULT_POLICY = Config()


def _word_in(needle: str, hay: str) -> bool:
    """'group' must not match 'Grouper', 'inc' must not match 'Vincent' — whole words only."""
    return re.search(rf"\b{re.escape(needle)}\b", hay) is not None


def load_rubric(icp: ICP) -> dict:
    # packaged resource, not a repo-relative path: `pip install .` (non-editable) has no repo root
    rubric = yaml.safe_load((_pkg_files("leadforge") / "data" / "scoring.default.yaml")
                            .read_text(encoding="utf-8"))
    for factor, weight in icp.scoring.weights_override.items():
        if factor in rubric["factors"]:
            rubric["factors"][factor]["weight"] = weight
    return rubric


def load_category_aliases() -> dict:
    """vertical -> {"exact": [...], "adjacent": [...]} — packaged resource, loaded once per Scorer."""
    doc = yaml.safe_load((_pkg_files("leadforge") / "data" / "category_aliases.yaml")
                         .read_text(encoding="utf-8"))
    return (doc or {}).get("verticals", {})


# words that must not drive the token-overlap fallback in _f_industry_match: generic enough that they
# fuzzy-match unrelated categories ("Community centre" must not read as adjacent to "MOT centre").
_CATEGORY_STOPWORDS = {"shop", "service", "services", "centre", "center", "station"}


def _category_tokens(cat: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", cat.casefold()) if len(t) >= 4 and t not in _CATEGORY_STOPWORDS}


class Scorer:
    def __init__(self, conn: sqlite3.Connection, icp: ICP, run_id: str):
        self.conn = conn
        self.icp = icp
        self.run_id = run_id
        self.rubric = load_rubric(icp)
        self.wanted_cats = [c.casefold() for c in icp.target.categories]
        self.soft = set(icp.qualify.soft)
        self.category_aliases = load_category_aliases()
        self._icp_verticals = self._match_verticals(self.wanted_cats)
        self.chain_map = db.chain_map(conn)

    def _match_verticals(self, wanted_cats: list[str]) -> list[str]:
        """Which alias-table verticals the ICP's own categories belong to (exact OR adjacent) — the
        business's category is then graded against those verticals' buckets."""
        verts = []
        for vert, buckets in self.category_aliases.items():
            alias_set = {a.casefold() for a in (buckets.get("exact") or [])} | \
                        {a.casefold() for a in (buckets.get("adjacent") or [])}
            if any(w in alias_set for w in wanted_cats):
                verts.append(vert)
        return verts

    # ---- individual factors ---------------------------------------------------------
    # EVERY factor function takes the SAME (self, business_row, ctx) signature and returns
    # (score 0..1, why-string), so `_FACTOR_FNS` can dispatch them uniformly from the rubric config.
    # Some factors legitimately ignore `b` or `ctx` — do NOT "tidy" those parameters away or the
    # table dispatch below breaks. (ruff ARG002 is intentionally not enabled for this reason.)
    def _f_industry_match(self, b, ctx) -> tuple[float, str]:
        """1.0 exact ICP-literal or same-vertical-exact-bucket match; 0.6 same-vertical-adjacent-bucket
        or a generic shared significant token; 0.1 otherwise. Vertical buckets: data/category_aliases.yaml."""
        cats = [c.casefold() for c in (json.loads(b["categories_json"]) or [])]
        primary = (b["category"] or "").casefold()
        all_biz_cats = set(cats) | ({primary} if primary else set())
        if primary and any(w == primary for w in self.wanted_cats):
            return 1.0, f"category '{b['category']}' is an exact ICP match"
        if all_biz_cats & set(self.wanted_cats):
            return 1.0, f"category '{b['category']}' is an exact ICP match"
        for vert in self._icp_verticals:
            exact_set = {a.casefold() for a in (self.category_aliases[vert].get("exact") or [])}
            if all_biz_cats & exact_set:
                return 1.0, f"category '{b['category']}' matches the {vert.replace('_', ' ')} vertical"
        for vert in self._icp_verticals:
            adj_set = {a.casefold() for a in (self.category_aliases[vert].get("adjacent") or [])}
            if all_biz_cats & adj_set:
                return 0.6, f"category '{b['category']}' is adjacent to the {vert.replace('_', ' ')} vertical"
        wanted_tokens = {t for c in self.wanted_cats for t in _category_tokens(c)}
        for c in all_biz_cats:
            if wanted_tokens & _category_tokens(c):
                return 0.6, "category adjacent to ICP targets (shared token)"
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
        chain_hits = [w for w in ("franchise", "group", "inc", "corporation") if _word_in(w, name)]
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
        if best == "inferred":  # a guess is worth something, but less than a phone you can dial
            return 0.2, "inferred email only (unconfirmed)"
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
            "people": people, "dm": dm, "contacts": contacts, "best_email_tier": best_email_tier(email_tiers),
            "need_hits": need_hits, "enrich": enrich, "registry": registry,
        }

    @staticmethod
    def _has_signal(enrich: dict, key: str) -> bool:
        """A crawl-derived hook fires only on real evidence: the site was actually crawled AND this
        specific signal key was computed — not just missing/defaulted-falsy (v0.3: the live campaign
        stamped `crawled_at` on 115 'phantom' crawls with zero pages, which used to fire hooks anyway)."""
        return bool(enrich.get("crawled_at")) and key in (enrich.get("signals") or {})

    def _need_hits(self, b, enrich: dict) -> list[str]:
        hits = []
        signals = enrich.get("signals") or {}
        if "website_missing" in self.soft and not b["website"]:
            hits.append("website_missing")
        if ("website_no_ssl" in self.soft and b["website"] and self._has_signal(enrich, "https")
                and signals.get("https") is False):
            hits.append("website_no_ssl")
        if "stale_site" in self.soft and self._has_signal(enrich, "stale_site") and signals.get("stale_site"):
            hits.append("stale_site")
        if "low_rating_high_volume" in self.soft and (b["rating"] or 5) <= 3.9 and (b["review_count"] or 0) >= 50:
            hits.append("low_rating_high_volume")
        if "few_reviews" in self.soft and (b["review_count"] or 0) < 10:
            hits.append("few_reviews")
        if "weak_social_presence" in self.soft and "socials" in enrich and enrich.get("crawled_at") \
                and not enrich.get("socials"):
            hits.append("weak_social_presence")
        if "phone_only_booking" in self.soft and self._has_signal(enrich, "booking_hint") \
                and not signals.get("booking_hint"):
            hits.append("phone_only_booking")
        if "hiring" in self.soft and self._has_signal(enrich, "careers") and signals.get("careers"):
            hits.append("hiring")
        # social/video presence signals (set by the optional Agent-Reach unit U4.8; absent until enabled)
        for sig in ("stale_social", "no_social_presence", "no_video_presence"):
            if sig in self.soft and self._has_signal(enrich, sig) and signals.get(sig):
                hits.append(sig)
        return hits

    def _hard_dq(self, b, ctx) -> str | None:
        name = b["name_norm"] or ""
        for q in self.icp.qualify.hard:
            if q == "no_phone" and not b["phone_e164"]:
                return "no_phone"
            if q == "franchise_or_chain" and any(_word_in(w, name) for w in ("franchise", "group")):
                return "franchise_or_chain"
            if q == "no_website_hard" and not b["website"]:
                return "no_website_hard"
            if q.startswith(("competitor:", "existing_client:")) and _word_in(q.split(":", 1)[1].casefold(), name):
                return q
        return None

    _COUNTRY_ALIASES = {"uk": "gb", "united kingdom": "gb", "united states": "us", "usa": "us"}

    def _negatives(self, b, ctx) -> list[tuple[str, float, str]]:
        neg = self.rubric.get("negatives", {})
        out: list[tuple[str, float, str]] = []
        name = b["name_norm"] or ""
        # the franchise/group NAME penalty is a local-business-mode heuristic — a chain's own company-
        # mode ICP (Target.mode == "company", Wave 2) must not penalize its own kind of name.
        mode = getattr(self.icp.target, "mode", "local_business") or "local_business"
        if mode == "local_business" and (_word_in("franchise", name) or _word_in("group", name)):
            out.append(("franchise_or_chain", neg.get("franchise_or_chain", -25),
                       "name suggests a franchise/chain"))
        # out_of_area only on unambiguous evidence: the listing's own country differs from the
        # campaign's. Same-country suburb spillover is graded softly by geography_match instead.
        want = (self.icp.target.geography.country or "").strip().casefold()
        got = (b["address_country"] or "").strip().casefold()
        want, got = self._COUNTRY_ALIASES.get(want, want), self._COUNTRY_ALIASES.get(got, got)
        if want and got and want != got:
            out.append(("out_of_area", neg.get("out_of_area", -20),
                       f"listing country '{b['address_country']}' differs from the campaign's"))
        if b["id"] in self.chain_map:
            out.append(("chain_member", neg.get("chain_member", -15),
                       f"shares {self.chain_map[b['id']].split(':', 1)[0]} with another business in this DB"))
        return out

    def _hooks(self, b, ctx) -> list[str]:
        templates = self.rubric.get("hooks", {})
        offer = self.icp.offer.what
        signals = ctx["enrich"].get("signals") or {}
        out = []
        for sig in ctx["need_hits"]:
            tmpl = templates.get(sig)
            if not tmpl:
                continue
            text = tmpl.replace("{offer}", offer)
            if "{year}" in text:
                text = text.replace("{year}", str(signals.get("copyright_year", "unknown")))
            out.append(text)
        return out

    # ---- contactability + status (meta factors; never affect total/tier) ------------
    def _contactability_and_status(self, b, ctx, tier: str, dq: str | None) -> tuple[int, str, str]:
        """-> (contactability 0-100, contactability why, status). Weights: docs/09 §D / the
        `contactability_weights` block in scoring.default.yaml (read by humans, not by this code —
        keeping one source of truth in code avoids the two silently drifting)."""
        from leadforge.enrich.validate import rank_email_contacts

        contacts = ctx["contacts"]
        # Affinity MUST be backfilled BEFORE ranking, not after: every pre-v0.3 contact row stores
        # affinity '' (100% of the live campaign DB), so ranking on raw rows falls through to tier
        # order alone and a stranger's freemail 'valid' address can outrank the business's own
        # 'role' mailbox — which was then exported and labeled as the (wrong) winner. Rank and grade
        # the SAME filled list compliance.email_eligibility below also sees, so contactability,
        # Lawful Basis and the exported Email column can never disagree about which address won.
        contacts_filled = fill_email_affinity(contacts, b["domain"])
        dm = ctx["dm"]
        pts = 0
        why: list[str] = []
        if dm and dm["is_dm"] == 1:
            pts += 30
            why.append("DM identified")
        ranked = rank_email_contacts(contacts_filled)
        best = ranked[0] if ranked else None
        email_tier = (best["tier"] if best else "") or ""
        affinity = (best["affinity"] if best else "") or ""
        backfilled = bool(best and best.get("_affinity_backfilled"))
        if affinity == "own_domain" and email_tier == "valid":
            pts += 30
            why.append("own-domain valid email")
        elif affinity == "own_domain" and email_tier == "role":
            pts += 22
            why.append("own-domain role email")
        elif affinity == "freemail_linked" and email_tier == "valid":
            pts += 20
            why.append("linked-freemail valid email" + (" (linkage not checked)" if backfilled else ""))
        elif email_tier == "inferred":
            pts += 8
            why.append("inferred email only")
        elif email_tier == "risky":
            why.append("risky email (0 pts)")
        phone_ok = phone_is_validated(b["phone_e164"])
        if phone_ok:
            pts += 25
            why.append("validated phone")
        entity = compliance.entity_type(b, ctx["people"])
        if entity == compliance.ENTITY_CORPORATE_ACTIVE:
            pts += 5
            why.append("registry-corroborated (active)")
        if (ctx["enrich"].get("signals") or {}).get("phone_confirmed"):
            pts += 5
            why.append("site phone matches Maps phone")
        phones = [c["value"] for c in contacts_filled if c["kind"] == "phone"]
        if _any_mobile(phones):
            pts += 3
            why.append("mobile/direct number")
        pts = min(100, pts)

        eligibility = compliance.email_eligibility(
            b, contacts_filled, entity, self.icp.compliance.region_profile,
            freemail_policy=_DEFAULT_POLICY.validation.freemail_policy,
            require_corporate=_DEFAULT_POLICY.outreach.require_corporate,
            suppressed=False, site_dead=False,
        )
        if dq:
            status = "DQ"
        elif tier in ("A", "B") and pts >= 50:
            status = "READY"
        elif phone_ok and not eligibility.get("eligible"):
            status = "CALL_ONLY"
        else:
            status = "RESEARCH"
        return pts, ("; ".join(why) or "no contactability signals"), status

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
        for label, pts, why in self._negatives(b, ctx):
            neg_total += pts
            factors.append(ScoreFactor(factor=f"negative:{label}", group="negative", weight=pts,
                                       score=1.0, points=pts, why=why))
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

        contact_pts, contact_why, status = self._contactability_and_status(b, ctx, tier, dq)
        factors.append(ScoreFactor(factor="contactability", group="meta", weight=100,
                                   score=contact_pts / 100, points=contact_pts, why=contact_why))
        factors.append(ScoreFactor(factor="status", group="meta", weight=0, score=0.0, points=0.0,
                                   why=status))

        return Score(business_id=b["id"], run_id=self.run_id, total=round(total, 1), tier=tier,
                     factors=factors, need_hooks=self._hooks(b, ctx), scored_at=now_iso())


def phone_is_validated(phone_e164: str | None) -> bool:
    """Next Action / contactability's 'validated phone': the stored E.164 string actually parses as a
    real number — a raw unparsed Maps string in phone_raw does not count (docs/09 §D)."""
    if not phone_e164:
        return False
    try:
        import phonenumbers
        return phonenumbers.is_valid_number(phonenumbers.parse(phone_e164, None))
    except Exception:  # noqa: BLE001 — malformed stored value -> not validated, never a crash
        return False


def fallback_email_affinity(email: str | None, business_domain: str | None) -> str:
    """Legacy pre-v0.3 contact rows carry no `affinity` column value (added this release) — derive the
    same coarse class compliance.py's own `_infer_affinity` fallback uses, so contactability/export
    never disagree with Lawful Basis on an old row. Real-data proof on the live UK campaign DB caught
    this: a role@own-domain address scored 0 contactability points and exported 'Email Confidence:
    none' while Lawful Basis correctly read 'b2b_legitimate_interest' for the SAME address."""
    if not email:
        return ""
    from leadforge.enrich.extract import FREEMAIL_DOMAINS

    dom = email.rsplit("@", 1)[-1].casefold()
    biz = (business_domain or "").casefold()
    if biz and (dom == biz or dom.endswith("." + biz)):
        return "own_domain"
    return "freemail_linked" if dom in FREEMAIL_DOMAINS else "foreign"


def fill_email_affinity(contacts: list, business_domain: str | None) -> list[dict]:
    """One contact list with `affinity` backfilled on every email row BEFORE it is ranked or fed to
    `compliance.email_eligibility` — the fix for the blocker the fresh-context review caught: ranking
    on raw rows (100% of the live campaign DB stores affinity '') falls through to tier order alone,
    so a stranger's freemail 'valid' address could outrank the business's own 'role' mailbox, and the
    wrong winner was then exported and labeled as if it were the best one. Every caller that ranks or
    grades email contacts (contactability, eligibility, the exported Email column) MUST rank and grade
    this SAME filled list — never the raw one — so they can never describe two different addresses.
    A dict carries `_affinity_backfilled: True` when its affinity came from the coarse guess rather
    than a stored value, so callers can word confidence honestly instead of overclaiming linkage."""
    out = []
    for c in contacts:
        d = dict(c)
        if d.get("kind") == "email" and not d.get("affinity"):
            d["affinity"] = fallback_email_affinity(d.get("value"), business_domain)
            d["_affinity_backfilled"] = True
        out.append(d)
    return out


# ---- scoring-profile registry (v0.3 U9.D) -----------------------------------------------------
PROFILES: dict[str, Callable[[sqlite3.Connection, ICP, str], dict]] = {}


def register_profile(name: str, fn: Callable[[sqlite3.Connection, ICP, str], dict]) -> None:
    PROFILES[name] = fn


def score_run(conn: sqlite3.Connection, icp: ICP, run_id: str) -> dict:
    """Dispatches on icp.scoring.profile via the PROFILES registry (default: 'default')."""
    fn = PROFILES.get(icp.scoring.profile, score_run_default)
    return fn(conn, icp, run_id)


def score_run_default(conn: sqlite3.Connection, icp: ICP, run_id: str) -> dict:
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
                      + (30 if email_tier == "valid" else 15 if email_tier == "role"
                         else 8 if email_tier == "inferred" else 0)  # a guess counts least
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


register_profile("default", score_run_default)
register_profile("account_fit", score_run_account_fit)
