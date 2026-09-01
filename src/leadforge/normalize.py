"""Normalization layer (U3.4) — raw provider output -> canonical, sheet-ready Business (docs/03 §3).

This is the "clean sheet formatting" guarantee: E.164 phones, split addresses, canonical URLs/domains,
cleaned names, category mapping, deterministic dedupe keys. Pure logic; heavily unit-tested.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import phonenumbers

from leadforge.models import ICP, Business, RawListing
from leadforge.providers.gosom import GOSOM_FIELD_MAP
from leadforge.util import apex_domain, now_iso, sha1_hex, social_network

_LEGAL_SUFFIX = re.compile(
    r"\b(llc|l\.l\.c\.|inc\.?|incorporated|ltd\.?|limited|gmbh|s\.?a\.?|pllc|llp|co\.|corp\.?|corporation)\b\.?",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "ref"}

COUNTRY_TO_REGION = {
    "united states": "US", "usa": "US", "united states of america": "US", "canada": "CA",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "australia": "AU", "germany": "DE", "france": "FR", "spain": "ES", "italy": "IT",
    "netherlands": "NL", "egypt": "EG", "united arab emirates": "AE", "saudi arabia": "SA",
    "mexico": "MX", "brazil": "BR", "india": "IN", "new zealand": "NZ", "ireland": "IE",
}


def _pick(data: dict, keys: list[str]):
    for k in keys:
        v = data.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def clean_name(raw: str) -> str:
    name = _WS.sub(" ", str(raw)).strip()
    return name


def norm_name(name: str) -> str:
    n = _LEGAL_SUFFIX.sub("", name.casefold())
    n = re.sub(r"[^\w\s]", "", n)
    return _WS.sub(" ", n).strip()


def canonical_website(url: str | None) -> tuple[str | None, str | None]:
    """-> (website, domain). Social-platform URLs are not websites (they go to socials)."""
    if not url:
        return None, None
    url = url.strip()
    if not url:
        return None, None
    if "://" not in url:
        url = "https://" + url
    if social_network(url):
        return None, None
    parts = urlsplit(url)
    if not parts.netloc:
        return None, None
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS])
    # scheme preserved as-is (never fabricate https on a scraped URL); root path normalized to empty
    website = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))
    return website, apex_domain(parts.netloc)


def to_e164(raw_phone: str | None, region: str) -> tuple[str | None, str | None]:
    """-> (e164 or None, raw). Only valid numbers become E.164 (docs/03 §3)."""
    if not raw_phone:
        return None, None
    raw = str(raw_phone).strip()
    try:
        parsed = phonenumbers.parse(raw, region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), raw
    except phonenumbers.NumberParseException:
        pass
    return None, raw


def split_address(data: dict, address_full: str | None, region: str) -> dict:
    """Prefer the provider's structured complete_address; fall back to pyap/usaddress parsing."""
    out = {"address_street": None, "address_city": None, "address_region": None,
           "address_postal": None, "address_country": None}
    comp = data.get("complete_address")
    if isinstance(comp, dict) and any(comp.values()):
        street = " ".join(x for x in [comp.get("borough"), comp.get("street")] if x) or comp.get("street")
        out.update(
            address_street=street or None,
            address_city=comp.get("city") or None,
            address_region=comp.get("state") or None,
            address_postal=comp.get("postal_code") or None,
            address_country=comp.get("country") or None,
        )
        return out
    if not address_full:
        return out
    try:  # optional better US splitter
        import usaddress  # type: ignore

        tagged, _ = usaddress.tag(address_full)
        out.update(
            address_street=" ".join(v for k, v in tagged.items() if k.startswith(("AddressNumber", "StreetName"))) or None,
            address_city=tagged.get("PlaceName"),
            address_region=tagged.get("StateName"),
            address_postal=tagged.get("ZipCode"),
        )
        return out
    except Exception:  # noqa: BLE001 — usaddress absent or RepeatedLabelError: fall through to pyap
        pass
    try:
        import pyap

        country = {"US": "US", "CA": "CA", "GB": "GB"}.get(region, "US")
        found = pyap.parse(address_full, country=country)
        if found:
            a = found[0]
            out.update(
                address_street=getattr(a, "street_address", None) or None,
                address_city=getattr(a, "city", None) or None,
                address_region=getattr(a, "region1", None) or None,
                address_postal=getattr(a, "postal_code", None) or None,
            )
    except Exception:  # noqa: BLE001 — parsing is best-effort; full string still exported
        pass
    return out


