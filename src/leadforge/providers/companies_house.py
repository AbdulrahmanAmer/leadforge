"""Companies House advanced-search discovery provider (Unit H, docs/09 Wave 2 H).

Facts measured live 2026-09-02: GET https://api.company-information.service.gov.uk/advanced-search/companies
with HTTP Basic auth (key as username, blank password) accepts sic_codes (repeatable), location,
company_status, company_type, incorporated_from, incorporated_to, size, start_index; returns
{etag, top_hit, items[{company_name, company_number, company_status, company_type, date_of_creation,
registered_office_address{address_line_1, locality, postal_code, region, country}, sic_codes[]}], kind,
hits}. Rate limit 600 req / 5 min (shared bucket, providers.registry._CH_BUCKET); paging stops at 10,000.

RawListing.data keys are declared via FIELD_MAP below (providers/base.py's @register decorator picks it
up automatically, and normalize.to_business merges it over GOSOM_FIELD_MAP for provider="companies_house"
rows) — "name" and "complete_address"/"category"/"categories" are the same keys GOSOM_FIELD_MAP already
points normalize.split_address()/map_category() at, so this provider's map is a documented passthrough.
"""

from __future__ import annotations

import re
import time

import httpx

from leadforge.grid import PlannedQuery
from leadforge.models import RawListing
from leadforge.providers.base import DiscoveryProvider, register
from leadforge.providers.registry import _CH_BUCKET, CH_BASE
from leadforge.util import LOG, ProviderDegraded, ProviderFailed, now_iso

ADVANCED_SEARCH_PATH = "/advanced-search/companies"
_QUERY_RE = re.compile(r"^sic:(?P<codes>[\d,]+)\s+loc:(?P<loc>.+)$")

# Belt-and-braces only: cfg.discovery.companies_house (config.py's CompaniesHouseDiscoveryCfg) always
# provides page_size/max_hits_per_shard/exclude_sic via its own field defaults, so this is reached
# only if exclude_sic is explicitly emptied in leadforge.yaml.
DEFAULT_EXCLUDE_SIC = frozenset({"82200"})
_PAGING_HARD_CAP = 10_000  # the API itself refuses start_index beyond this


