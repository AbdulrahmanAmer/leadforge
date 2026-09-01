"""Public-registry cross-check providers — ICM unit U4.6 (implemented; Companies House live-proven v0.1.1). Opt-in, free keys (ADR-007).

=== SPEC (implement exactly; acceptance criteria in docs/05 U4.6) ===

class RegistryProvider(Protocol):
    def lookup(self, business_row) -> list[Person]   # labeled_by="registry", dm_confidence=0.0, is_dm=0

1. CompaniesHouseRegistry (UK; cfg.registry.companies_house_key non-empty enables):
   - base https://api.company-information.service.gov.uk, HTTP Basic auth (key as username, blank password)
   - GET /search/companies?q=<business name>&items_per_page=3 -> match by locality/postcode overlap with
     business address; on match GET /company/{number}/officers (active only) ->
     Person(name=officer name title-cased, title=officer_role humanized: "director" -> "Director").
   - Throttle: <= 600 req / 5 min (shared token bucket in the module); on 429 back off 60s once, then skip.
   - Evidence per person: fact="registry_officer", url=company profile URL, snippet=officer role + appointed_on.
2. OpenCorporatesRegistry (cfg.registry.opencorporates_token enables):
   - GET https://api.opencorporates.com/v0.4/companies/search?q=<name>&api_token=..&jurisdiction_code=<us_xx|gb>
   - Same matching + Person/Evidence emission; respect free-tier limits (unpublished — treat 403/429 as disable-for-run).
3. Gate: only called from enrich when cfg has a key AND business country matches the registry's jurisdiction.
   With no keys configured, enrich never imports this module's network paths (silent no-op).
4. Never blocks the run: any exception -> log + return [].
5. tests/test_registry.py: canned JSON fixtures for both APIs, no network; assert Person mapping + evidence facts.

Effect on scoring: presence of labeled_by="registry" person rows feeds the data_confidence factor
("registry_corroborated") — see score.py _f_data_confidence.
"""

from __future__ import annotations

import threading
import time

from leadforge.models import Evidence, Person
from leadforge.util import LOG, natural_name, now_iso

CH_BASE = "https://api.company-information.service.gov.uk"
OC_BASE = "https://api.opencorporates.com/v0.4"


class _TokenBucket:
    """Companies House cap: 600 requests per 5-minute window."""

    def __init__(self, limit: int = 600, window_s: float = 300.0):
        self.limit, self.window_s = limit, window_s
        self._stamps: list[float] = []
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._stamps = [t for t in self._stamps if now - t < self.window_s]
            if len(self._stamps) >= self.limit:
                sleep_for = self.window_s - (now - self._stamps[0])
                time.sleep(max(0.0, sleep_for))
            self._stamps.append(time.monotonic())


_CH_BUCKET = _TokenBucket()


def _locality_overlap(company: dict, biz) -> bool:
    """Accept a search hit only if locality or postcode overlaps — a wrong match is worse than none."""
    addr = company.get("address") or {}
    hay = " ".join(str(v) for v in addr.values() if v).casefold()
    hay = hay or str(company.get("address_snippet") or "").casefold()
    city = (biz["address_city"] or "").strip().casefold()
    postal = (biz["address_postal"] or "").strip().casefold()
    return bool((city and city in hay) or (postal and postal in hay))


def _humanize(role: str) -> str:
    return role.replace("-", " ").replace("_", " ").strip().title()


class CompaniesHouseRegistry:
    name = "companies_house"

    def __init__(self, cfg):
        self.key = cfg.registry.companies_house_key
        self.disabled = False

    def jurisdictions(self) -> set[str]:
        return {"GB", "UK"}

    def lookup(self, business_row) -> list[tuple[Person, Evidence]]:
        people, _profile = self.lookup_with_profile(business_row)
        return people

    def lookup_with_profile(self, business_row) -> tuple[list[tuple[Person, Evidence]], dict | None]:
        """Officers + the matched company's registry profile (number, incorporation, status, SIC).
        Never raises; ([], None) on any problem. On 429: back off 60s once, then disable for the run."""
        if self.disabled or not self.key:
            return [], None
        import httpx

        try:
            auth = (self.key, "")
            _CH_BUCKET.wait()
            r = httpx.get(f"{CH_BASE}/search/companies",
                          params={"q": business_row["name"], "items_per_page": 3},
                          auth=auth, timeout=15.0)
            if r.status_code == 429:
                time.sleep(60)
                self.disabled = True
                return [], None
            r.raise_for_status()
            for item in (r.json().get("items") or []):
                if not _locality_overlap(item, business_row):
                    continue
                number = item.get("company_number")
                if not number:
                    continue
                profile = {"company_number": number,
                           "incorporated": item.get("date_of_creation") or "",
                           "company_status": item.get("company_status") or "",
                           "legal_name": item.get("title") or "", "sic_codes": []}
                try:
                    _CH_BUCKET.wait()
                    rp = httpx.get(f"{CH_BASE}/company/{number}", auth=auth, timeout=15.0)
                    if rp.status_code == 200:
                        pj = rp.json()
                        profile["sic_codes"] = pj.get("sic_codes") or []
                        profile["incorporated"] = pj.get("date_of_creation") or profile["incorporated"]
                        profile["company_status"] = pj.get("company_status") or profile["company_status"]
                except Exception:  # noqa: BLE001 — profile is a bonus, never blocks officers
                    pass
                _CH_BUCKET.wait()
                _CH_BUCKET.wait()
                ro = httpx.get(f"{CH_BASE}/company/{number}/officers",
                               params={"register_type": "directors"}, auth=auth, timeout=15.0)
                if ro.status_code == 429:
                    time.sleep(60)
                    self.disabled = True
                    return [], profile
                ro.raise_for_status()
                profile_url = f"https://find-and-update.company-information.service.gov.uk/company/{number}"
                out = []
                for off in (ro.json().get("items") or []):
                    if off.get("resigned_on"):
                        continue
                    role = _humanize(str(off.get("officer_role") or "officer"))
                    person = Person(business_id=business_row["id"],
                                    name=natural_name(str(off.get("name") or "")).title(),
                                    title=role, labeled_by="registry", is_dm=0, source_url=profile_url)
                    ev = Evidence(business_id=business_row["id"], ref_table="people", fact="registry_officer",
                                  url=profile_url, snippet=f"{role} — appointed {off.get('appointed_on', '?')}",
                                  observed_at=now_iso())
                    out.append((person, ev))
                return out, profile
        except Exception as e:  # noqa: BLE001 — registries must never block the run
            LOG.warning("companies_house lookup failed for %s: %s", business_row["name"], type(e).__name__)
        return [], None


