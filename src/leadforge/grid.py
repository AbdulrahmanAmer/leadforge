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
_GEOCODE_ATTEMPTS = 3

# A1 (docs/09): Nominatim addresstype values that are real inhabited places vs. incidental hits
# (a canal, a tourist attraction, a shop) that outrank the place we actually want by importance
# alone. Disfavored rows are excluded from both the resolved pick and the ambiguity comparison.
_PREFERRED_ADDRESSTYPES = {
    "city", "town", "borough", "administrative", "suburb", "village", "county", "municipality",
}
_DISFAVORED_ADDRESSTYPES = {"waterway", "amenity", "tourism", "information", "canal"}

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
    depth: int = 0  # A2: 0 = the plan's original tile, N = the Nth saturation-subdivision generation

    def to_json(self) -> dict:
        return {"bbox": list(self.bbox), "cell_km": self.cell_km, "depth": self.depth}

    as_json = to_json  # back-compat alias (pre-v0.3 callers)

    @classmethod
    def from_json(cls, d: dict) -> Tile:
        return cls(bbox=tuple(d["bbox"]), cell_km=d["cell_km"], depth=d.get("depth", 0))


@dataclass
class PlannedQuery:
    text: str          # "auto repair shop in Houston, TX" (always set; engines without geo mode use it)
    category: str
    area: str
    tile: Tile | None = None


def _area_bbox_override(area: str, cfg: Config) -> list[float] | None:
    """cfg.discovery.area_bbox[area] (exact or casefolded) bypasses Nominatim entirely (docs/09 A1) —
    for areas the geocoder gets wrong (e.g. 'Manchester' resolving to a canal) or that the operator
    already knows the box for. [minLng, minLat, maxLng, maxLat], same convention as Tile.bbox."""
    d = cfg.discovery.area_bbox
    if area in d:
        return d[area]
    cf = area.strip().casefold()
    for k, v in d.items():
        if k.strip().casefold() == cf:
            return v
    return None


def geocode(area: str, cfg: Config, country: str) -> dict:
    """Resolve one area WITHIN a country -> {"lat","lng","bbox","display"} ; cached forever.

    `country` (ISO2) is mandatory: it constrains Nominatim so 'Houston' can't silently resolve to the wrong
    country. Genuinely ambiguous matches inside the country raise InputError listing the candidates so the
    operator can disambiguate, rather than the run quietly scraping the wrong place.
    """
    override = _area_bbox_override(area, cfg)
    if override is not None:
        min_lng, min_lat, max_lng, max_lat = (float(x) for x in override)
        out = {"lat": (min_lat + max_lat) / 2, "lng": (min_lng + max_lng) / 2,
               "bbox": [min_lng, min_lat, max_lng, max_lat], "display": area, "type": "override"}
        LOG.info("geocode override for '%s' -> %s (Nominatim skipped)", area, out["bbox"])
        return out

    cache_file = cfg.cache_dir / "geocode.json"
    cache: dict = {}
    if cache_file.is_file():
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    key = f"{country.upper()}|{area.strip().lower()}"
    if key in cache:
        return cache[key]
    rows = None
    last_err: Exception | None = None
    for attempt in range(_GEOCODE_ATTEMPTS):
        time.sleep(1.0 * (attempt + 1))  # Nominatim usage policy: max 1 req/s; back off on retries
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
            break
        except httpx.HTTPError as e:
            # a transient blip must not kill a multi-hour tiled plan on its first area
            last_err = e
            LOG.warning("geocode attempt %d/%d for '%s' failed: %s",
                        attempt + 1, _GEOCODE_ATTEMPTS, area, type(e).__name__)
    _bbox_hint = (
        f'or set discovery.area_bbox["{area}"] = [minLng, minLat, maxLng, maxLat] in config '
        "to bypass the geocoder"
    )
    if rows is None:
        raise InputError(
            f"could not geocode '{area}' in {country} after {_GEOCODE_ATTEMPTS} attempts: "
            f"{type(last_err).__name__} — check spelling/network or set target.geography.bbox, {_bbox_hint}"
        ) from last_err
    if not rows:
        raise InputError(
            f"'{area}' not found in country {country}. Check the spelling, add a state/region "
            f"(e.g. 'Springfield, IL'), {_bbox_hint}."
        )

    rows = [r_ for r_ in rows if r_.get("boundingbox")]
    if not rows:
        raise InputError(f"'{area}' returned no usable bounding box in {country}, {_bbox_hint}")

    # A1: a canal/amenity/tourist-spot row is never the resolved place and never counts toward
    # ambiguity — only real-place rows (or, failing that, whatever Nominatim gave us) compete.
    candidates = [r_ for r_ in rows if (r_.get("addresstype") or r_.get("type") or "").casefold()
                  not in _DISFAVORED_ADDRESSTYPES] or rows

    # A1: among the survivors, a _PREFERRED_ADDRESSTYPES row (city/town/borough/...) always outranks
    # a row that is merely not-disfavored (railway, shop, building, road, place, ...) even at lower
    # Nominatim importance — "prefer city, town, ..." would otherwise let e.g. a high-importance
    # "road" beat a lower one "city" row. Preferred rows are their own tier: like the disfavored
    # exclusion above, a preferred row is never compared for ambiguity against a merely-not-disfavored
    # one — only candidates within the SAME tier compete (this also keeps the ambiguity gap's
    # importance-descending assumption intact, since each tier preserves Nominatim's own ordering).
    preferred = [r_ for r_ in candidates if (r_.get("addresstype") or r_.get("type") or "").casefold()
                 in _PREFERRED_ADDRESSTYPES]
    candidates = preferred or candidates

    # Ambiguity guard: two near-equally strong, geographically distinct matches -> ask, don't guess.
    if len(candidates) > 1:
        top, second = candidates[0], candidates[1]
        gap = float(top.get("importance", 0) or 0) - float(second.get("importance", 0) or 0)
        if gap < 0.05 and _distinct_places(top, second):
            names = "; ".join(r_.get("display_name", "?") for r_ in candidates[:3])
            raise InputError(
                f"'{area}' is ambiguous in {country} — candidates: {names}. "
                f"Re-run with a more specific area (add state/region/county), {_bbox_hint}."
            )

    row = candidates[0]
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


