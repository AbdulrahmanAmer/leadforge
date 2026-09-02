"""Company mode (Unit H, docs/09 Wave 2 H): GainLev's own client pipeline on Companies House.

Two independent things live here:
  1. build_company_plan() — turns an ICP with target.mode=="company" into (sic-code-shard x area)
     PlannedQuery objects for providers.companies_house.CompaniesHouseDiscovery. grid.py's build_plan
     dispatches to it directly.
  2. The company scoring rubric (score_business_company / score_run_company) — industry fit from SIC
     overlap, incorporation-age banding, a new-director trigger, hiring, domain resolution and data
     confidence, with contactability/readiness as separate informational (non-total) meta factors.

score.register_profile("company", score_run_company) wires #2 into leadforge.score.score_run's PROFILES
registry at import time (idempotent — score.py's dict assignment just overwrites the same value on a
second import). intake.py additionally imports this module whenever an ICP's target.mode is "company"
so the profile is registered on every CLI entry point — including a resumed run that skips planning and
goes straight to scoring, not just a freshly-planned one. Importing this module also registers the
companies_house discovery provider as a side effect of providers.base's own builtin-provider list
(providers/base.py's `_import_builtins` already names "companies_house") — no extra step needed here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

from leadforge import db, score
from leadforge.config import Config
from leadforge.grid import PlannedQuery
from leadforge.models import ICP, Score, ScoreFactor
from leadforge.util import now_iso

# ============================================================================ SIC taxonomy
# Curated descriptions for the codes this unit actually plans against (docs/09 Wave 2 H); an
# uncurated code still exports fine, just as "SIC nnnnn" instead of a human label.
SIC_DESCRIPTIONS: dict[str, str] = {
    "62012": "Business and domestic software development",
    "62020": "Information technology consultancy activities",
    "62090": "Other information technology service activities",
    "63110": "Data processing, hosting and related activities",
    "70229": "Management consultancy activities other than financial management",
    "73110": "Advertising agencies",
    "73120": "Media representation services",
    "74100": "Specialised design activities",
    "74909": "Other professional, scientific and technical activities n.e.c.",
    "78109": "Activities of employment placement agencies",
    "78200": "Temporary employment agency activities",
    "82110": "Combined office administrative service activities",
    "82990": "Other business support service activities n.e.c.",
    "82200": "Activities of call centres",
    "68310": "Real estate agencies",
    "68320": "Management of real estate on a fee or contract basis",
    "69201": "Accounting and auditing activities",
    "69102": "Solicitors",
    "71111": "Architectural activities",
    "45200": "Maintenance and repair of motor vehicles",
    # common trades, rounding out the curated set
    "43210": "Electrical installation",
    "43220": "Plumbing, heat and air-conditioning installation",
    "41202": "Construction of commercial buildings",
    "47190": "Other retail sale in non-specialised stores",
    "56101": "Licensed restaurants",
    "96020": "Hairdressing and other beauty treatment",
}


def sic_description(code: str) -> str:
    return SIC_DESCRIPTIONS.get(code, f"SIC {code}")


# GainLev's own ICP: everyone who sells B2B — agencies, SaaS, consultancies, recruiters, wholesalers,
# estate/letting agents, accountants, solicitors — EXCLUDING 82200 (owner decision 7: no call centres).
GAINLEV_ICP_SIC: list[str] = [
    "62012", "62020", "62090", "63110",   # software / IT consultancy / hosting
    "70229", "74909",                     # management & other consultancy
    "73110", "73120",                     # advertising agencies
    "74100",                              # design
    "78109", "78200",                     # recruitment
    "68310", "68320",                     # estate & letting agents
    "69201",                              # accountants
    "69102",                              # solicitors
    "71111",                              # architects
    "82110", "82990",                     # office/business support (excl. 82200)
]


# ============================================================================ planning
_MAX_SIC_PER_SHARD = 5


def build_company_plan(icp: ICP, cfg: Config) -> list[PlannedQuery]:
    """shards = sic-code groups (<=5 codes) x areas. Companies House advanced-search has no map-tile
    concept, so tile is always None; `text` is the provider's own mini query language
    ("sic:<codes> loc:<location>"), parsed back apart by CompaniesHouseDiscovery.fetch()."""
    codes = list(icp.target.sic_codes)
    areas = list(icp.target.geography.areas)
    chunks = [codes[i:i + _MAX_SIC_PER_SHARD] for i in range(0, len(codes), _MAX_SIC_PER_SHARD)]
    queries: list[PlannedQuery] = []
    for area in areas:
        for chunk in chunks:
            queries.append(PlannedQuery(text=f"sic:{','.join(chunk)} loc:{area}",
                                        category=chunk[0] if chunk else "", area=area, tile=None))
    return queries


# ============================================================================ scoring rubric
_APPOINTED_RE = re.compile(r"appointed[:\s]+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_TIER_A_MIN, _TIER_B_MIN = 75.0, 55.0  # matches scoring.default.yaml so the About-sheet legend still applies


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _f_industry_fit(b, ctx) -> tuple[float, float, str, bool]:
    """-> (points, cap, why, known). SIC overlap with the ICP's target list."""
    cap = 25.0
    biz_sics = {str(s) for s in (ctx["registry_profile"].get("sic_codes") or [])}
    if not biz_sics:
        return 0.0, cap, "no SIC codes on file for this company", False
    overlap = biz_sics & set(ctx["icp"].target.sic_codes)
    if overlap:
        return cap, cap, f"SIC overlap with target: {', '.join(sorted(overlap))}", True
    return 0.0, cap, f"SIC codes {sorted(biz_sics)} don't overlap the target list", True


