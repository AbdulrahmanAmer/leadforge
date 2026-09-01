"""DVSA "Active MOT test stations" register provider (v0.3 unit B, docs/09 wave 1).

Official gov.uk OGL dataset, refreshed quarterly. Not a scraper: this downloads one CSV asset,
caches it under `cfg.cache_dir/"dvsa"/active-mot-stations.csv`, and filters rows by town for the
query's locality. `supports_tiles = False` — the pipeline already warns when a tiled plan falls
through to a text-only provider (see tests/test_providers.py::test_fallback_without_tile_support...).

Live-file quirk (measured 2026-09-02): the CSV is cp1252, not UTF-8 — some trading names carry a
0x92 byte (a right single quote from a Windows export). Decode explicitly as cp1252 with
errors="replace" so a genuinely bad byte degrades gracefully instead of raising.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path

import httpx

from leadforge.config import Config
from leadforge.grid import PlannedQuery
from leadforge.models import RawListing
from leadforge.providers.base import DiscoveryProvider, register
from leadforge.util import LOG, ProviderDegraded, ProviderFailed, now_iso

CSV_FILENAME = "active-mot-stations.csv"
_CLASS_COLS = ["Class_1", "Class_2", "Class_3", "Class_4", "Class_5", "Class_7"]
_CLASS_NUMS = ["1", "2", "3", "4", "5", "7"]

# Common connector words that stay lowercase in title-cased trading names (unless they open the
# name); everything else <=3 chars and ALL-CAPS is treated as an acronym ('MOT', 'ABS', 'LTD')
# and kept upper.
_TITLE_STOPWORDS = {"and", "of", "the", "for", "at", "on", "to", "in", "&"}


def _title_case_name(raw: str) -> str:
    """Title-case a trading name, keeping short all-caps acronym-like tokens (<=3 chars, e.g.
    'MOT', 'ABS') upper while lowercasing common connector words ('and', 'of', ...)."""
    words = raw.split(" ")
    out: list[str] = []
    for i, w in enumerate(words):
        if not w:
            out.append(w)
            continue
        bare = w.strip("&.,'")
        low = w.casefold()
        if low in _TITLE_STOPWORDS and i != 0:
            out.append(low)
        elif bare and bare.isalpha() and bare.isupper() and len(bare) <= 3:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def _cache_path(cfg: Config) -> Path:
    d = cfg.cache_dir / "dvsa"
    d.mkdir(parents=True, exist_ok=True)
    return d / CSV_FILENAME


def _is_stale(path: Path, refresh_days: int) -> bool:
    if not path.exists():
        return True
    age_days = (datetime.now(UTC).timestamp() - path.stat().st_mtime) / 86400.0
    return age_days >= refresh_days


def _download_csv(cfg: Config) -> bytes:
    """Module-level downloader so tests can monkeypatch it directly instead of httpx internals."""
    headers = {"User-Agent": cfg.politeness.user_agent}
    with httpx.Client(follow_redirects=True, timeout=60, headers=headers) as client:
        r = client.get(cfg.discovery.dvsa.url)
        r.raise_for_status()
        return r.content


def _ensure_csv(cfg: Config) -> Path:
    path = _cache_path(cfg)
    if _is_stale(path, cfg.discovery.dvsa.refresh_days):
        try:
            content = _download_csv(cfg)
        except httpx.HTTPError as e:
            if path.exists():
                LOG.warning("dvsa: refresh failed (%s: %s); using stale cache", type(e).__name__, e)
                return path
            raise ProviderDegraded(f"dvsa CSV download failed: {type(e).__name__}: {e}") from e
        path.write_bytes(content)
        LOG.info("dvsa: downloaded %d bytes to %s", len(content), path)
    return path


def _register_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date().isoformat()


def _read_rows(path: Path) -> list[dict]:
    text = path.read_bytes().decode("cp1252", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def _locality_from_query(query: PlannedQuery) -> str:
    area = (query.area or "").strip()
    if area:
        return area.split(",")[0].strip()
    text = query.text or ""
    idx = text.find(" in ")
    if idx == -1:
        return ""
    rest = text[idx + len(" in "):]
    return rest.split(",")[0].strip()


def enrich_for(raw_data: dict) -> dict:
    """Standalone accessor for the dvsa enrich payload already embedded in RawListing.data['enrich'].

    normalize.to_business (owned by unit A) does not read data['enrich'] today, so nothing wires
    this into Business.enrich automatically yet — see requests_for_other_units in this unit's report.
    Any caller (pipeline stage, a future normalize.py change, a test) can pull the same dict straight
    out of a RawListing.data via this helper without re-deriving the shape.
    """
    return dict(raw_data.get("enrich", {}).get("dvsa", {}))


def _row_to_listing(row: dict, register_date: str, fetched_at: str) -> RawListing | None:
    name_raw = (row.get("Trading_Name") or "").strip()
    if not name_raw:
        return None
    name = _title_case_name(name_raw)
    addr1 = (row.get("Address1") or "").strip()
    addr2 = (row.get("Address2") or "").strip()
    addr3 = (row.get("Address3") or "").strip()
    town = (row.get("Town") or "").strip()
    postcode = (row.get("Postcode") or "").strip()
    phone = (row.get("Phone") or "").strip()
    site_number = (row.get("Site_Number") or "").strip()

    address = ", ".join(p for p in [addr1, addr2, addr3, town, postcode] if p)
    street = " ".join(p for p in [addr1, addr2] if p)
    classes = [n for col, n in zip(_CLASS_COLS, _CLASS_NUMS, strict=True) if (row.get(col) or "").strip()]
    categories = ["MOT test station"] + [f"MOT class {n}" for n in classes]

    data = {
        "name": name,
        "phone": phone or None,
        "address": address or None,
        "complete_address": {
            "street": street or None,
            "city": town.title() if town else None,
            "postal_code": postcode or None,
            "country": "United Kingdom",
        },
        "category": "MOT test station",
        "categories": categories,
        "website": None,
        "place_id": None,
        "maps_url": None,
        "site_number": site_number or None,
        "classes": classes,
        "source_register": "dvsa",
        "enrich": {
            "dvsa": {
                "site_number": site_number or None,
                "classes": classes,
                "register_date": register_date,
            }
        },
    }
    return RawListing(provider="dvsa", fetched_at=fetched_at, data=data)


@register
class DvsaProvider(DiscoveryProvider):
    name = "dvsa"
    supports_tiles = False  # town-filter only; the pipeline warns when a tiled plan reaches us

    # v0.3: set as a class attribute so providers.base.register() picks it up at decoration time
    # (unlike gosom's FIELD_MAP, which is assigned after the class body and registered explicitly).
    FIELD_MAP = {
        "name": ["name"],
        "phone": ["phone"],
        "address": ["address"],
        "category": ["category"],
        "categories": ["categories"],
        "website": ["website"],  # always absent for dvsa
        "place_id": ["place_id"],  # always absent for dvsa
        "maps_url": ["maps_url"],  # always absent for dvsa
    }

    def available(self) -> tuple[bool, str]:
        url = self.cfg.discovery.dvsa.url
        if not url:
            return False, "discovery.dvsa.url is empty — set it to enable the dvsa provider"
        path = _cache_path(self.cfg)
        if path.exists() and not _is_stale(path, self.cfg.discovery.dvsa.refresh_days):
            return True, f"cached {_register_date(path)}"
        return True, "will download"

    def fetch(self, query: PlannedQuery, limit: int | None = None) -> list[RawListing]:
        if not self.cfg.discovery.dvsa.url:
            raise ProviderFailed("discovery.dvsa.url is empty")
        locality = _locality_from_query(query)
        if not locality:
            LOG.warning("dvsa: could not determine a locality from query text=%r area=%r", query.text, query.area)
            return []
        path = _ensure_csv(self.cfg)
        register_date = _register_date(path)
        fetched_at = now_iso()
        locality_cf = locality.casefold()

        out: list[RawListing] = []
        for row in _read_rows(path):
            if (row.get("Town") or "").strip().casefold() != locality_cf:
                continue
            listing = _row_to_listing(row, register_date, fetched_at)
            if listing is None:
                continue
            out.append(listing)
            if limit and len(out) >= limit:
                break
        LOG.info("dvsa: %d listing(s) for locality '%s'", len(out), locality)
        return out
