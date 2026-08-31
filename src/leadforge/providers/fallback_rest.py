"""Fallback REST discovery provider — ICM unit U3.6 (STUB with binding spec).

Target engine: conor-is-my-name/google-maps-scraper (MIT, Playwright/FastAPI, Docker).
An INDEPENDENT selector implementation, so a Google markup change rarely kills both providers at once.

=== SPEC (implement exactly; acceptance criteria in docs/05 U3.6) ===
1. available(): GET {cfg.discovery.fallback_rest.url}/docs (or /) with 3s timeout ->
   (True, "rest up") | (False, "docker service not reachable at <url> — see repo README of the engine").
2. fetch(query, limit): GET {url}/scrape-get params {"query": query.text, "max_results": limit or 100},
   timeout 30s per request; response JSON list of listings.
   Map fields -> RawListing.data with keys matching GOSOM-style names where possible:
   name, address, phone, website, rating (float), review_count (int), latitude, longitude, place_id (may be absent
   -> normalize.py falls back to name+address dedupe).
3. Errors: httpx timeout / 5xx -> ProviderDegraded(msg). Connection refused -> available() False path;
   fetch should raise ProviderDegraded, never ProviderFailed (the chain decides).
4. No tile support: ignore query.tile (text queries only) — document in msg when tiles requested.
5. Add tests/test_providers.py::test_fallback_rest_parse with a canned JSON fixture (no network).
"""

from __future__ import annotations

from leadforge.grid import PlannedQuery
from leadforge.models import RawListing
from leadforge.providers.base import DiscoveryProvider, register


@register
class FallbackRestProvider(DiscoveryProvider):
    name = "fallback_rest"

    def available(self) -> tuple[bool, str]:
        return False, "U3.6 not implemented yet — see module docstring spec"

    def fetch(self, query: PlannedQuery, limit: int | None = None) -> list[RawListing]:
        raise NotImplementedError("U3.6: implement per module docstring spec")
