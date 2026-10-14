"""Conservative jurisdiction policy metadata for public enrichment.

These profiles are operational guardrails, not legal advice. Unknown jurisdictions fail
closed and require operator review rather than inheriting a permissive default.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JurisdictionPolicy:
    code: str
    public_business_data_allowed: bool
    personal_email_requires_review: bool
    inferred_personal_email_allowed: bool
    lawful_basis_review: str
    permitted_sources: tuple[str, ...]


_PUBLIC_SOURCES = ("public_site", "public_registry", "trade_license", "gbp")
_POLICIES = {
    "US": JurisdictionPolicy("US", True, True, False, "operator_review_required", _PUBLIC_SOURCES),
    "GB": JurisdictionPolicy("GB", True, True, False, "legitimate_interest_review_required", _PUBLIC_SOURCES),
    "EU": JurisdictionPolicy("EU", True, True, False, "legitimate_interest_review_required", _PUBLIC_SOURCES),
}


def policy_for(country: str | None) -> JurisdictionPolicy:
    """Return a conservative policy; unknown/blank country is manual-review only."""
    code = (country or "").strip().upper()
    if code in _POLICIES:
        return _POLICIES[code]
    return JurisdictionPolicy(code or "UNKNOWN", False, True, False, "manual_review_required", ())