@register
class CompaniesHouseDiscovery(DiscoveryProvider):
    name = "companies_house"
    supports_tiles = False

    # providers/base.py's @register decorator reads this off the class and calls register_field_map()
    # automatically — normalize.to_business then merges it over GOSOM_FIELD_MAP for provider=name rows.
    FIELD_MAP = {
        "name": ["name"],
        "address": ["address"],
        "complete_address": ["complete_address"],
        "category": ["category"],
        "categories": ["categories"],
    }

    def available(self) -> tuple[bool, str]:
        if not self.cfg.registry.companies_house_key:
            return False, ("no registry.companies_house_key configured — get a free key at "
                          "https://developer.company-information.service.gov.uk then run: "
                          "leadforge config set registry.companies_house_key <KEY>")
        return True, "companies house advanced search"

    def _shard_cfg(self) -> tuple[int, int, set[str]]:
        dcfg = self.cfg.discovery.companies_house
        exclude_sic = {str(c) for c in dcfg.exclude_sic} if dcfg.exclude_sic else set(DEFAULT_EXCLUDE_SIC)
        return dcfg.page_size, dcfg.max_hits_per_shard, exclude_sic

    def fetch(self, query: PlannedQuery, limit: int | None = None) -> list[RawListing]:
        m = _QUERY_RE.match(query.text.strip())
        if not m:
            raise ProviderFailed(
                f"companies_house provider got an unparseable query: '{query.text}' "
                "(expected 'sic:<code1,code2> loc:<location>' — see company.build_company_plan)"
            )
        sic_codes = [c.strip() for c in m.group("codes").split(",") if c.strip()]
        location = m.group("loc").strip()
        page_size, max_hits, exclude_sic = self._shard_cfg()
        if limit:
            max_hits = min(max_hits, limit)
        key = self.cfg.registry.companies_house_key
        auth = (key, "")

        out: list[RawListing] = []
        seen_numbers: set[str] = set()
        start_index = 0
        while len(out) < max_hits and start_index < _PAGING_HARD_CAP:
            size = min(page_size, max_hits - len(out))
            params = [("sic_codes", c) for c in sic_codes]
            params += [("location", location), ("company_status", "active"),
                      ("size", size), ("start_index", start_index)]
            try:
                _CH_BUCKET.wait()
                r = httpx.get(f"{CH_BASE}{ADVANCED_SEARCH_PATH}", params=params, auth=auth, timeout=20.0)
                if r.status_code == 429:
                    time.sleep(60)
                    _CH_BUCKET.wait()
                    r = httpx.get(f"{CH_BASE}{ADVANCED_SEARCH_PATH}", params=params, auth=auth, timeout=20.0)
                r.raise_for_status()
            except httpx.HTTPError as e:
                if out:
                    LOG.warning("companies_house shard '%s' stopped early at %d results: %s",
                                query.text, len(out), type(e).__name__)
                    break
                raise ProviderDegraded(f"companies_house request failed for '{query.text}': {type(e).__name__}") from e

            items = r.json().get("items") or []
            if not items:
                break
            for item in items:
                number = item.get("company_number")
                if not number or number in seen_numbers:
                    continue
                seen_numbers.add(number)
                status = (item.get("company_status") or "").strip().lower()
                if status and status != "active":
                    continue
                item_sics = [str(s) for s in (item.get("sic_codes") or [])]
                if any(s in exclude_sic for s in item_sics):
                    continue
                out.append(_to_raw_listing(self.name, item, item_sics))
                if len(out) >= max_hits:
                    break
            if len(items) < size:
                break
            start_index += size

        if start_index >= _PAGING_HARD_CAP:
            LOG.info("companies_house: paging cap (%d) reached for '%s'", _PAGING_HARD_CAP, query.text)
        elif len(out) >= max_hits:
            LOG.info("companies_house: max_hits_per_shard (%d) reached for '%s'", max_hits, query.text)
        return out[:limit] if limit else out


def _to_raw_listing(provider_name: str, item: dict, item_sics: list[str]) -> RawListing:
    from leadforge.company import sic_description  # local import: avoids a top-level import cycle
    addr = item.get("registered_office_address") or {}
    categories = [sic_description(s) for s in item_sics]
    return RawListing(
        provider=provider_name, fetched_at=now_iso(),
        data={
            "name": item.get("company_name") or "",
            "address": ", ".join(x for x in (addr.get("address_line_1"), addr.get("locality"),
                                             addr.get("postal_code"), addr.get("country")) if x),
            "complete_address": {
                "street": addr.get("address_line_1"), "city": addr.get("locality"),
                "state": addr.get("region"), "postal_code": addr.get("postal_code"),
                "country": addr.get("country"),
            },
            "category": categories[0] if categories else "",
            "categories": categories,
            "company_number": item.get("company_number") or "",
            "company_status": item.get("company_status") or "",
            "company_type": item.get("company_type") or "",
            "incorporated": item.get("date_of_creation") or "",
            "sic_codes": item_sics,
        },
    )


def enrich_for(raw_data: dict) -> dict:
    """Pre-fills the registry stage's own output shape so enrich/runner.py's `_registry_stage` (which
    filters on `registry_checked IS NULL`) skips a business we already have the profile for from
    discovery — one Companies House lookup per company, not two.

    Module-level by design (pipeline.py's discover loop reads it as one), but also attached to the
    provider class below so `getattr(provider_class, "enrich_for", None)` — pipeline.py's actual hook
    lookup, keyed off the provider CLASS returned from discovery, not the module — finds it too."""
    return {
        "registry_profile": {
            "company_number": raw_data.get("company_number") or "",
            "legal_name": raw_data.get("name") or "",
            "company_status": raw_data.get("company_status") or "",
            "incorporated": raw_data.get("incorporated") or "",
            "sic_codes": list(raw_data.get("sic_codes") or []),
            "match_similarity": 1.0,
        },
        "registry_checked": True,
    }


CompaniesHouseDiscovery.enrich_for = staticmethod(enrich_for)
