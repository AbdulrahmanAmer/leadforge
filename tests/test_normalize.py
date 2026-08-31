from leadforge.models import RawListing
from leadforge.normalize import (
    canonical_website,
    map_category,
    norm_name,
    to_business,
    to_e164,
)
from leadforge.util import now_iso


def test_norm_name_strips_legal_suffix():
    assert norm_name("Joe's Auto Repair, LLC") == "joes auto repair"
    assert norm_name("ACME Inc.") == "acme"


def test_canonical_website_drops_socials_and_tracking():
    # scheme preserved (we never fabricate https), tracking params dropped, host lowercased
    w, d = canonical_website("http://WWW.Foo.com/path/?utm_source=x&id=1")
    assert w == "http://www.foo.com/path?id=1"
    assert d == "foo.com"
    assert canonical_website("https://facebook.com/foo") == (None, None)


def test_canonical_website_cc_sld():
    _, d = canonical_website("https://shop.bar.co.uk")
    assert d == "bar.co.uk"


def test_to_e164_valid_and_invalid():
    e164, raw = to_e164("(713) 555-0100", "US")
    assert e164 == "+17135550100"
    none, raw2 = to_e164("not a phone", "US")
    assert none is None and raw2 == "not a phone"


def test_map_category_prefers_icp(sample_icp):
    cat, cats = map_category("Car Repair", ["Auto Repair Shop", "Car Repair"], sample_icp)
    assert cat == "Auto Repair Shop"


def test_to_business_full_row(sample_icp):
    raw = RawListing(provider="gosom", fetched_at=now_iso(), data={
        "title": "Joe's Transmission LLC",
        "category": "Auto repair shop",
        "categories": ["Auto repair shop", "Transmission shop"],
        "address": "123 Main St, Houston, TX 77002",
        "complete_address": {"street": "123 Main St", "city": "Houston", "state": "TX",
                             "postal_code": "77002", "country": "United States"},
        "phone": "713-555-0100",
        "web_site": "http://joestransmission.com?utm_medium=maps",
        "review_rating": "4.6", "review_count": "128",
        "latitude": 29.76, "longitude": -95.36,
        "place_id": "ChIJxyz",
    })
    b = to_business(raw, "run_1", sample_icp, "US")
    assert b.name == "Joe's Transmission LLC"
    assert b.name_norm == "joes transmission"
    assert b.phone_e164 == "+17135550100"
    assert b.website == "http://joestransmission.com"
    assert b.domain == "joestransmission.com"
    assert b.address_city == "Houston" and b.address_postal == "77002"
    assert b.rating == 4.6 and b.review_count == 128
    assert b.place_id == "ChIJxyz" and b.dedupe_key == "pid:ChIJxyz"
    assert b.category == "Auto repair shop"


def test_to_business_dedupe_without_place_id(sample_icp):
    raw = RawListing(provider="gosom", fetched_at=now_iso(), data={
        "title": "No PlaceID Shop", "address": "9 Oak Ave, Houston, TX", "phone": "713-555-0199",
    })
    b = to_business(raw, "r", sample_icp, "US")
    assert b.dedupe_key.startswith("na:")