class OpenCorporatesRegistry:
    name = "opencorporates"

    def __init__(self, cfg):
        self.token = cfg.registry.opencorporates_token
        self.disabled = False

    def jurisdictions(self) -> set[str]:
        return {"GB", "UK", "US"}

    def _jurisdiction_code(self, biz) -> str:
        country = (biz["address_country"] or "").strip().upper()
        if country in ("GB", "UK"):
            return "gb"
        region = (biz["address_region"] or "").strip().lower()
        return f"us_{region}" if country == "US" and len(region) == 2 else ""

    def lookup(self, business_row) -> list[tuple[Person, Evidence]]:
        if self.disabled or not self.token:
            return []
        import httpx

        try:
            params = {"q": business_row["name"], "api_token": self.token}
            code = self._jurisdiction_code(business_row)
            if code:
                params["jurisdiction_code"] = code
            r = httpx.get(f"{OC_BASE}/companies/search", params=params, timeout=15.0)
            if r.status_code in (403, 429):
                self.disabled = True
                return []
            r.raise_for_status()
            out = []
            for wrap in ((r.json().get("results") or {}).get("companies") or [])[:3]:
                comp = wrap.get("company") or {}
                addr = {"address_snippet": comp.get("registered_address_in_full") or ""}
                if not _locality_overlap(addr | {"address": {}}, business_row):
                    continue
                url = comp.get("opencorporates_url") or ""
                for off in (comp.get("officers") or []):
                    o = off.get("officer") or {}
                    if o.get("end_date"):
                        continue
                    role = _humanize(str(o.get("position") or "officer"))
                    person = Person(business_id=business_row["id"],
                                    name=natural_name(str(o.get("name") or "")).title(),
                                    title=role, labeled_by="registry", is_dm=0, source_url=url)
                    ev = Evidence(business_id=business_row["id"], ref_table="people", fact="registry_officer",
                                  url=url, snippet=f"{role} — {o.get('start_date', '?')}", observed_at=now_iso())
                    out.append((person, ev))
            return out
        except Exception as e:  # noqa: BLE001
            LOG.warning("opencorporates lookup failed for %s: %s", business_row["name"], type(e).__name__)
        return []


def get_registries(cfg) -> list:
    """Instances for every registry with a configured key (none -> no network paths reachable)."""
    out = []
    if cfg.registry.companies_house_key:
        out.append(CompaniesHouseRegistry(cfg))
    if cfg.registry.opencorporates_token:
        out.append(OpenCorporatesRegistry(cfg))
    return out


def enabled_registries(cfg) -> list[str]:
    out = []
    if cfg.registry.companies_house_key:
        out.append("companies_house")
    if cfg.registry.opencorporates_token:
        out.append("opencorporates")
    return out


# ============================================================================ v0.3 interface (U9.6)
_LEGAL_TOKENS = {"ltd", "limited", "llp", "plc", "the", "and", "co", "company", "uk", "group", "holdings", "services"}


def name_similarity(business_name_norm: str, company_name: str) -> float:
    """0..1 — how much a registry company name resembles the business name (legal tokens ignored).
    Max of token Jaccard and a character-sequence ratio, so 'A B S MOT Station' ~ 'ABS MOT STATION LTD'."""
    import difflib
    import re as _re

    def toks(s: str) -> set[str]:
        return {t for t in _re.split(r"[^a-z0-9]+", (s or "").casefold()) if t and t not in _LEGAL_TOKENS}

    a, b = toks(business_name_norm), toks(company_name)
    jacc = len(a & b) / len(a | b) if (a | b) else 0.0
    ca, cb = "".join(sorted(a)), "".join(sorted(b))
    ratio = difflib.SequenceMatcher(None, ca, cb).ratio() if ca and cb else 0.0
    return round(max(jacc, ratio), 3)
