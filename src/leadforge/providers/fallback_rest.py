"""Fallback REST discovery provider — ICM unit U3.6 (implemented v0.1.0; docker up-path verified by operator).

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

import httpx

from leadforge.grid import PlannedQuery
from leadforge.models import RawListing
from leadforge.providers.base import DiscoveryProvider, register
from leadforge.util import ProviderDegraded, now_iso


@register
class FallbackRestProvider(DiscoveryProvider):
    name = "fallback_rest"

    def available(self) -> tuple[bool, str]:
        url = self.cfg.discovery.fallback_rest.url
        try:
            r = httpx.get(f"{url}/docs", timeout=3.0)
        except httpx.HTTPError:
            return False, f"fallback REST service not reachable at {url} (start its docker container)"
        if r.status_code < 500:
            return True, f"rest up at {url}"
        return False, f"rest unhealthy ({r.status_code})"

    def fetch(self, query: PlannedQuery, limit: int | None = None) -> list[RawListing]:
        # No geo-tiling in this engine: query.tile is deliberately ignored (text queries only).
        url = self.cfg.discovery.fallback_rest.url
        params = {"query": query.text, "max_results": limit or 100}
        try:
            r = httpx.get(f"{url}/scrape-get", params=params, timeout=30.0)
            r.raise_for_status()
            rows = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ProviderDegraded(f"fallback_rest failed on '{query.text}': {type(e).__name__}") from e
        if not isinstance(rows, list):
            raise ProviderDegraded(f"fallback_rest returned {type(rows).__name__}, expected list")
        return [RawListing(provider=self.name, fetched_at=now_iso(), data=self._map(row))
                for row in rows if isinstance(row, dict)]

    @staticmethod
    def _map(row: dict) -> dict:
        """Translate this engine's keys into the GOSOM-style keys normalize.py already understands.
        Keeps every original key too, so nothing is lost if the map is incomplete."""
        out = dict(row)
        alias = {
            "name": "title", "business_name": "title",
            "website": "web_site", "url": "web_site",
            "phone_number": "phone",
            "rating": "review_rating", "reviews": "review_count", "num_reviews": "review_count",
            "lat": "latitude", "lng": "longitude", "lon": "longitude",
        }
        for src, dst in alias.items():
            if src in row and dst not in out:
                out[dst] = row[src]
        return out
