"""v0.4 "autopilot" unit B — the deterministic template drafter (draft.template). `template_draft`
must never produce anything `draft.gate.check_draft` would reject: for every fact key it knows how
to write about, build a standalone packet carrying only that fact (plus a `constraints` block, like
a real packet), draft it, and run the SAME mechanical gate `draft apply`/`auto_draft` run drafts
through. Also proves the abstain paths: grade 'C', and a distinctive fact outside the drafter's
five-key priority list."""

from __future__ import annotations

import pytest

from leadforge.draft.gate import check_draft
from leadforge.draft.template import template_draft, template_drafts

CONSTRAINTS = {"max_observation_words": 45, "max_subject_chars": 60, "template_numbers": [], "literals": []}


def _packet(co: str, facts: list[dict], grade: str = "B", **constraint_overrides) -> dict:
    constraints = {**CONSTRAINTS, **constraint_overrides}
    return {
        "co": co, "city": "Leeds", "facts": facts,
        "offer": {"what": "website refresh", "value_prop": "more bookings"},
        "sender": {"from_name": "GainLev", "label": "gainlev-main"},
        "purpose": "gainlev_leadgen", "greeting": "Hello,",
        "constraints": constraints, "grade": grade,
    }


def _fact(k: str, v) -> dict:
    return {"k": k, "v": v, "src": "site", "at": "2026-01-01T00:00:00Z"}


# ------------------------------------------------------------------------- per-fact-key: draft, then gate
@pytest.mark.parametrize(
    "co,facts,expected_used_fact",
    [
        ("Acme Garage Ltd", [_fact("booking", "has online booking (Google Business)")], "booking"),
        ("Acme Garage Ltd", [_fact("site_stale", 2018)], "site_stale"),
        ("Acme Garage Ltd", [_fact("legal_name", "Acme Garage Limited"),
                             _fact("incorporated_year", "2015")], "legal_name"),
        ("Acme Garage Ltd", [_fact("legal_name", "Acme Garage Limited")], "legal_name"),  # no year fact
        ("Acme Garage Ltd", [_fact("hiring", "has a live careers/jobs page")], "hiring"),
        ("Acme Garage Ltd", [_fact("company_status", "active"),
                             _fact("rating", "4.5 stars (120 reviews)")], "rating"),
    ],
    ids=["booking", "site_stale", "legal_name+year", "legal_name_only", "hiring", "rating"],
)
def test_template_draft_passes_the_gate_for_every_known_fact_key(co, facts, expected_used_fact):
    packet = _packet(co, facts)
    draft = template_draft(packet)
    assert draft is not None, packet
    assert draft["used_fact"] == expected_used_fact
    assert draft["subject"] and draft["observation"]
    result = check_draft(packet, draft)
    assert result["ok"], (draft, result["reasons"])


def test_template_draft_uses_only_verbatim_packet_strings_no_possessive():
    """The observation must never write co + "'s" (e.g. "Ltd's") — that token is not itself present
    anywhere in the packet JSON and would fail the gate's PROPER_NOUN check."""
    packet = _packet("Acme Garage Ltd", [_fact("site_stale", 2018)])
    draft = template_draft(packet)
    assert "Ltd's" not in draft["observation"]
    assert "Acme Garage Ltd's" not in draft["observation"]


def test_template_draft_priority_prefers_booking_over_lower_priority_facts():
    packet = _packet("Acme Garage Ltd", [
        _fact("rating", "4.5 stars (120 reviews)"),
        _fact("hiring", "has a live careers/jobs page"),
        _fact("booking", "shows an online-booking option on its site"),
    ])
    draft = template_draft(packet)
    assert draft["used_fact"] == "booking"
    assert check_draft(packet, draft)["ok"]


# ------------------------------------------------------------------------- abstain paths
def test_template_draft_abstains_on_grade_c():
    packet = _packet("Acme Garage Ltd", [_fact("no_website", "no business website found")], grade="C")
    assert template_draft(packet) is None


def test_template_draft_abstains_when_no_priority_fact_present():
    """A distinctive fact that grade A/B could ride on (phone_confirmed) but that this drafter does
    not know how to write about -- 'no usable fact', not a grade problem."""
    packet = _packet("Acme Garage Ltd", [_fact("phone_confirmed", "site phone matches the GBP phone")], grade="B")
    assert template_draft(packet) is None


def test_template_draft_abstains_on_empty_packet():
    assert template_draft({}) is None
    assert template_draft({"co": "", "facts": [], "grade": "B"}) is None


def test_template_draft_abstains_when_observation_would_exceed_max_words():
    """site_stale's sentence is 11 words; a packet whose constraints cap it below that (and offers
    no other usable fact) must abstain rather than emit a draft the gate would reject on LENGTH."""
    packet = _packet("Acme Garage Ltd", [_fact("site_stale", 2018)], max_observation_words=3)
    assert template_draft(packet) is None


def test_template_draft_falls_through_a_too_long_fact_to_the_next_priority():
    """site_stale's sentence (11 words) doesn't fit under 10; hiring's (9 words) does -- the drafter
    must fall through to it rather than abstain outright."""
    packet = _packet("Acme Garage Ltd", [
        _fact("site_stale", 2018),
        _fact("hiring", "has a live careers/jobs page"),
    ], max_observation_words=10)
    draft = template_draft(packet)
    assert draft is not None and draft["used_fact"] == "hiring"
    assert check_draft(packet, draft)["ok"]


def test_template_draft_falls_back_to_quick_note_when_the_full_subject_is_too_long():
    packet = _packet("Acme Garage And Automotive Repair Centre Of Greater Leeds Limited",
                     [_fact("booking", "has online booking (Google Business)")], max_subject_chars=15)
    draft = template_draft(packet)
    assert draft is not None
    assert draft["subject"] == "Quick note"
    assert check_draft(packet, draft)["ok"]


# ------------------------------------------------------------------------- template_drafts (batch wrapper)
def test_template_drafts_maps_each_line_to_a_draft_or_abstain():
    booking_packet = _packet("Acme Garage Ltd", [_fact("booking", "has online booking (Google Business)")])
    grade_c_packet = _packet("Zed Autos", [_fact("no_website", "no business website found")], grade="C")
    lines = [
        {"target": 1, "packet": booking_packet},
        {"target": 2, "packet": grade_c_packet},
    ]
    out = template_drafts(lines)
    assert len(out) == 2
    by_target = {d["target"]: d for d in out}
    assert by_target[1]["used_fact"] == "booking" and "abstain" not in by_target[1]
    assert by_target[2] == {"target": 2, "abstain": True}
    assert check_draft(booking_packet, by_target[1])["ok"]


def test_template_drafts_skips_a_line_with_no_target():
    out = template_drafts([{"packet": {}}, {"target": 5, "packet": {}}])
    assert len(out) == 1 and out[0] == {"target": 5, "abstain": True}


def test_template_drafts_on_empty_input():
    assert template_drafts([]) == []