def _bboxes_overlap(bb_a, bb_b) -> bool:
    """Nominatim boundingbox order: [minLat, maxLat, minLng, maxLng] (strings)."""
    if not bb_a or not bb_b:
        return False
    try:
        a_min_lat, a_max_lat, a_min_lng, a_max_lng = (float(x) for x in bb_a)
        b_min_lat, b_max_lat, b_min_lng, b_max_lng = (float(x) for x in bb_b)
    except (TypeError, ValueError):
        return False
    return a_min_lat <= b_max_lat and b_min_lat <= a_max_lat and a_min_lng <= b_max_lng and b_min_lng <= a_max_lng


def _distinct_places(a: dict, b: dict) -> bool:
    """True when two Nominatim hits are different real places (not the same city returned twice).

    A1 (docs/09): coordinates are compared FIRST. Two hits within 0.15 degrees of each other whose
    bounding boxes overlap are the same place regardless of how their address dicts differ — a
    city-boundary row and an administrative-boundary row for the same city carry different address
    component keys (one may have no 'city' key at all) but sit at (almost) the same point. Only when
    the coordinates don't already prove sameness do address labels get a say.
    """
    try:
        lat_a, lon_a = float(a["lat"]), float(a["lon"])
        lat_b, lon_b = float(b["lat"]), float(b["lon"])
    except (KeyError, TypeError, ValueError):
        lat_a = lon_a = lat_b = lon_b = None
    if lat_a is not None and abs(lat_a - lat_b) <= 0.15 and abs(lon_a - lon_b) <= 0.15:
        if _bboxes_overlap(a.get("boundingbox"), b.get("boundingbox")):
            return False

    ad, bd = a.get("address", {}) or {}, b.get("address", {}) or {}
    keys = ("state", "county", "city", "town", "village")
    av = tuple(ad.get(k) for k in keys)
    bv = tuple(bd.get(k) for k in keys)
    if av != bv:
        return True
    if lat_a is not None:
        return abs(lat_a - lat_b) > 0.3 or abs(lon_a - lon_b) > 0.3
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


def quarter_tile(tile: Tile) -> list[Tile]:
    """Split a saturated tile into 4 equal quadrants, one depth deeper (A2 saturation subdivision)."""
    min_lng, min_lat, max_lng, max_lat = tile.bbox
    mid_lng = (min_lng + max_lng) / 2
    mid_lat = (min_lat + max_lat) / 2
    depth = tile.depth + 1
    cell = tile.cell_km / 2
    return [
        Tile(bbox=(min_lng, min_lat, mid_lng, mid_lat), cell_km=cell, depth=depth),
        Tile(bbox=(mid_lng, min_lat, max_lng, mid_lat), cell_km=cell, depth=depth),
        Tile(bbox=(min_lng, mid_lat, mid_lng, max_lat), cell_km=cell, depth=depth),
        Tile(bbox=(mid_lng, mid_lat, max_lng, max_lat), cell_km=cell, depth=depth),
    ]


