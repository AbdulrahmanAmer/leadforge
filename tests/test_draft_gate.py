"""v0.3 unit F — the mechanical no-fabrication gate (docs/09 Wave 2 F acceptance): a 12-case
watched-fail suite. Every mutation below starts from ONE clean, passing draft and changes exactly one
thing; each assertion is watched-fail per AGENTS.md — break the code the check covers, see the SAME
mutation pass (red for the right reason at the code level, i.e. a real bug), then restore. The proof
for each is in the unit report's `watched_fail` list, not here; this file only proves the current gate
rejects every fabrication and accepts the clean draft, both before and after each single mutation."""

from leadforge.draft.gate import check_draft

PACKET = {
    "co": "Acme Garage Ltd",
    "city": "Leeds",
    "facts": [
        {"k": "category", "v": "Car repair", "src": "maps", "at": "2026-01-01T00:00:00Z"},
        {"k": "city", "v": "Leeds", "src": "maps", "at": "2026-01-01T00:00:00Z"},
        {"k": "site_stale", "v": 2018, "src": "site", "at": "2026-01-01T00:00:00Z"},
        {"k": "booking", "v": "shows an online-booking option on its site", "src": "site", "at": "2026-01-01T00:00:00Z"},
        {"k": "dm_name", "v": "Sarah Smith", "src": "registry", "at": "2026-01-01T00:00:00Z"},
    ],
    "offer": {"what": "website refresh", "value_prop": "more bookings"},
    "sender": {"from_name": "GainLev", "label": "gainlev-main"},
    "purpose": "gainlev_leadgen",
    "greeting": "Hi Sarah,",
    "constraints": {"max_observation_words": 12, "max_subject_chars": 40, "template_numbers": [], "literals": []},
    "grade": "A",
}

CLEAN_DRAFT = {
    "target": 1,
    "subject": "Quick note about Acme Garage Ltd",
    "observation": "Noticed your site footer still says 2018 - happy to refresh it.",
    "used_fact": "site_stale",
}


def _mutate(**over):
    return {**CLEAN_DRAFT, **over}


def test_clean_draft_passes():
    """#12 — the reference draft this whole file mutates from is itself gate-clean."""
    result = check_draft(PACKET, CLEAN_DRAFT)
    assert result == {"ok": True, "reasons": []}


def test_injected_number_rejected_and_clean_still_passes():
    """#1 NUMBER — a number that appears nowhere in the packet."""
    bad = _mutate(observation="We've already helped 47 garages like yours grow.")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("NUMBER") for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_fake_email_rejected_and_clean_still_passes():
    """#2 EMAIL — an email address invented by the model, not present in the packet."""
    bad = _mutate(observation="Reach me directly at sarah@totally-made-up.example any time.")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("EMAIL") for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_fake_url_rejected_and_clean_still_passes():
    """#3 URL — a link the packet never mentioned."""
    bad = _mutate(observation="See our case studies at https://example-not-in-packet.test/cases")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("URL") for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_invented_proper_noun_rejected_and_clean_still_passes():
    """#4 PROPER NOUN — a competitor/person name that isn't in the packet anywhere."""
    bad = _mutate(observation="Even John Bloggs over at Speedy Repairs was impressed by this.")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("PROPER_NOUN") for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_missing_used_fact_rejected_and_clean_still_passes():
    """#5 USED_FACT — no fact cited at all."""
    bad = _mutate(used_fact="")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("USED_FACT") for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_overlong_subject_rejected_and_clean_still_passes():
    """#6 LENGTH — subject over constraints.max_subject_chars (40 in this fixture packet)."""
    bad = _mutate(subject="This subject line is deliberately far too long for the configured cap")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("LENGTH") and "subject" in r for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_overlong_observation_rejected_and_clean_still_passes():
    """#7 LENGTH — observation over constraints.max_observation_words (12 in this fixture packet)."""
    bad = _mutate(observation="Noticed your site footer still says 2018 which is honestly quite a long "
                              "time ago now and well overdue a refresh if you ask me")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("LENGTH") and "observation" in r for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_banned_social_proof_rejected_and_clean_still_passes():
    """#8 BANNED — unverifiable "dozens of garages" social-proof claim."""
    bad = _mutate(observation="Dozens of garages just like yours have already signed up.")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("BANNED") for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_banned_we_cut_rejected_and_clean_still_passes():
    """#9 BANNED — an unverifiable outcome claim ("we cut/reduced/increased/saved")."""
    bad = _mutate(observation="We cut their no-show rate in half within a month.")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("BANNED") for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_negation_contradiction_rejected_and_clean_still_passes():
    """#10 NEGATION — the packet's `booking` fact is present (truthy), but the draft denies it."""
    bad = _mutate(observation="I noticed you don't take bookings online at all yet.")
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("NEGATION") for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_negation_reverse_claim_rejected_when_booking_fact_absent():
    """#10b NEGATION, the other direction — no `booking` fact at all, so the draft may not claim one."""
    packet_no_booking = {**PACKET, "facts": [f for f in PACKET["facts"] if f["k"] != "booking"]}
    bad = _mutate(observation="Great that customers can already book online with you.")
    result = check_draft(packet_no_booking, bad)
    assert not result["ok"]
    assert any(r.startswith("NEGATION") for r in result["reasons"])
    assert check_draft(packet_no_booking, CLEAN_DRAFT)["ok"]


