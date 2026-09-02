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


def test_unclosed_script_content_does_not_leak_an_email():
    """A malformed/truncated document with a <script> that has no matching </script> — a real HTML
    parser (or a browser) implicitly closes it at end-of-document. v0.3 fix: noise stripping now
    decomposes the <script>/<style>/<noscript>/<template> nodes via the selectolax parser itself
    (_strip_noise_elements) rather than a hand-rolled closing-tag regex, so this "boundary at EOF"
    behavior comes for free from the parser instead of needing a bespoke `(?:</tag>|\\Z)` fallback."""
    html = (
        "<html><body><p>Contact real@shop.com for a quote.</p>"
        '<script>var config = {"support": "leak@gmail.com"};'
        # deliberately no closing </script> tag
    )
    out = extract_emails(html, "Contact real@shop.com for a quote.")
    assert "leak@gmail.com" not in out, f"unclosed <script> content leaked: {out}"
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


def test_booking_hint_nav_anchor_survives_trafilatura_boilerplate_stripping():
    """v0.3 fix regression test: trafilatura treats nav/header anchors as boilerplate and strips them
    from Page.text — on a document with enough real paragraph content that trafilatura's extraction
    is the ACTIVE path (not the selectolax fallback), a "Book Online" nav anchor must still be found.
    Before the fix, compute_signals ran the word-distance regex over Page.text and this went False."""
    html = (
        '<html><body><nav><a href="/booking">Book Online</a></nav><main>'
        "<p>We are a family-run garage in the heart of town, serving the local community for over "
        "twenty years with honest, reliable car repair and servicing. Our fully qualified technicians "
        "handle everything from routine maintenance to major mechanical work, and we pride ourselves "
        "on clear communication and fair pricing for every customer who walks through our doors.</p>"
        "<p>Whether you need an MOT test, a full service, brake repairs, or diagnostic work, our team "
        "has the experience and equipment to get the job done right the first time, every time, with "
        "a genuine commitment to customer satisfaction that keeps people coming back year after "
        "year.</p></main></body></html>"
    )
    pages = _pages(html)
    assert len(pages[0].text) >= 400, "fixture must be long enough that trafilatura is the active path"
    assert "book" not in pages[0].text.casefold(), "trafilatura must have actually dropped the nav anchor"
    signals = SiteCrawler.compute_signals(pages, stale_after_years=3)
    assert signals["booking_hint"] is True
    assert signals["booking_source"] == "regex"


# unit C2's reviewer, on booking on REAL crawled HTML: the fixture above proves the mechanism (one bare
# nav anchor) but is thinner than an actual crawled site — a real crawl hands compute_signals full site
# chrome (header nav with SEVERAL links, a footer) across MULTIPLE pages, the way SiteCrawler.crawl()
# actually assembles result.pages. This fixture matches that shape: two pages, each with a header nav
# containing "Book Online" alongside unrelated nav links (Services/About/Contact), real body copy long
# enough that trafilatura is the active extraction path on both pages, and a footer — proving booking_hint
# still reads the nav anchor from the raw HTML, not from trafilatura's boilerplate-stripped Page.text,
# on something closer to what actually comes back from a live site.
def test_booking_hint_realistic_multi_page_site_nav():
    home_html = (
        "<html><body>"
        "<header><nav>"
        '<a href="/services">Services</a> '
        '<a href="/about">About</a> '
        '<a href="/booking">Book Online</a> '
        '<a href="/contact">Contact</a>'
        "</nav></header>"
        "<main><p>We are a family-run garage in the heart of town, serving the local community for over "
        "twenty years with honest, reliable car repair and servicing. Our fully qualified technicians "
        "handle everything from routine maintenance to major mechanical work, and take pride in clear "
        "communication and fair pricing for every single customer who walks through our doors each day.</p>"
        "<p>Whether it is a routine oil change, a full brake overhaul, or a pre-purchase inspection, our "
        "team has the tools and the training to get the job done right the first time, every time, with "
        "a genuine commitment to keeping local drivers safely on the road.</p></main>"
        "<footer><p>Copyright 2024 Example Garage. All rights reserved.</p></footer>"
        "</body></html>"
    )
    about_html = (
        "<html><body>"
        "<header><nav>"
        '<a href="/services">Services</a> '
        '<a href="/about">About</a> '
        '<a href="/booking">Book Online</a> '
        '<a href="/contact">Contact</a>'
        "</nav></header>"
        "<main><p>Our story began decades ago when the founder opened the doors to serve local drivers "
        "with fair prices and expert workmanship, a tradition the whole team continues to this day with "
        "the same care and attention to detail that built the shop's reputation in the first place.</p>"
        "</main></body></html>"
    )
    pages = [
        Page(url="https://shop.example/", html=home_html, text=SiteCrawler.extract_text(home_html)),
        Page(url="https://shop.example/about", html=about_html, text=SiteCrawler.extract_text(about_html)),
    ]
    for p in pages:
        assert "book" not in p.text.casefold(), (
            f"trafilatura must have dropped the nav anchor from {p.url}'s Page.text for this fixture "
            "to actually exercise the HTML-derived path, not the (identical either way) text path"
        )
    signals = SiteCrawler.compute_signals(pages, stale_after_years=3)
    assert signals["booking_hint"] is True
    assert signals["booking_source"] == "regex"


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