def qualify_area(area: str, country: str) -> str:
    """Append the country name to a search phrase so the scraper itself isn't guessing either.
    'Houston, TX' + US -> 'Houston, TX, United States'."""
    country_name = COUNTRY_NAMES.get(country.upper(), country.upper())
    if country_name.casefold() in area.casefold():
        return area
    return f"{area}, {country_name}"


def build_plan(icp: ICP, cfg: Config) -> list[PlannedQuery]:
    """Ordered so a `caps.max_leads` stop mid-plan can never starve a whole category or area.

    Discovery walks queries in insertion order and breaks the moment the unique-lead cap is hit
    (pipeline.run_discover). Emitting category-major ("all tiles of category 1, then category 2")
    meant a capped run could finish having never searched the last category at all. Queries are
    therefore rotated: tile index first, then area, then category — every category is served in
    every area before any tile advances.
    """
    geo = icp.target.geography
    grid_on = cfg.discovery.grid_mode == "auto" and geo.grid == "auto"
    cats = icp.target.categories
    # (area_label, query_text_prefix, tiles|None) per area, in ICP order
    slots: list[tuple[str, str, list[Tile] | None]] = []

    if geo.bbox and grid_on:
        # bbox campaign: the box IS the geography, so the text carries no area at all
        slots.append(((geo.areas or ["(bbox)"])[0], "", make_tiles(
            geo.bbox, cfg.discovery.grid_cell_km, icp.caps.max_tiles)))
    elif geo.bbox and not geo.areas:
        raise InputError(
            "this ICP has target.geography.bbox but no areas, and grid tiling is off — a bbox is only "
            "usable in grid mode. Set discovery.grid_mode: auto in leadforge.yaml "
            "(leadforge config set discovery.grid_mode auto), or list geography.areas instead."
        )
    else:
        for area in geo.areas:
            tiles = None
            if grid_on:
                g = geocode(area, cfg, geo.country)
                tiles = make_tiles(g["bbox"], cfg.discovery.grid_cell_km, icp.caps.max_tiles)
            # country-qualified query text: keeps the scraper in the right country too
            slots.append((area, f" in {qualify_area(area, geo.country)}", tiles))

    if not slots or not cats:
        raise InputError("ICP produced no queries: need target.categories and geography.areas (or bbox)")

    max_tiles = max(len(t) if (t := s[2]) else 1 for s in slots)
    queries: list[PlannedQuery] = []
    for ti in range(max_tiles):  # tile-major rotation -> fair to categories AND areas under a cap
        for area, suffix, tiles in slots:
            if tiles is not None and ti >= len(tiles):
                continue
            if tiles is None and ti > 0:
                continue
            tile = tiles[ti] if tiles else None
            for cat in cats:
                queries.append(PlannedQuery(text=f"{cat}{suffix}", category=cat, area=area, tile=tile))
    return queries


def plan_counts(queries: list[PlannedQuery], cfg: Config | None = None) -> dict:
    """A4 (docs/09): tiled and untiled queries cost different amounts of gosom time (a tiled query
    visits a smaller area more thoroughly), so each is estimated with its own configured average
    rather than one blended constant. cfg is optional — defaults to the config's own defaults."""
    if cfg is None:
        cfg = Config()
    untiled = sum(1 for q in queries if not q.tile)
    tiled = sum(1 for q in queries if q.tile)
    cells = len({q.tile.bbox for q in queries if q.tile})  # distinct map cells, not tiled queries
    est_runtime = untiled * cfg.discovery.est_min_per_query + tiled * cfg.discovery.est_min_per_tiled_query
    return {
        "queries": len(queries),
        "tiles": cells,
        "cells": cells,
        "untiled_queries": untiled,
        "tiled_queries": tiled,
        "est_max_results": len(queries) * 120,  # Google Maps ~120-results-per-query ceiling (docs/01 §2)
        "est_runtime_min": round(est_runtime),
    }


def cache_plan_path(cfg: Config) -> Path:
    return cfg.cache_dir / "last_plan.json"
