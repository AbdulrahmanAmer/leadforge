"""v0.3 unit C1 — extraction truth. docs/09-v0.3-build-plan.md.

Fixture cases mirror the live-report findings: a template-credit gmail buried in a <style> block, a
booking-widget config array buried in <script>, "Book A Service"/"Book an MOT" anchors, a bookmygarage
iframe, a Google-reviews block that must not surface as a team candidate, a real team block that must,
the classify_email_affinity truth table, and rank_email_contacts' ordering guarantee.
"""

import sqlite3

from leadforge.enrich.crawler import Page, SiteCrawler
from leadforge.enrich.extract import (
    JUNK_LOCALPARTS,
    PersonCandidate,
    classify_email_affinity,
    email_context,
    extract_emails,
    extract_people,
)
from leadforge.enrich.validate import TIER_ORDER, rank_email_contacts


# --- noise stripping: <style>/<script>/<noscript>/<template> content never reaches the email regex ----
def test_style_block_credit_email_not_extracted():
    html = (
        "<html><head><style>\n"
        "/* Template by Andrea Impallari - impallari@gmail.com */\n"
        ".header { color: red; }\n"
        "</style></head><body><p>Welcome to Abbey Service</p></body></html>"
    )
    out = extract_emails(html, "Welcome to Abbey Service")
    assert "impallari@gmail.com" not in out, f"style-block email leaked: {out}"


def test_script_array_emails_not_extracted():
    html = (
        '<html><body><script>\n'
        'var testUsers = ["chenowith52@gmail.com", "test@test.com", "test@gmail.com"];\n'
        '</script><p>Call our shop</p></body></html>'
    )
    out = extract_emails(html, "Call our shop")
    assert out == {}, f"script-block emails leaked: {out}"


def test_mailto_still_read_despite_noise_stripping():
    """mailto: hrefs come from the FULL document — a <style>/<script> block elsewhere must never
    suppress a real, published mailto contact point."""
    html = (
        "<html><head><style>.x{color:blue}</style></head>"
        '<body><a href="mailto:owner@shop.com">Email us</a></body></html>'
    )
    out = extract_emails(html, "")
    assert out["owner@shop.com"] == "personal"


def test_cfemail_still_read_despite_noise_stripping():
    def enc(addr, key=0x33):
        return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in addr)
    html = (
        "<html><head><script>var x = 1;</script></head>"
        f'<body><span data-cfemail="{enc("real@shop.com")}"></span></body></html>'
    )
    out = extract_emails(html, "")
    assert "real@shop.com" in out


# --- JUNK_LOCALPARTS dropped at extraction (not just at validation) ------------------------------------
def test_junk_localparts_dropped_at_extraction():
    html = '<a href="mailto:noreply@shop.com">x</a>'
    text = "contact test@shop.com or sample@shop.com or real@shop.com"
    out = extract_emails(html, text)
    assert "real@shop.com" in out
    assert "noreply@shop.com" not in out
    assert "test@shop.com" not in out
    assert "sample@shop.com" not in out


# --- booking_hint: broadened word-distance regex + platform detection ---------------------------------
def _pages(html: str) -> list[Page]:
    text = SiteCrawler.extract_text(html)
    return [Page(url="https://shop.example/", html=html, text=text)]


def test_booking_hint_book_a_service_anchor():
    html = '<html><body><a href="/booking">Book A Service</a></body></html>'
    signals = SiteCrawler.compute_signals(_pages(html), stale_after_years=3)
    assert signals["booking_hint"] is True
    assert signals["booking_source"] == "regex"


def test_booking_hint_book_an_mot_anchor():
    html = '<html><body><a href="/booking">Book an MOT</a></body></html>'
    signals = SiteCrawler.compute_signals(_pages(html), stale_after_years=3)
    assert signals["booking_hint"] is True
    assert signals["booking_source"] == "regex"


def test_booking_hint_bookmygarage_iframe_is_platform_source():
    html = (
        '<html><body><p>Welcome</p>'
        '<iframe src="https://www.bookmygarage.com/widget/abc123"></iframe>'
        "</body></html>"
    )
    signals = SiteCrawler.compute_signals(_pages(html), stale_after_years=3)
    assert signals["booking_hint"] is True
    assert signals["booking_source"] == "platform:bookmygarage"


def test_booking_hint_false_when_no_booking_language():
    html = "<html><body><p>We fix cars. Call us today.</p></body></html>"
    signals = SiteCrawler.compute_signals(_pages(html), stale_after_years=3)
    assert signals["booking_hint"] is False
    assert "booking_source" not in signals


def test_booking_hint_word_distance_bounded_at_four_words():
    """"book" and a target word more than 4 words apart must NOT trigger — otherwise any page
    mentioning both "book" and "service" anywhere would false-positive."""
    html = ("<html><body><p>Please book early, we get very busy, so call ahead of your visit "
            "before we can help with your car service needs.</p></body></html>")
    signals = SiteCrawler.compute_signals(_pages(html), stale_after_years=3)
    assert signals["booking_hint"] is False