def test_company_history_years_ago_does_not_suppress_a_confirmed_owner():
    """v0.3 fix: "N years ago" in a company-history paragraph is not review-shaped and must not trip
    the 'ago' marker (which is now restricted to days/weeks/months, review-timestamp units)."""
    text = "Our Story. Gary Robertson, Owner, started the garage 45 years ago and still runs it today."
    people = extract_people(text, "https://shop.example/about")
    assert len(people) == 1, f"a real owner was suppressed by the loosened 'ago' marker: {people}"
    assert people[0].name == "Gary Robertson"


def test_single_weak_marker_alone_does_not_suppress_a_team_candidate():
    """v0.3 fix: a lone weak marker ("thank you") used to suppress on its own — "Paul says thank you"
    on an otherwise ordinary team page is not review noise without a second co-occurring marker."""
    text = "Meet the Team. Paul Smith, Owner. Paul says thank you to everyone who supports the shop."
    people = extract_people(text, "https://shop.example/team")
    assert len(people) == 1, f"a single weak marker wrongly suppressed a team candidate: {people}"
    assert people[0].name == "Paul Smith"


def test_single_weak_marker_google_alone_does_not_suppress_a_team_candidate():
    text = "Our Team. Jane Doe - Managing Director. Find us on Google for directions to the workshop."
    people = extract_people(text, "https://shop.example/team")
    assert len(people) == 1, f"a single weak marker wrongly suppressed a team candidate: {people}"
    assert people[0].name == "Jane Doe"


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


