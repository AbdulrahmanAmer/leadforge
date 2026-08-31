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
        if b["address_city"] or b["address_region"]:
            return 1.0, f"located in {b['address_city'] or b['address_region']}"
        return 0.5, "location present but unstructured"

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

    def _negatives(self, b, ctx) -> list[tuple[str, float]]:
        neg = self.rubric.get("negatives", {})
        out = []
        name = b["name_norm"] or ""
        if "franchise" in name or "group" in name:
            out.append(("franchise_or_chain", neg.get("franchise_or_chain", -25)))
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
    scorer = Scorer(conn, icp, run_id)
    counts = {"scored": 0, "tier_a": 0, "tier_b": 0, "tier_c": 0, "dq": 0}
    for b in db.all_businesses(conn):
        s = scorer.score_business(b)
        db.save_score(conn, s)
        counts["scored"] += 1
        counts[{"A": "tier_a", "B": "tier_b", "C": "tier_c", "DQ": "dq"}[s.tier]] += 1
    conn.commit()
    return counts