# --- people extraction: review/testimonial noise suppressed, team blocks kept ---------------------------
def test_reviews_block_yields_no_candidate():
    text = ("Customer Reviews. Catalina Campbell — General Manager, 5 stars ★★★★★, "
            "2 weeks ago. I took my car in and they fixed it fast, would recommend on Google!")
    people = extract_people(text, "https://shop.example/")
    assert people == [], f"review noise produced a candidate: {people}"


def test_team_block_yields_candidate_with_team_context():
    text = "Our Team. Paul Smith — Owner. Paul started the shop in 2004 and still runs the front desk."
    people = extract_people(text, "https://shop.example/team")
    assert len(people) == 1
    assert people[0].name == "Paul Smith"
    assert people[0].context == "team"


def test_non_team_page_defaults_to_other_context():
    text = "In the press: Jane Doe, Managing Director, spoke to the local paper about growth plans."
    people = extract_people(text, "https://shop.example/news")
    assert len(people) == 1
    assert people[0].context == "other"


def test_person_candidate_context_defaults_to_other():
    c = PersonCandidate(name="Jane Doe", title="Owner", snippet="s", source_url="https://x")
    assert c.context == "other"


def test_form_labels_never_read_as_a_name():
    """A title word ("Manager") sitting right before a contact form's stacked field labels — a real
    false-positive shape: the labels satisfy NAME_RE's two-capitalized-words test and are the nearest
    thing to the title word, so without the stoplist entries "Name Email Message" reads as a name."""
    text = "Reach the Manager here. Name Email Message Phone Subject. We reply within 24 hours."
    people = extract_people(text, "https://shop.example/contact")
    assert people == [], f"form labels produced a candidate: {people}"


# --- classify_email_affinity truth table ---------------------------------------------------------------
def test_affinity_freemail_unlinked_template_credit():
    assert classify_email_affinity("impallari@gmail.com", "abbeyservice.co.uk", "abbeyservice") == "freemail_unlinked"


def test_affinity_freemail_linked_last_comma_first():
    assert classify_email_affinity("johnhoggarth@live.co.uk", "hoggarthmotors.co.uk", "Hoggarth Motors",
                                    people_names=["Hoggarth, John"]) == "freemail_linked"


def test_affinity_freemail_linked_two_char_digit_token():
    """'a1' is only 2 chars but contains a digit, so it counts as a significant token — the alternative
    (requiring 3+ chars always) would miss a real, common UK trade-name pattern."""
    assert classify_email_affinity("a1autoserviceplus@hotmail.com", "a1carbodyrepair.co.uk",
                                    "A1 Car Body Repair") == "freemail_linked"


def test_affinity_own_domain():
    assert classify_email_affinity("info@own.co.uk", "own.co.uk", "Own Shop") == "own_domain"


def test_affinity_foreign():
    assert classify_email_affinity("someone@otherbiz.com", "mybiz.co.uk", "My Biz") == "foreign"


def test_affinity_apostrophe_folding():
    """"o'brien" in the business name must match a freemail local part containing "obrien" (local parts
    never carry punctuation) — the apostrophe is folded away, not treated as a word separator."""
    assert classify_email_affinity("obrienauto99@gmail.com", "somewhere.co.uk", "O'Brien Auto Repair") == "freemail_linked"


def test_affinity_hyphen_folding_joins_pieces_too_short_alone():
    """A discriminating case: business_name_norm is ONLY "Jo-El" (no other word to accidentally match
    on its own). Split-without-folding gives "jo"/"el", each 2 chars with no digit — neither qualifies
    as a token, and the 2-char initials "je" don't match either. Only the FOLDED "joel" (4 chars) is a
    significant token, so this can only match when the hyphen is joined, not treated as a separator."""
    assert classify_email_affinity("joelrepairs@gmail.com", "somewhere.co.uk", "Jo-El") == "freemail_linked"


# --- rank_email_contacts: ordering + dict/sqlite3.Row compatibility ------------------------------------
def _row(conn, **fields):
    keys = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"CREATE TABLE IF NOT EXISTS t ({keys})")
    conn.execute(f"INSERT INTO t ({keys}) VALUES ({placeholders})", list(fields.values()))
    return conn.execute("SELECT * FROM t WHERE rowid = last_insert_rowid()").fetchone()


