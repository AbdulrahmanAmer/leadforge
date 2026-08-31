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


# --- U4.7 GLiNER path (skips when [ner] extra absent) ----------------------------------------
def test_gliner_path_matches_heuristic_quality():
    import pytest as _pytest
    _pytest.importorskip("gliner")
    from leadforge.enrich.extract import extract_people, extract_people_ner
    text = "About us. Jane Doe, Owner. Contact John Smith — Managing Director — for quotes."
    heur = extract_people(text, "http://x")
    ner = extract_people_ner(text, "http://x")
    assert len(ner) >= len(heur)
    assert all(len(c.snippet) <= 300 for c in ner)


def test_ner_available_is_bool():
    from leadforge.enrich.extract import ner_available
    assert ner_available() in (True, False)


# --- U8.1 extractor edge cases ---------------------------------------------------------------
def test_html_entity_encoded_email():
    from leadforge.enrich.extract import extract_emails
    html = "<p>Write to &#109;ail&#64;x.com for info</p>"
    text = "Write to mail@x.com for info"
    out = extract_emails(html, text)
    assert "mail@x.com" in out


def test_two_cfemail_spans_on_one_page():
    from leadforge.enrich.extract import decode_cfemail, extract_emails
    # cfemail encoding: first byte is the XOR key
    def enc(addr, key=0x42):
        return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in addr)
    a, b = enc("one@shop.test"), enc("two@shop.test")
    assert decode_cfemail(a) == "one@shop.test"
    html = (f'<span class="__cf_email__" data-cfemail="{a}"></span>'
            f'<span class="__cf_email__" data-cfemail="{b}"></span>')
    out = extract_emails(html, "")
    assert "one@shop.test" in out and "two@shop.test" in out


def test_uk_phone_with_gb_region():
    from leadforge.enrich.extract import extract_phones
    text = "Call us on 020 7946 0958 today"
    out = extract_phones("", text, "GB")
    assert any(p.startswith("+44") for p in out)


def test_stopword_name_yields_no_candidate():
    from leadforge.enrich.extract import extract_people
    text = "Our Owner And The Team are here to help."
    assert extract_people(text, "http://x") == []
