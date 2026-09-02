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

# A5 (docs/09): GBP owner-reply signatures and review-credited names. Case-sensitive on purpose —
# only an actually-capitalised token is treated as a candidate first name.
_SIGNOFF_RE = re.compile(
    r"(?:[Tt]hanks|[Tt]hank you|[Rr]egards|[Cc]heers|(?:[Bb]est |[Ww]arm )?[Ww]ishes)[,]?\s+([A-Z][a-zA-Z']+)"
)
_SIGNOFF_STOP = {
    "team", "thanks", "thank", "regards", "regard", "kind", "best", "warm", "the", "staff", "service", "you",
    "customer", "customers", "car", "cars", "ms", "mr", "mrs", "dr", "mot", "again", "everyone", "all",
    "guys", "sir", "madam",
}
_REVIEW_NAME_RE = re.compile(
    r"\b([A-Z][a-zA-Z']+)\s+(?:and his|and the|is|was|the owner|who|did|sorted|fixed|helped|looked)\b"
)
_REVIEW_NAME_STOP = {
    "it", "this", "that", "they", "there", "here", "he", "she", "we", "you", "i", "team", "staff",
    "service", "car", "garage", "work", "nothing", "everything", "cash", "all", "very", "great",
    "excellent", "highly", "guys", "guy", "lady", "man", "woman", "friendly", "professional",
    "amazing", "brilliant", "thanks", "thank", "mot", "customer", "customers", "everyone",
}

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


def _appointments_from_about(about) -> str:
    """"about" is gosom's Google Business Profile attribute list: [{id,name,options:[{name,enabled}]}].
    The 'Planning' group's option text ("Appointments recommended"/"Appointment required") is the
    only signal for this; anything else defaults to 'none' rather than guessing.

    Measured on the live campaign's raw cache (848 places): ~8% of places list BOTH options as true
    at once (Google shows both chips) — 'required' is checked first and wins as the stricter, more
    informative claim (every place that requires an appointment can also, trivially, be described as
    one where appointments are recommended, but not the reverse)."""
    if not isinstance(about, list):
        return "none"
    for grp in about:
        if not isinstance(grp, dict):
            continue
        gid = str(grp.get("id") or grp.get("name") or "").casefold()
        if "planning" not in gid and "appointment" not in gid:
            continue
        for opt in grp.get("options") or []:
            opt = opt or {}
            if opt.get("enabled") is False:  # explicit False only — absent/None still counts (gosom's default)
                continue
            label = str(opt.get("name") or "").casefold()
            if "required" in label:
                return "required"
            if "recommended" in label:
                return "recommended"
    return "none"


def _booking_links_from_order_online(order_online) -> list[str]:
    """order_online: [{"link": url, "source": host}, ...]; a wa.me link is a WhatsApp booking channel
    but the url itself is kept exactly as scraped either way."""
    if not isinstance(order_online, list):
        return []
    return [str(e["link"]) for e in order_online if isinstance(e, dict) and e.get("link")]


def _name_token_set(business_name: str) -> set[str]:
    return {t.casefold() for t in re.findall(r"[A-Za-z']+", business_name or "")}


def _is_plausible_name(name: str, name_tokens: set[str]) -> bool:
    """Reject SHOUTY acronyms ('MS', 'MOT' — matched by the regex because it allows any-case letters
    after the first) and a word that is just a token of the business's own name ('Car' from 'XYZ Car
    Care', 'Customer' where the stoplist alone missed a variant) — review noise the GBP side must not
    re-create (docs/09 A5 review, major)."""
    if len(name) > 1 and name == name.upper():
        return False
    if name.casefold() in name_tokens:
        return False
    return True


def _reply_signatures(reviews: list, business_name: str = "") -> list[str]:
    """First names an owner signed their review replies with (reply_text_original), e.g. 'Thanks Sam' —
    the LAST sign-off in a reply is kept (sign-offs sit at the end, so requiring it near the end of the
    text avoids matching a sign-off word used mid-sentence earlier in a long reply); one entry per
    distinct name."""
    name_tokens = _name_token_set(business_name)
    names: list[str] = []
    for r in reviews:
        if not isinstance(r, dict):
            continue
        text = str(r.get("reply_text_original") or "")
        if not text:
            continue
        matches = list(_SIGNOFF_RE.finditer(text))
        if not matches:
            continue
        last = matches[-1]
        if len(text) - last.start() > 40:  # a real sign-off sits at the tail of the reply
            continue
        name = last.group(1)
        if name.casefold() in _SIGNOFF_STOP or not _is_plausible_name(name, name_tokens):
            continue
        if name not in names:
            names.append(name)
    return names


def _review_credited_names(reviews: list, business_name: str = "") -> list[str]:
    """First names a reviewer credits ('Ali the owner', 'Sam sorted...'), kept only when they show up
    in >= 3 DISTINCT reviews — a name mentioned once or twice is more likely a sentence-initial common
    word (It was.../This is...) than a real credit."""
    name_tokens = _name_token_set(business_name)
    counts: dict[str, int] = {}
    for r in reviews:
        if not isinstance(r, dict):
            continue
        text = str(r.get("Description") or r.get("description") or r.get("text_original") or "")
        if not text:
            continue
        seen_in_review: set[str] = set()
        for m in _REVIEW_NAME_RE.finditer(text):
            name = m.group(1)
            if name.casefold() not in _REVIEW_NAME_STOP and _is_plausible_name(name, name_tokens):
                seen_in_review.add(name)
        for name in seen_in_review:
            counts[name] = counts.get(name, 0) + 1
    return [n for n, c in counts.items() if c >= 3]


def gbp_facts(d: dict, g: dict, business_name: str = "") -> dict:
    """Business.enrich["gbp"] (A5, docs/09): Google Business Profile facts the raw provider payload
    already carries. Empty defaults, never None, so export/scoring never has to null-check this."""
    about = _pick(d, g["about"])
    order_online = _pick(d, g["order_online"])
    reviews = _pick(d, g["reviews"])
    reviews = reviews if isinstance(reviews, list) else []
    status = _pick(d, g["status"]) or ""
    owner = _pick(d, g["owner"])
    owner_name = owner.get("name") if isinstance(owner, dict) else ""
    description = _pick(d, g["description"]) or ""
    return {
        "appointments": _appointments_from_about(about),
        "booking_links": _booking_links_from_order_online(order_online),
        "status": str(status),
        "owner_name": str(owner_name or ""),
        "reply_signatures": _reply_signatures(reviews, business_name),
        "review_names": _review_credited_names(reviews, business_name),
        "reviews_captured": len(reviews),
        "description": str(description)[:300],
    }


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
        enrich={"gbp": gbp_facts(d, g, name)},
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