def test_rank_email_contacts_ordering_dicts():
    contacts = [
        {"kind": "email", "value": "guess@shop.com", "tier": "inferred", "affinity": ""},
        {"kind": "email", "value": "owner@gmail.com", "tier": "valid", "affinity": "freemail_linked"},
        {"kind": "email", "value": "info@shop.com", "tier": "role", "affinity": "own_domain"},
        {"kind": "email", "value": "sales@shop.com", "tier": "valid", "affinity": "own_domain"},
    ]
    ranked = [c["value"] for c in rank_email_contacts(contacts)]
    assert ranked == ["sales@shop.com", "info@shop.com", "owner@gmail.com", "guess@shop.com"], ranked


def test_rank_email_contacts_own_domain_role_above_freemail_valid():
    contacts = [
        {"kind": "email", "value": "freemail@gmail.com", "tier": "valid", "affinity": "freemail_linked"},
        {"kind": "email", "value": "info@shop.com", "tier": "role", "affinity": "own_domain"},
    ]
    ranked = [c["value"] for c in rank_email_contacts(contacts)]
    assert ranked[0] == "info@shop.com", "own-domain role must outrank freemail valid"


def test_rank_email_contacts_inferred_ranks_below_own_domain_and_freemail_linked():
    contacts = [
        {"kind": "email", "value": "guess@shop.com", "tier": "inferred", "affinity": ""},
        {"kind": "email", "value": "info@shop.com", "tier": "role", "affinity": "own_domain"},
        {"kind": "email", "value": "owner@gmail.com", "tier": "valid", "affinity": "freemail_linked"},
    ]
    ranked = [c["value"] for c in rank_email_contacts(contacts)]
    assert ranked.index("guess@shop.com") > ranked.index("info@shop.com")
    assert ranked.index("guess@shop.com") > ranked.index("owner@gmail.com")


def test_rank_email_contacts_works_on_sqlite3_row():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row_a = _row(conn, kind="email", value="info@shop.com", tier="role", affinity="own_domain")
    row_b = _row(conn, kind="email", value="owner@gmail.com", tier="valid", affinity="freemail_linked")
    ranked = [r["value"] for r in rank_email_contacts([row_b, row_a])]
    assert ranked == ["info@shop.com", "owner@gmail.com"]
    conn.close()


def test_rank_email_contacts_missing_affinity_column_treated_as_empty():
    """A contact from a pre-v0.3 DB row lacks 'affinity' entirely — must not KeyError, must sort as ''."""
    contacts = [
        {"kind": "email", "value": "legacy@shop.com", "tier": "valid"},  # no 'affinity' key at all
        {"kind": "email", "value": "info@shop.com", "tier": "role", "affinity": "own_domain"},
    ]
    ranked = [c["value"] for c in rank_email_contacts(contacts)]
    assert ranked[0] == "info@shop.com"
    assert "legacy@shop.com" in ranked


def test_tier_order_never_ranks_inferred_above_an_observed_tier():
    assert TIER_ORDER.index("inferred") > TIER_ORDER.index("valid")
    assert TIER_ORDER.index("inferred") > TIER_ORDER.index("role")
    assert TIER_ORDER.index("inferred") > TIER_ORDER.index("risky")
    assert TIER_ORDER.index("inferred") > TIER_ORDER.index("catch_all")


# --- validate_email: placeholder localpart rejected before any DNS call --------------------------------
def test_validate_email_placeholder_localpart_never_hits_dns(tmp_path, monkeypatch):
    from leadforge.config import load_config
    from leadforge.enrich import validate as validate_mod

    def _boom(*a, **k):
        raise AssertionError("DNS was called for a placeholder localpart")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(validate_mod, "_mx_cached", _boom)
    cfg = load_config(tmp_path)
    for junk in ("test", "noreply", "sample"):
        tier, meta = validate_mod.validate_email(f"{junk}@example-shop.com", "personal", cfg)
        assert tier == "invalid"
        assert meta["reason"] == "placeholder_localpart"


def test_junk_localparts_constant_matches_extraction_use():
    assert "test" in JUNK_LOCALPARTS and "noreply" in JUNK_LOCALPARTS


# --- email_context ---------------------------------------------------------------------------------
def test_email_context_returns_collapsed_window():
    text = "Reach   the   shop\nat   owner@shop.com   for   quotes any time"
    ctx = email_context(text, "owner@shop.com", window=10)
    assert "owner@shop.com" in ctx
    assert "\n" not in ctx and "  " not in ctx


def test_email_context_missing_address_returns_address_itself():
    assert email_context("nothing relevant here", "ghost@shop.com") == "ghost@shop.com"


# --- crawler signals: final_host / offsite_redirect / http_status --------------------------------------
def test_crawler_result_signal_keys_documented():
    """compute_signals' return only carries the documented keys — a stray key would be silent scope
    creep no downstream consumer expects."""
    html = "<html><body>hello</body></html>"
    signals = SiteCrawler.compute_signals(_pages(html), stale_after_years=3)
    assert set(signals) <= {"copyright_year", "stale_site", "careers", "booking_hint", "booking_source"}