# --- extract_people_ner: same review-noise skip + context='team' as extract_people --------------------
# GLiNER (the [ner] extra) is not installed on this machine (test_gliner_path_matches_heuristic_quality
# above skips itself for exactly that reason via importorskip), so the review-noise-skip and
# context='team' behavior of extract_people_ner had NO coverage at all here. This monkeypatches
# extract._gliner_model with a stub that returns fixed, position-accurate spans — no gliner install
# required — so the two v0.3 behaviors (shared with extract_people via _is_review_noise/_context_for)
# are proven on the NER code path itself, not just inferred from the heuristic path's tests.
def test_ner_path_applies_review_noise_skip_and_team_context(monkeypatch):
    from leadforge.enrich import extract as extract_mod

    # The review block and the team block are kept > 120 chars apart (the +-120-char window
    # extract_people_ner checks around each NAME span) with neutral filler text between them, so the
    # two candidates' windows don't bleed into each other — Paul's window must stay noise-free on its
    # own merits, not merely because it is short enough to dodge the review markers by accident.
    text = (
        "Customer Reviews. Catalina Campbell rated us 5 stars ★★★★★, 2 weeks ago, "
        "and said she would recommend us on Google. "
        "We are a family-run garage that has served this community for many years with honest pricing "
        "and clear communication on every job, large or small, and we always aim to get your vehicle "
        "back to you as quickly as safely possible without cutting any corners on quality. "
        "Our Team. Paul Smith, Owner, runs the front desk and has done for years."
    )

    class _StubGlinerModel:
        """Minimal stand-in for the real GLiNER model: same predict_entities(text, labels, threshold)
        shape, fixed spans computed against the ACTUAL text passed in (so window slicing in
        extract_people_ner lines up exactly like it would against a real model's output)."""

        def predict_entities(self, scan_text, labels, threshold=0.4):
            def span(needle):
                i = scan_text.index(needle)
                return i, i + len(needle)

            r_start, r_end = span("Catalina Campbell")
            o_start, o_end = span("Paul Smith")
            t_start, t_end = span("Owner")
            return [
                {"text": "Catalina Campbell", "label": "person name", "start": r_start, "end": r_end},
                {"text": "Paul Smith", "label": "person name", "start": o_start, "end": o_end},
                {"text": "Owner", "label": "job title", "start": t_start, "end": t_end},
            ]

    monkeypatch.setattr(extract_mod, "_gliner_model", lambda: _StubGlinerModel())
    people = extract_mod.extract_people_ner(text, "https://shop.example/team")
    names = {p.name for p in people}
    assert "Catalina Campbell" not in names, f"review noise leaked into the NER path: {people}"
    assert "Paul Smith" in names, f"a real team candidate was lost on the NER path: {people}"
    paul = next(p for p in people if p.name == "Paul Smith")
    assert paul.title == "Owner"
    assert paul.context == "team", f"NER-path candidate did not get context='team': {paul}"


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
    """A discriminating case (mirrors test_affinity_hyphen_folding_joins_pieces_too_short_alone below):
    business_name_norm is ONLY "Jo'El" (ASCII apostrophe, U+0027) — no other word to accidentally match.
    Split-without-folding gives "jo"/"el", each 2 chars with no digit — neither qualifies as a token on
    its own, and the 2-char initials "je" don't match the local part "joelrepairs" either. Only the
    FOLDED "joel" (4 chars) is a significant token, so this can only pass when the apostrophe is folded
    away, not treated as a separator. (The old fixture, "O'Brien Auto Repair" x "obrienauto99", did NOT
    discriminate: "brien" alone — the unfolded second half — already matches as a substring, so it
    passed whether or not folding actually happened.)"""
    assert classify_email_affinity("joelrepairs@gmail.com", "somewhere.co.uk", "Jo'El") == "freemail_linked"


def test_affinity_hyphen_folding_joins_pieces_too_short_alone():
    """A discriminating case: business_name_norm is ONLY "Jo-El" (no other word to accidentally match
    on its own). Split-without-folding gives "jo"/"el", each 2 chars with no digit — neither qualifies
    as a token, and the 2-char initials "je" don't match either. Only the FOLDED "joel" (4 chars) is a
    significant token, so this can only match when the hyphen is joined, not treated as a separator."""
    assert classify_email_affinity("joelrepairs@gmail.com", "somewhere.co.uk", "Jo-El") == "freemail_linked"


def test_affinity_typographic_apostrophe_folding():
    """Same discriminating "Jo?El" shape as test_affinity_apostrophe_folding above, but with the
    typographic right single quote (U+2019) -- what real sites actually emit for names like "O'Brien",
    not the ASCII apostrophe -- written as a \\u escape so this file stays ASCII. Must fold the same way."""
    name = "Jo" + "\u2019" + "El"
    assert classify_email_affinity("joelrepairs@gmail.com", "somewhere.co.uk", name) == "freemail_linked"


def test_affinity_left_typographic_apostrophe_folding():
    """Same discriminating shape again with the typographic LEFT single quote (U+2018) -- less common but
    still seen (curly-quote autocorrect sometimes picks the wrong direction) -- written as a \\u escape."""
    name = "Jo" + "\u2018" + "El"
    assert classify_email_affinity("joelrepairs@gmail.com", "somewhere.co.uk", name) == "freemail_linked"


def test_affinity_generic_stopword_token_does_not_link_unrelated_name():
    """"The Car Garage" -> "the"/"car"/"garage" as raw tokens; "matthew" contains "the" as a bare
    substring but is an unrelated person's own gmail, not a link to this business."""
    assert classify_email_affinity("matthew@gmail.com", "somewhere.co.uk", "The Car Garage") == "freemail_unlinked"


