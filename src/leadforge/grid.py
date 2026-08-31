"""Geocoding + query planning (U3.2).

Turns ICP geography into a provider-agnostic query plan:
- text mode (default): one query per (category x area) -> "category in area"
- grid mode (config discovery.grid_mode=auto, or ICP bbox): bbox split into tiles, one job per
  (category x tile) for engines that support geo-targeted search. Tile math is pure + unit-tested;
  the gosom grid flags themselves are live-verified in U8.2 before enabling by default.

Geocoding: Nominatim public API, 1 req/s, permanent JSON cache, identifying UA (docs/04 §3.1).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from leadforge.config import Config
from leadforge.models import ICP
from leadforge.util import LOG, InputError

NOMINATIM = "https://nominatim.openstreetmap.org/search"
_KM_PER_DEG_LAT = 110.574

# ISO2 -> plain name, used to country-qualify query text (not exhaustive; falls back to the code itself).
COUNTRY_NAMES = {
    "US": "United States", "CA": "Canada", "GB": "United Kingdom", "IE": "Ireland", "AU": "Australia",
    "NZ": "New Zealand", "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy", "NL": "Netherlands",
    "BE": "Belgium", "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland",
    "PT": "Portugal", "CH": "Switzerland", "AT": "Austria", "EG": "Egypt", "AE": "United Arab Emirates",
    "SA": "Saudi Arabia", "QA": "Qatar", "KW": "Kuwait", "MA": "Morocco", "ZA": "South Africa",
    "NG": "Nigeria", "KE": "Kenya", "IN": "India", "PK": "Pakistan", "SG": "Singapore", "MY": "Malaysia",
    "PH": "Philippines", "ID": "Indonesia", "JP": "Japan", "KR": "South Korea", "MX": "Mexico",
    "BR": "Brazil", "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "TR": "Turkey", "GR": "Greece",
}


@dataclass
class Tile:
    bbox: tuple[float, float, float, float]  # minLng, minLat, maxLng, maxLat
    cell_km: float

    def as_json(self) -> dict:
        return {"bbox": list(self.bbox), "cell_km": self.cell_km}


@dataclass
class PlannedQuery:
    text: str          # "auto repair shop in Houston, TX" (always set; engines without geo mode use it)
    category: str
    area: str
    tile: Tile | None = None


def geocode(area: str, cfg: Config, country: str) -> dict:
    """Resolve one area WITHIN a country -> {"lat","lng","bbox","display"} ; cached forever.

    `country` (ISO2) is mandatory: it constrains Nominatim so 'Houston' can't silently resolve to the wrong
    country. Genuinely ambiguous matches inside the country raise InputError listing the candidates so the
    operator can disambiguate, rather than the run quietly scraping the wrong place.
    """
    cache_file = cfg.cache_dir / "geocode.json"
    cache: dict = {}
    if cache_file.is_file():
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    key = f"{country.upper()}|{area.strip().lower()}"
    if key in cache:
        return cache[key]
    time.sleep(1.0)  # Nominatim usage policy: max 1 req/s
    try:
        r = httpx.get(
            NOMINATIM,
            params={"q": area, "format": "jsonv2", "limit": 5, "countrycodes": country.lower(),
                    "addressdetails": 1},
            headers={"User-Agent": cfg.politeness.user_agent},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
    except httpx.HTTPError as e:
        raise InputError(
            f"could not geocode '{area}' in {country}: {type(e).__name__} — check spelling or set target.geography.bbox"
        ) from e
    if not rows:
        raise InputError(
            f"'{area}' not found in country {country}. Check the spelling, add a state/region "
            f"(e.g. 'Springfield, IL'), or confirm the country code is right."
        )

    rows = [r_ for r_ in rows if r_.get("boundingbox")]
    if not rows:
        raise InputError(f"'{area}' returned no usable bounding box in {country}")

    # Ambiguity guard: two near-equally strong, geographically distinct matches -> ask, don't guess.
    if len(rows) > 1:
        top, second = rows[0], rows[1]
        gap = float(top.get("importance", 0) or 0) - float(second.get("importance", 0) or 0)
        if gap < 0.05 and _distinct_places(top, second):
            names = "; ".join(r_.get("display_name", "?") for r_ in rows[:3])
            raise InputError(
                f"'{area}' is ambiguous in {country} — candidates: {names}. "
                f"Re-run with a more specific area (add state/region/county)."
            )

    row = rows[0]
    bb = [float(x) for x in row["boundingbox"]]  # Nominatim: [minLat, maxLat, minLng, maxLng]
    out = {"lat": float(row["lat"]), "lng": float(row["lon"]), "bbox": [bb[2], bb[0], bb[3], bb[1]],
           "display": row.get("display_name", area), "type": row.get("addresstype") or row.get("type", "")}
    span_km = (out["bbox"][2] - out["bbox"][0]) * 85 + (out["bbox"][3] - out["bbox"][1]) * 111
    if span_km > 1500:
        LOG.warning("area '%s' resolved to a very large region (%s) — expect a huge query plan", area, out["display"])
    cache[key] = out
    cache_file.write_text(json.dumps(cache, indent=0), encoding="utf-8")
    LOG.info("geocoded %s (%s) -> %s", area, country, out["display"])
    return out


def _distinct_places(a: dict, b: dict) -> bool:
    """True when two Nominatim hits are different real places (not the same city returned twice)."""
    ad, bd = a.get("address", {}) or {}, b.get("address", {}) or {}
    keys = ("state", "county", "city", "town", "village")
    av = tuple(ad.get(k) for k in keys)
    bv = tuple(bd.get(k) for k in keys)
    if av != bv:
        return True
    try:
        return abs(float(a["lat"]) - float(b["lat"])) > 0.3 or abs(float(a["lon"]) - float(b["lon"])) > 0.3
    except (KeyError, TypeError, ValueError):
        return True


def make_tiles(bbox: list[float], cell_km: float, max_tiles: int) -> list[Tile]:
    """Split bbox into ~cell_km tiles; if the count exceeds max_tiles, grow the cell to fit the cap."""
    min_lng, min_lat, max_lng, max_lat = bbox
    mid_lat = (min_lat + max_lat) / 2
    km_per_deg_lng = _KM_PER_DEG_LAT * max(0.1, math.cos(math.radians(mid_lat)))
    w_km = max(0.001, (max_lng - min_lng) * km_per_deg_lng)
    h_km = max(0.001, (max_lat - min_lat) * _KM_PER_DEG_LAT)

    def counts(cell: float) -> tuple[int, int]:
        return max(1, math.ceil(w_km / cell)), max(1, math.ceil(h_km / cell))

    nx, ny = counts(cell_km)
    while nx * ny > max_tiles:
        cell_km *= 1.5
        nx, ny = counts(cell_km)

    tiles = []
    for ix in range(nx):
        for iy in range(ny):
            t_min_lng = min_lng + (max_lng - min_lng) * ix / nx
            t_max_lng = min_lng + (max_lng - min_lng) * (ix + 1) / nx
            t_min_lat = min_lat + (max_lat - min_lat) * iy / ny
            t_max_lat = min_lat + (max_lat - min_lat) * (iy + 1) / ny
            tiles.append(Tile(bbox=(t_min_lng, t_min_lat, t_max_lng, t_max_lat), cell_km=cell_km))
    return tiles


def qualify_area(area: str, country: str) -> str:
    """Append the country name to a search phrase so the scraper itself isn't guessing either.
    'Houston, TX' + US -> 'Houston, TX, United States'."""
    country_name = COUNTRY_NAMES.get(country.upper(), country.upper())
    if country_name.casefold() in area.casefold():
        return area
    return f"{area}, {country_name}"


def build_plan(icp: ICP, cfg: Config) -> list[PlannedQuery]:
    geo = icp.target.geography
    grid_on = cfg.discovery.grid_mode == "auto" and geo.grid == "auto"
    queries: list[PlannedQuery] = []
    areas = geo.areas or ["(bbox)"]

    if geo.bbox and grid_on:
        tiles = make_tiles(geo.bbox, cfg.discovery.grid_cell_km, icp.caps.max_tiles)
        for cat in icp.target.categories:
            for tile in tiles:
                queries.append(PlannedQuery(text=f"{cat}", category=cat, area=areas[0], tile=tile))
        return queries

    for area in geo.areas:
        tiles: list[Tile] | None = None
        if grid_on:
            g = geocode(area, cfg, geo.country)
            tiles = make_tiles(g["bbox"], cfg.discovery.grid_cell_km, icp.caps.max_tiles)
        # country-qualified query text: keeps the scraper in the right country too
        qtext_area = qualify_area(area, geo.country)
        for cat in icp.target.categories:
            if tiles:
                for tile in tiles:
                    queries.append(PlannedQuery(text=f"{cat} in {qtext_area}", category=cat, area=area, tile=tile))
            else:
                queries.append(PlannedQuery(text=f"{cat} in {qtext_area}", category=cat, area=area))
    if not queries:
        raise InputError("ICP produced no queries: need target.categories and geography.areas (or bbox)")
    return queries


def plan_counts(queries: list[PlannedQuery]) -> dict:
    tiles = sum(1 for q in queries if q.tile)
    return {
        "queries": len(queries),
        "tiles": tiles,
        "est_max_results": len(queries) * 120,  # Google Maps ~120-results-per-query ceiling (docs/01 §2)
    }


def cache_plan_path(cfg: Config) -> Path:
    return cfg.cache_dir / "last_plan.json"