def _f_incorporation_age(b, ctx) -> tuple[float, float, str, bool]:
    cap = 20.0
    inc = _parse_date(ctx["registry_profile"].get("incorporated"))
    if inc is None:
        return 5.0, cap, "incorporation date unknown", False
    years = (date.today() - inc).days / 365.25
    if years < 1:
        return 10.0, cap, f"incorporated {years:.1f}y ago (<1y — very new, unproven)", True
    if years <= 3:
        return 15.0, cap, f"incorporated {years:.1f}y ago (1-3y band)", True
    if years <= 10:
        return 20.0, cap, f"incorporated {years:.1f}y ago (3-10y band, sweet spot)", True
    return 15.0, cap, f"incorporated {years:.1f}y ago (>10y, established)", True


def _f_new_director(b, ctx) -> tuple[float, float, str, bool]:
    """New officer appointed within 12 months, parsed from evidence.registry_officer snippets
    ('<role> — appointed YYYY-MM-DD', providers/registry.py)."""
    cap = 20.0
    cutoff = date.today().replace(year=date.today().year - 1)
    hits = []
    officer_rows = 0
    for ev in ctx["evidence"]:
        if ev["fact"] != "registry_officer":
            continue
        officer_rows += 1
        m = _APPOINTED_RE.search(ev["snippet"] or "")
        d = _parse_date(m.group(1)) if m else None
        if d and d >= cutoff:
            hits.append((d, ev["snippet"]))
    if hits:
        hits.sort(reverse=True)
        return cap, cap, f"new officer within 12 months: {hits[0][1]}", True
    return 0.0, cap, ("no officer appointed within 12 months" if officer_rows
                      else "no registry officer data"), bool(officer_rows)


def _f_hiring(b, ctx) -> tuple[float, float, str, bool]:
    cap = 15.0
    signals = ctx["enrich"].get("signals") or {}
    if signals.get("careers"):
        return cap, cap, "careers/jobs page detected", True
    return 0.0, cap, ("no hiring signal on site" if ctx["enrich"].get("crawled_at")
                      else "site not crawled yet"), bool(ctx["enrich"].get("crawled_at"))


def _f_domain_resolved(b, ctx) -> tuple[float, float, str, bool]:
    cap = 10.0
    if b["domain"]:
        return cap, cap, f"domain resolved: {b['domain']}", True
    return 0.0, cap, "no domain resolved for this company", True


def _f_data_confidence(b, ctx) -> tuple[float, float, str, bool]:
    cap = 15.0
    score, why = 0.3, []
    if ctx["registry_profile"]:
        score += 0.4
        why.append("registry-corroborated")
    if ctx["enrich"].get("crawled_at"):
        score += 0.2
        why.append("site crawled")
    if b["domain"]:
        score += 0.1
        why.append("domain resolved")
    return round(min(1.0, score) * cap, 2), cap, (", ".join(why) or "single-source"), True


_FACTORS = (
    ("industry_fit", _f_industry_fit),
    ("incorporation_age", _f_incorporation_age),
    ("new_director", _f_new_director),
    ("hiring", _f_hiring),
    ("domain_resolved", _f_domain_resolved),
    ("data_confidence", _f_data_confidence),
)