def map_category(raw_cat: str | None, raw_cats, icp: ICP | None) -> tuple[str | None, list[str]]:
    cats: list[str] = []
    if isinstance(raw_cats, list):
        cats = [str(c).strip() for c in raw_cats if str(c).strip()]
    if raw_cat and str(raw_cat).strip() and str(raw_cat).strip() not in cats:
        cats.insert(0, str(raw_cat).strip())
    primary = cats[0] if cats else None
    if icp:
        wanted = [w.casefold() for w in icp.target.categories]
        for c in cats:  # exact/containment match against ICP taxonomy wins
            cf = c.casefold()
            for w in wanted:
                if w == cf or w in cf or cf in w:
                    return c, cats
    return primary, cats


def region_for(data_country: str | None, default_region: str) -> str:
    if data_country:
        return COUNTRY_TO_REGION.get(str(data_country).strip().casefold(), default_region)
    return default_region


def to_business(raw: RawListing, run_id: str, icp: ICP | None, default_region: str = "US") -> Business | None:
    d = raw.data
    from leadforge.providers.base import get_field_map
    g = {**GOSOM_FIELD_MAP, **(get_field_map(raw.provider) or {})}  # v0.3: per-provider field names
    name = _pick(d, g["name"]) or d.get("name")
    if not name:
        return None
    name = clean_name(name)
    addr_split_pre = split_address(d, None, default_region)
    country = addr_split_pre["address_country"] or _pick(d, ["country"])
    region = region_for(country, default_region)

    address_full = _pick(d, g["address"]) or None
    addr = split_address(d, address_full, region)
    website, domain = canonical_website(_pick(d, g["website"]))
    phone_e164, phone_raw = to_e164(_pick(d, g["phone"]), region)
    category, categories = map_category(_pick(d, g["category"]), _pick(d, g["categories"]), icp)
    place_id = _pick(d, g["place_id"])
    maps_url = _pick(d, g["maps_url"]) or (
        f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else None
    )

    nn = norm_name(name)
    if place_id:
        dedupe_key = f"pid:{place_id}"
    else:
        street_city = f"{(addr['address_street'] or address_full or '')}|{addr['address_city'] or ''}".casefold()
        dedupe_key = f"na:{sha1_hex(nn + '|' + street_city)}"

    rating = _pick(d, g["rating"])
    review_count = _pick(d, g["review_count"])
    hours = _pick(d, g["hours"])
    return Business(
        id=f"biz_{sha1_hex(dedupe_key)}",
        place_id=str(place_id) if place_id else None,
        cid=str(_pick(d, g["cid"])) if _pick(d, g["cid"]) else None,
        name=name,
        name_norm=nn,
        category=category,
        categories=categories,
        website=website,
        domain=domain,
        phone_e164=phone_e164,
        phone_raw=phone_raw,
        address_full=address_full,
        address_street=addr["address_street"],
        address_city=addr["address_city"],
        address_region=addr["address_region"],
        address_postal=addr["address_postal"],
        address_country=addr["address_country"] or country,
        lat=_as_float(_pick(d, g["lat"])),
        lng=_as_float(_pick(d, g["lng"])),
        rating=_as_float(rating),
        review_count=_as_int(review_count),
        hours=hours if isinstance(hours, dict) else None,
        maps_url=maps_url,
        source=raw.provider,
        first_run_id=run_id,
        last_seen_at=now_iso(),
        dedupe_key=dedupe_key,
    )


def _as_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
