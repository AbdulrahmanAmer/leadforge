"""Intake / ICP compiler (U2.2) — docs/03 §4, question bank in skills/.../references/icp-guide.md.

Reads the agent-written answers.yaml, validates it hard (models.py does the field validation), applies
sensible defaults, and writes a canonical icp.yaml whose hash is deterministic (re-compile -> same hash).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from leadforge.models import ICP, Answers
from leadforge.util import InputError


def compile_icp(answers_path: Path, out_path: Path) -> tuple[ICP, list[str]]:
    if not answers_path.is_file():
        raise InputError(f"answers file not found: {answers_path}")
    try:
        raw = yaml.safe_load(answers_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise InputError(f"answers.yaml is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise InputError("answers.yaml must be a YAML mapping (see icp-guide.md)")

    errors = _preflight(raw)
    if errors:
        raise InputError("answers.yaml problems:\n  - " + "\n  - ".join(errors))

    try:
        answers = Answers.model_validate(raw)
    except Exception as e:  # noqa: BLE001 — surface pydantic errors as clean field messages
        raise InputError(_fmt_validation(e)) from e

    icp = ICP.model_validate(answers.model_dump())
    _activate_company_mode(icp)

    # Hooks are gated on qualify.soft — an ICP that lists none gets an empty "Likely Need" column.
    # Seed offer-agnostic defaults so every campaign produces hooks unless the user opts out.
    _SOFT_DEFAULTS = ["website_missing", "stale_site", "few_reviews",
                      "weak_social_presence", "no_video_presence"]
    seeded = [s for s in _SOFT_DEFAULTS if s not in icp.qualify.soft]
    if len(icp.qualify.soft) < 3 and seeded:
        icp.qualify.soft = list(icp.qualify.soft) + seeded
    else:
        seeded = []

    warnings = _warnings(icp)
    if seeded:
        warnings.append(f"seeded default need signals: {', '.join(seeded)} "
                        "(list 3+ soft qualifiers in answers.yaml to override)")

    # deterministic serialization -> stable hash
    out_path.write_text(
        yaml.safe_dump(icp.model_dump(mode="json"), sort_keys=True, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    return icp, warnings


def _preflight(raw: dict) -> list[str]:
    errs = []
    if not raw.get("campaign"):
        errs.append("missing 'campaign' (a kebab-case slug you invent)")
    if not raw.get("offer", {}).get("what"):
        errs.append("missing 'offer.what' (one sentence: what is being sold)")
    target = raw.get("target", {})
    is_company = target.get("mode") == "company"
    if not is_company and not target.get("categories"):
        errs.append("missing 'target.categories' (1-5 business types, Maps-style phrasing)")
    if is_company and not target.get("sic_codes"):
        errs.append("missing 'target.sic_codes' (>=1 five-digit UK SIC code — see icp-guide.md "
                    "company-mode section); target.categories may be empty in company mode")
    geo = target.get("geography", {})
    if not geo.get("areas") and not geo.get("bbox"):
        errs.append("missing 'target.geography.areas' (or a bbox)")
    if is_company and not geo.get("areas"):
        errs.append("target.mode 'company' requires 'target.geography.areas' — Companies House "
                    "advanced-search needs a location string, a bbox alone is not usable")
    if not geo.get("country"):
        errs.append(
            "missing 'target.geography.country' — ASK THE USER which country (ISO2, e.g. US/GB/EG). "
            "A bare city name is ambiguous worldwide and would scrape the wrong place"
        )
    return errs


_VAGUE_HINT = ("downtown", "north", "south", "east", "west", "central", "area", "region", "metro")


def _warnings(icp: ICP) -> list[str]:
    w = []
    geo = icp.target.geography
    for area in geo.areas:
        # a bare single-token city inside a big federal country is the classic garbage-results trap
        if geo.country in ("US", "CA", "AU", "BR", "IN", "MX") and "," not in area:
            w.append(f"'{area}' has no state/region — add one (e.g. '{area}, TX') to avoid same-name mixups")
        if any(t in area.casefold() for t in _VAGUE_HINT) and "," not in area:
            w.append(f"'{area}' is vague; a named city/suburb geocodes far more reliably")
    if icp.caps.max_leads > 500:
        w.append(f"max_leads={icp.caps.max_leads} is large; expect long runtime + more captcha risk")
    if len(icp.target.categories) > 3:
        w.append(f"{len(icp.target.categories)} categories multiplies queries; consider splitting campaigns")
    if not icp.decision_maker.titles_priority:
        w.append("no DM titles set; DM labeling will be weaker")
    if geo.country in ("GB", "UK"):
        from leadforge.config import load_config
        if not load_config(".").registry.companies_house_key:
            w.append("UK campaign: a free Companies House key adds registry-verified directors — "
                     "get one at https://developer.company-information.service.gov.uk then run: "
                     "leadforge config set registry.companies_house_key <KEY>")
    if icp.target.mode == "company":
        w.append("company mode: set discovery.providers: [companies_house] in leadforge.yaml so "
                 "discovery uses the Companies House advanced-search provider (not gosom/Maps)")
        excluded = [c for c in icp.target.sic_codes if c == "82200"]
        if excluded:
            w.append("target.sic_codes includes 82200 (call centres) — owner decision 7 excludes it "
                     "from GAINLEV_ICP_SIC by default; the provider will drop any company matched only "
                     "via an excluded SIC code")
    return w


def _activate_company_mode(icp: ICP) -> None:
    """Company mode needs two side effects that happen nowhere else on a resumed run (one that skips
    straight to scoring and never calls grid.build_plan, the only other place a company-mode ICP would
    otherwise get its provider/profile wired up):

    1. Importing leadforge.company registers the "company" scoring profile via score.register_profile
       (see company.py) and, as a side effect of that import, the companies_house discovery provider
       registers itself with providers.base (its @register decorator runs on import).
    2. An ICP left on the default scoring profile is switched to "company" here — company-mode
       campaigns almost never set scoring.profile explicitly, and the default rubric (Maps
       category/geography fit) does not apply to a Companies House business at all. An ICP that
       explicitly requests a different profile (e.g. a future company-mode variant) is left alone.
    """
    if icp.target.mode != "company":
        return
    import leadforge.company  # noqa: F401
    if icp.scoring.profile == "default":
        icp.scoring.profile = "company"


def _fmt_validation(e: Exception) -> str:
    lines = ["answers.yaml failed validation:"]
    errors = getattr(e, "errors", None)
    if callable(errors):
        for err in e.errors():  # type: ignore[attr-defined]
            loc = ".".join(str(x) for x in err.get("loc", ()))
            lines.append(f"  - {loc}: {err.get('msg')}")
    else:
        lines.append(f"  - {e}")
    return "\n".join(lines)


def load_icp(icp_path: Path) -> ICP:
    if not icp_path.is_file():
        raise InputError(f"icp file not found: {icp_path} (run `leadforge intake` first)")
    raw = yaml.safe_load(icp_path.read_text(encoding="utf-8")) or {}
    try:
        icp = ICP.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        raise InputError(_fmt_validation(e)) from e
    _activate_company_mode(icp)
    return icp
