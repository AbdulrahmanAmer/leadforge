"""Canonical schema (U1.1) — the ONLY shapes that cross module boundaries (docs/03).

Everything is pydantic v2. Providers emit RawListing; normalize.py turns those into Business;
enrichment attaches Contact/Person/Evidence; score.py emits Score; export reads the lot from SQLite.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------- intake / ICP
HARD_QUALIFIERS = {"franchise_or_chain", "no_phone", "no_website_hard", "closed_or_unverified"}
HARD_PREFIXES = ("competitor:", "existing_client:")
SOFT_QUALIFIERS = {
    "website_missing", "website_no_ssl", "stale_site", "low_rating_high_volume",
    "few_reviews", "weak_social_presence", "phone_only_booking", "hiring",
    # social/video presence signals (U4.8, providers/social.py; on by default, degrade silently).
    # no_social_presence = the site links no profile at all; weak_social_presence = crawler found none.
    # active_social is a NEGATIVE need signal (they post recently) — no hook template on purpose.
    "stale_social", "no_social_presence", "no_video_presence", "active_social",
}


class Offer(BaseModel):
    what: str
    value_prop: str = ""
    sender: str = ""


class Geography(BaseModel):
    """Where to hunt. `country` is REQUIRED: a bare city name is ambiguous worldwide (there are 20+
    'Springfield's, a Houston in the US and in the UK), and geocoding the wrong one silently poisons an
    entire run. It also sets the default phone region for E.164 normalization."""

    areas: list[str] = Field(default_factory=list)
    country: str  # ISO 3166-1 alpha-2, e.g. US, GB, EG, AE
    bbox: list[float] | None = None  # [minLng, minLat, maxLng, maxLat]
    grid: Literal["auto", "off"] = "auto"

    @field_validator("country")
    @classmethod
    def _iso2(cls, v: str) -> str:
        code = str(v).strip().upper()
        if len(code) != 2 or not code.isalpha():
            raise ValueError(f"country must be an ISO 3166-1 alpha-2 code like US/GB/EG (got '{v}')")
        try:
            import phonenumbers

            if code not in phonenumbers.SUPPORTED_REGIONS:
                raise ValueError(f"'{code}' is not a recognized country code")
        except ImportError:  # pragma: no cover — phonenumbers is a core dep
            pass
        return code

    @field_validator("areas")
    @classmethod
    def _areas_specific(cls, v: list[str]) -> list[str]:
        cleaned = [a.strip() for a in v if a.strip()]
        for a in cleaned:
            if len(a) < 3:
                raise ValueError(f"area '{a}' is too vague to geocode — use a city, or 'City, State'")
        return cleaned

    @field_validator("bbox")
    @classmethod
    def _bbox_len(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != 4:
            raise ValueError("bbox must be [minLng, minLat, maxLng, maxLat]")
        return v


class SizeBand(BaseModel):
    min_reviews: int | None = None
    max_reviews: int | None = None


class Target(BaseModel):
    # mode is declared before categories/sic_codes so their field_validators can see it via
    # ValidationInfo.data (pydantic v2 only exposes already-validated, earlier-declared fields).
    mode: Literal["local_business", "company"] = "local_business"
    categories: list[str]
    geography: Geography
    size: SizeBand = Field(default_factory=SizeBand)
    # company mode only (Unit H, docs/09 Wave 2 H): Companies House advanced-search targeting.
    sic_codes: list[str] = Field(default_factory=list)
    incorporated_from: str | None = None
    incorporated_to: str | None = None

    @field_validator("categories")
    @classmethod
    def _cats(cls, v: list[str], info) -> list[str]:
        v = [c.strip() for c in v if c.strip()]
        # company mode targets by SIC code, not Maps-style category phrasing — categories may be empty.
        if (info.data or {}).get("mode") == "company":
            return v
        if not 1 <= len(v) <= 20:
            raise ValueError("1-20 categories required")
        return v

    @field_validator("sic_codes")
    @classmethod
    def _sic_codes(cls, v: list[str]) -> list[str]:
        cleaned = [str(c).strip() for c in v if str(c).strip()]
        for c in cleaned:
            if not (len(c) == 5 and c.isdigit()):
                raise ValueError(f"sic code '{c}' must be a 5-digit UK SIC code (e.g. '62012'), got '{c}'")
        return cleaned

    @model_validator(mode="after")
    def _company_mode_requirements(self) -> Target:
        if self.mode == "company":
            if not self.sic_codes:
                raise ValueError(
                    "target.mode 'company' requires >= 1 five-digit target.sic_codes entry "
                    "(see icp-guide.md company-mode section)"
                )
            if not self.geography.areas:
                raise ValueError(
                    "target.mode 'company' requires >= 1 target.geography.areas entry — Companies House "
                    "advanced-search needs a location string, a bbox alone is not usable"
                )
        return self


class Qualify(BaseModel):
    hard: list[str] = Field(default_factory=list)
    soft: list[str] = Field(default_factory=list)

    @field_validator("hard")
    @classmethod
    def _hard(cls, v: list[str]) -> list[str]:
        for q in v:
            if q not in HARD_QUALIFIERS and not q.startswith(HARD_PREFIXES):
                raise ValueError(f"unknown hard qualifier '{q}' (see icp-guide vocabulary)")
        return v

    @field_validator("soft")
    @classmethod
    def _soft(cls, v: list[str]) -> list[str]:
        unknown = [q for q in v if q not in SOFT_QUALIFIERS]
        if unknown:
            raise ValueError(f"unknown soft qualifier(s) {unknown} (see icp-guide vocabulary)")
        return v


class DecisionMaker(BaseModel):
    titles_priority: list[str] = Field(default_factory=lambda: ["Owner", "General Manager", "Manager"])


class Scoring(BaseModel):
    weights_override: dict[str, float] = Field(default_factory=dict)
    profile: str = "default"  # default | account_fit (WE SCORE 0-100 rubric + contactability + data confidence)


class Caps(BaseModel):
    max_leads: int = 200
    max_sites: int = 300
    max_tiles: int = 60


class Compliance(BaseModel):
    region_profile: Literal["us", "uk", "eu"] = "us"


class ICP(BaseModel):
    version: int = 1
    campaign: str
    offer: Offer
    target: Target
    qualify: Qualify = Field(default_factory=Qualify)
    decision_maker: DecisionMaker = Field(default_factory=DecisionMaker)
    scoring: Scoring = Field(default_factory=Scoring)
    caps: Caps = Field(default_factory=Caps)
    compliance: Compliance = Field(default_factory=Compliance)
    notes: str = ""

    @field_validator("campaign")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not v or " " in v:
            raise ValueError("campaign must be a kebab-case slug")
        return v.lower()

    def icp_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(blob.encode()).hexdigest()[:12]


class Answers(ICP):
    """The interview file the agent writes — same shape as ICP; intake normalizes + re-validates."""


# ---------------------------------------------------------------------------- pipeline data
class RawListing(BaseModel):
    provider: str
    fetched_at: str
    query_id: int | None = None
    data: dict[str, Any]  # provider-native fields; FIELD_MAP in the provider documents them


class Business(BaseModel):
    id: str = ""
    place_id: str | None = None
    cid: str | None = None
    name: str
    name_norm: str = ""
    category: str | None = None
    categories: list[str] = Field(default_factory=list)
    website: str | None = None
    domain: str | None = None
    phone_e164: str | None = None
    phone_raw: str | None = None
    address_full: str | None = None
    address_street: str | None = None
    address_city: str | None = None
    address_region: str | None = None
    address_postal: str | None = None
    address_country: str | None = None
    lat: float | None = None
    lng: float | None = None
    rating: float | None = None
    review_count: int | None = None
    hours: dict[str, Any] | None = None
    maps_url: str | None = None
    source: str = "unknown"
    first_run_id: str | None = None
    last_seen_at: str = ""
    enrich: dict[str, Any] = Field(default_factory=dict)  # crawl status, signals, socials
    dedupe_key: str = ""


class Contact(BaseModel):
    business_id: str
    kind: Literal["email", "phone", "social"]
    value: str
    label: str = "unknown"       # role|personal|unknown ; or social network name
    # 'inferred' = derived from the domain's demonstrated naming convention, never observed
    # published (v0.2.0). Exported in its own column; never counted as a found email.
    tier: Literal["valid", "risky", "role", "catch_all", "inferred", "unknown", "invalid"] = "unknown"
    verified_at: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    # v0.3: own_domain | freemail_linked | freemail_unlinked | foreign (see extract.classify_email_affinity)
    affinity: str = ""


class Person(BaseModel):
    business_id: str
    name: str
    title: str = ""
    source_url: str = ""
    snippet: str = ""
    dm_confidence: float = 0.0
    is_dm: int = 0               # 1 accepted, 0 unlabeled, -1 rejected
    labeled_by: str = "heuristic"  # heuristic|agent|registry|gbp
    labeled_at: str = ""
    origin: str = ""               # v0.3: where the candidate came from (heuristic|registry|gbp); survives agent labeling

    @field_validator("snippet")
    @classmethod
    def _cap(cls, v: str) -> str:
        return v[:300]


class Evidence(BaseModel):
    business_id: str
    ref_table: str = "businesses"
    ref_id: int | None = None
    fact: str
    url: str = ""
    snippet: str = ""
    observed_at: str = ""


class ScoreFactor(BaseModel):
    factor: str
    group: str
    weight: float
    score: float  # 0..1
    points: float
    why: str


class Score(BaseModel):
    business_id: str
    run_id: str
    total: float
    tier: Literal["A", "B", "C", "D", "DQ"]
    factors: list[ScoreFactor]
    need_hooks: list[str] = Field(default_factory=list)
    scored_at: str = ""