def _contactability(conn: sqlite3.Connection, b) -> tuple[int, str]:
    """Simplified contactability (score.py's account_fit contactability is inline, not an importable
    helper — docs/09 Wave 2 H item 5): DM 30 / own-domain email 30 / phone 25 / registry 15."""
    people = db.people_for(conn, b["id"])
    contacts = db.contacts_for(conn, b["id"])
    dm = any(p["is_dm"] == 1 for p in people)
    has_email = any(c["kind"] == "email" and c["tier"] != "invalid" for c in contacts)
    has_phone = bool(b["phone_e164"]) or any(c["kind"] == "phone" for c in contacts)
    registry = any(p["labeled_by"] == "registry" for p in people)
    pts = (30 if dm else 0) + (30 if has_email else 0) + (25 if has_phone else 0) + (15 if registry else 0)
    parts = [label for hit, label in (
        (dm, "DM identified"), (has_email, "email on file"),
        (has_phone, "phone on file"), (registry, "registry officer(s) on file"),
    ) if hit]
    return pts, "; ".join(parts) or "no direct contact data yet"


def _readiness(grade: str, contactability: int) -> str:
    if grade == "DQ":
        return "DISQUALIFIED"
    if grade in ("A", "B") and contactability >= 55:
        return "READY_FOR_OUTREACH"
    if contactability == 0:
        return "NEEDS_ENRICHMENT"
    return "MANUAL_REVIEW"


def _hooks(icp: ICP, enrich: dict) -> list[str]:
    offer = icp.offer.what
    signals = enrich.get("signals") or {}
    hooks = []
    if signals.get("careers"):
        hooks.append(f"Actively hiring — growing and likely budgeted for: {offer}")
    if not enrich.get("crawled_at"):
        hooks.append(f"No site data crawled yet — early-mover angle for: {offer}")
    return hooks


def score_business_company(conn: sqlite3.Connection, icp: ICP, run_id: str, b) -> Score:
    enrich = json.loads(b["enrich_json"]) if b["enrich_json"] else {}
    ctx = {"icp": icp, "enrich": enrich, "registry_profile": enrich.get("registry_profile") or {},
          "evidence": db.evidence_for(conn, b["id"])}

    factors: list[ScoreFactor] = []
    for name, fn in _FACTORS:
        pts, cap, why, _known = fn(b, ctx)
        factors.append(ScoreFactor(factor=name, group="company_fit", weight=cap,
                                   score=round(pts / cap, 3) if cap else 0.0, points=round(pts, 2), why=why))

    total = max(0.0, min(100.0, round(sum(f.points for f in factors), 1)))

    status = (ctx["registry_profile"].get("company_status") or "").strip().lower()
    dq_reason = f"company_status is '{status}', not active" if status and status != "active" else ""
    grade = "DQ" if dq_reason else ("A" if total >= _TIER_A_MIN else "B" if total >= _TIER_B_MIN else "C")

    contact_pts, contact_why = _contactability(conn, b)
    # named "status" (not "readiness") on purpose: export._row_for()'s default-profile column block
    # reads meta["status"]["why"] straight into the exported Status column for ANY non-account_fit
    # profile, company mode included — matching the name is what makes that column populate at all.
    factors.append(ScoreFactor(factor="contactability", group="meta", weight=100,
                               score=contact_pts / 100, points=contact_pts, why=contact_why))
    factors.append(ScoreFactor(factor="status", group="meta", weight=0, score=0.0, points=0.0,
                               why=dq_reason or _readiness(grade, contact_pts)))

    return Score(business_id=b["id"], run_id=run_id, total=total, tier=grade,
                factors=factors, need_hooks=_hooks(icp, enrich), scored_at=now_iso())


def score_run_company(conn: sqlite3.Connection, icp: ICP, run_id: str, cfg: Config | None = None) -> dict:
    """`cfg` is accepted (unused, like score_run_account_fit) only so this profile's signature matches
    what score.PROFILES's dispatch call in score_run passes to every registered profile."""
    counts = {"scored": 0, "tier_a": 0, "tier_b": 0, "tier_c": 0, "dq": 0}
    for b in db.all_businesses(conn):
        s = score_business_company(conn, icp, run_id, b)
        db.save_score(conn, s)
        counts["scored"] += 1
        counts[{"A": "tier_a", "B": "tier_b", "C": "tier_c", "DQ": "dq"}[s.tier]] += 1
    conn.commit()
    return counts


# ============================================================================ profile registration
# Makes icp.scoring.profile == "company" dispatch through score.score_run to score_run_company, via
# score.py's own PROFILES registry (score.register_profile) — no monkeypatching, idempotent (a second
# import just re-assigns the same dict entry). intake.py imports this module for every mode=="company"
# ICP (both compile_icp and load_icp), so this runs on every real CLI entry point, including a resumed
# run that jumps straight to scoring and never calls grid.build_plan.
score.register_profile("company", score_run_company)
