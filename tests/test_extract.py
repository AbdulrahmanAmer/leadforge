from leadforge.enrich.extract import (
    decode_cfemail,
    extract_emails,
    extract_people,
    extract_phones,
    extract_socials,
)


def test_plain_and_mailto_email():
    html = '<a href="mailto:owner@shop.com">mail</a> or reach info@shop.com'
    emails = extract_emails(html, "reach info@shop.com")
    assert emails["owner@shop.com"] == "personal"
    assert emails["info@shop.com"] == "role"


def test_at_dot_obfuscation():
    text = "Contact: jane (at) example-shop (dot) com for details"
    emails = extract_emails("", text)
    assert "jane@example-shop.com" in emails


def test_cfemail_decode_roundtrip():
    # encode 'a@b.com' with key 0x7a
    email = "a@b.com"
    key = 0x7a
    encoded = bytes([key]) + bytes(ord(c) ^ key for c in email)
    hexstr = encoded.hex()
    assert decode_cfemail(hexstr) == email
    html = f'<span data-cfemail="{hexstr}">[email protected]</span>'
    assert email in extract_emails(html, "")


def test_junk_emails_filtered():
    emails = extract_emails("", "logo@2x.png user@example.com real@shop.com")
    assert "real@shop.com" in emails
    assert not any("example.com" in e for e in emails)


def test_extract_phones_us():
    phones = extract_phones('<a href="tel:+17135550100">call</a>', "or 713-555-0199", "US")
    assert "+17135550100" in phones


def test_extract_socials():
    html = '<a href="https://www.linkedin.com/company/foo">li</a><a href="https://x.com/foo?ref=1">x</a>'
    socials = extract_socials(html)
    assert socials["linkedin"].endswith("/company/foo")
    assert socials["x"] == "https://x.com/foo"


def test_extract_people_title_name():
    text = "Our founder Joe Alvarez started the shop in 2004. Maria Chen, Office Manager, runs the desk."
    people = extract_people(text, "https://shop.com/about")
    names = {p.name for p in people}
    assert "Joe Alvarez" in names or "Maria Chen" in names
    for p in people:
        assert len(p.snippet) <= 300 and p.source_url.endswith("/about")