def test_affinity_short_trade_token_still_links_as_bare_substring():
    """Documents a deliberate trade-off, not a gap: a stricter "token must sit at a local-part word
    boundary" rule was tried to close the ("oscar" contains "car") collision below, but measured
    against the real campaign DB it lost 15 of 81 (~18%) already-correct freemail_linked matches —
    "birminghammots@gmail.com" x "mot or repairs", "sngmotorsalesltd@gmail.com" x "sg motors" — where
    the trade-name token legitimately sits mid-word with no separator. A real, measured regression to
    guard a hypothetical collision that produced zero false links on the same data is the wrong trade,
    so "car"/"mot"/"motors" etc. still match anywhere in the local part; only the explicit generic
    stopword list (test_affinity_generic_stopword_token_does_not_link_unrelated_name) is excluded."""
    assert classify_email_affinity("oscar77@gmail.com", "somewhere.co.uk", "Car Care Centre") == "freemail_linked"
    assert classify_email_affinity("car.care@gmail.com", "somewhere.co.uk", "Car Care Centre") == "freemail_linked"


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
    """affinity='own_domain' here (an inferred guess is on the business domain BY CONSTRUCTION) — the
    v0.3 fix bug was exactly that "own_domain" affinity alone used to let an inferred/invalid/unknown/
    risky own-domain row outrank a validated freemail_linked one; the old fixture (affinity='') never
    exercised that path at all."""
    contacts = [
        {"kind": "email", "value": "guess@shop.com", "tier": "inferred", "affinity": "own_domain"},
        {"kind": "email", "value": "info@shop.com", "tier": "role", "affinity": "own_domain"},
        {"kind": "email", "value": "owner@gmail.com", "tier": "valid", "affinity": "freemail_linked"},
    ]
    ranked = [c["value"] for c in rank_email_contacts(contacts)]
    assert ranked.index("guess@shop.com") > ranked.index("info@shop.com")
    assert ranked.index("guess@shop.com") > ranked.index("owner@gmail.com")


def test_rank_email_contacts_own_domain_invalid_below_freemail_linked_valid():
    """The exact violation the reviewer measured: affinity alone as the primary key let an own-domain
    address of ANY tier (including invalid) outrank a validated freemail_linked mailbox."""
    contacts = [
        {"kind": "email", "value": "dead@shop.com", "tier": "invalid", "affinity": "own_domain"},
        {"kind": "email", "value": "owner@gmail.com", "tier": "valid", "affinity": "freemail_linked"},
    ]
    ranked = [c["value"] for c in rank_email_contacts(contacts)]
    assert ranked == ["owner@gmail.com", "dead@shop.com"], ranked


def test_rank_email_contacts_own_domain_unknown_and_risky_below_freemail_linked_valid():
    contacts = [
        {"kind": "email", "value": "timeout@shop.com", "tier": "unknown", "affinity": "own_domain"},
        {"kind": "email", "value": "risky@shop.com", "tier": "risky", "affinity": "own_domain"},
        {"kind": "email", "value": "owner@gmail.com", "tier": "valid", "affinity": "freemail_linked"},
    ]
    ranked = [c["value"] for c in rank_email_contacts(contacts)]
    assert ranked[0] == "owner@gmail.com", ranked


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


def test_rank_email_contacts_inferred_outranks_risky():
    """The SEND-ranking half of validate.py's TIER_ORDER docstring, proven through the public function
    directly: docs/09 puts inferred ABOVE risky/catch_all/unknown in SEND order, the OPPOSITE of
    TIER_ORDER's own coverage/display order (test_tier_order_never_ranks_inferred_above_an_observed_tier
    below proves that TIER_ORDER side). Both a bare 'inferred' vs 'risky' pair and a 'catch_all'/
    'unknown' pair are checked, all at the same (irrelevant here) affinity, isolating the tier
    comparison from any affinity effect."""
    contacts = [
        {"kind": "email", "value": "risky@shop.com", "tier": "risky", "affinity": ""},
        {"kind": "email", "value": "catchall@shop.com", "tier": "catch_all", "affinity": ""},
        {"kind": "email", "value": "unknown@shop.com", "tier": "unknown", "affinity": ""},
        {"kind": "email", "value": "guess@shop.com", "tier": "inferred", "affinity": ""},
    ]
    ranked = [c["value"] for c in rank_email_contacts(contacts)]
    assert ranked[0] == "guess@shop.com", (
        f"inferred must outrank risky/catch_all/unknown in SEND order (docs/09), got {ranked}"
    )


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
        assert meta["reason"] == "placeholder"


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