def test_cited_fact_absent_rejected_and_clean_still_passes():
    """#11 USED_FACT — the cited key does not exist in packet['facts'] at all."""
    bad = _mutate(used_fact="hiring")  # PACKET carries no 'hiring' fact
    result = check_draft(PACKET, bad)
    assert not result["ok"]
    assert any(r.startswith("USED_FACT") and "hiring" in r for r in result["reasons"])
    assert check_draft(PACKET, CLEAN_DRAFT)["ok"]


def test_tolerant_of_a_packet_missing_optional_keys():
    """The gate is called directly by an external gate script with a minimal packet — it must not
    crash on a packet carrying only the documented minimum shape."""
    minimal = {"co": "Bare Co", "city": "Leeds", "facts": [{"k": "category", "v": "MOT centre", "src": "maps"}],
              "offer": {}, "sender": {}}
    draft = {"subject": "Quick note", "observation": "Noticed you're an MOT centre in Leeds.", "used_fact": "category"}
    result = check_draft(minimal, draft)
    assert result["ok"], result


def test_missing_draft_and_packet_do_not_crash():
    result = check_draft({}, {})
    assert result["ok"] is False
    assert result["reasons"]


def test_possessive_of_a_packet_name_is_not_a_new_proper_noun():
    """Live 2026-09-03: "Arnold Service Centre's online booking" was rejected although every word is the
    business name. A possessive of a packet noun invents nothing; an unknown name still fails."""
    packet = {"co": "Arnold Service Centre", "city": "Nottingham",
              "facts": [{"k": "booking", "v": "shows an online-booking option on its site", "src": "site", "at": "x"}],
              "constraints": {"max_subject_chars": 60, "max_observation_words": 45}}
    ok = check_draft(packet, {"subject": "Arnold Service Centre's online booking",
                              "observation": "Your site shows an online-booking option on its site already.",
                              "used_fact": "booking"})
    assert ok["ok"], ok["reasons"]
    bad = check_draft(packet, {"subject": "Kens Garage's online booking",
                               "observation": "Your site shows an online-booking option on its site already.",
                               "used_fact": "booking"})
    assert any(r.startswith("PROPER_NOUN") for r in bad["reasons"])


def test_capitalised_fragment_glued_to_a_digit_is_not_a_proper_noun():
    """Live 2026-09-03: the template draft for "1stop MOT Centre" quoted the legal name
    "1STOP MOT CENTRE LIMITED" and the word scanner read "STOP" out of "1STOP"."""
    packet = {"co": "1stop MOT Centre Limited", "city": "Nottingham",
              "facts": [{"k": "legal_name", "v": "1STOP MOT CENTRE LIMITED", "src": "registry", "at": "x"},
                        {"k": "incorporated_year", "v": "2011", "src": "registry", "at": "x"}],
              "constraints": {"max_subject_chars": 60, "max_observation_words": 45}}
    res = check_draft(packet, {"subject": "Quick note for 1stop MOT Centre Limited",
                               "observation": "Saw 1STOP MOT CENTRE LIMITED has been trading since 2011.",
                               "used_fact": "legal_name"})
    assert res["ok"], res["reasons"]
