"""v0.2.0 inferred emails — pattern inference from PUBLIC evidence only.

The red line (icm/SCOPE.md #5) is untouched: no SMTP, no RCPT probing, no mailbox enumeration.
An inference is allowed only when a real email already seen on the same domain reveals the
local-part shape. No anchor -> no guess.
"""
import pytest

from leadforge.enrich.infer_email import (
    infer_email,
    pattern_from_anchor,
    split_person_name,
)


# --- name splitting ---------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "expect"), [
    ("Jane Smith", ("jane", "smith")),
    ("Sean Vincent Murphy", ("sean", "murphy")),      # middle names ignored
    ("MURPHY, Sean", ("sean", "murphy")),             # registry order, via natural_name
    ("jane  smith", ("jane", "smith")),
    ("Ana de la Cruz", ("ana", "delacruz")),          # particles join the surname
    ("O'Brien", (None, None)),                        # single token: not enough to infer
    ("", (None, None)),
    ("Acme Widgets Ltd", (None, None)),               # company, not a person
])
def test_split_person_name(raw, expect):
    assert split_person_name(raw) == expect


# --- pattern derivation from an anchor ----------------------------------------------------------
@pytest.mark.parametrize(("anchor_local", "first", "last", "expect"), [
    ("john.doe", "john", "doe", "first.last"),
    ("johndoe", "john", "doe", "firstlast"),
    ("jdoe", "john", "doe", "flast"),
    ("john", "john", "doe", "first"),
    ("doe.john", "john", "doe", "last.first"),
    ("john_doe", "john", "doe", "first_last"),
    ("j.doe", "john", "doe", "f.last"),
    ("info", "john", "doe", None),                    # role mailbox reveals nothing about people
    ("xqz12", "john", "doe", None),                   # unrecognizable
])
def test_pattern_from_anchor(anchor_local, first, last, expect):
    assert pattern_from_anchor(anchor_local, first, last) == expect


# --- the inference gate -------------------------------------------------------------------------
def _cfg_on(cfg):
    cfg.validation.infer_emails = True
    return cfg


def test_infers_only_from_a_person_anchor_on_the_same_domain(cfg, monkeypatch):
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: True)
    out = infer_email("Jane Smith", "acme.example", ["bob.jones@acme.example"], _cfg_on(cfg))
    assert out is not None
    assert out["email"] == "jane.smith@acme.example"
    assert out["pattern"] == "first.last"
    assert out["basis"].startswith("pattern first.last from bob.jones@acme.example")
    assert 0.0 < out["confidence"] <= 1.0


def test_no_anchor_means_no_guess(cfg, monkeypatch):
    """The whole honesty premise: without evidence of the domain's shape we do not invent one."""
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: True)
    assert infer_email("Jane Smith", "acme.example", [], _cfg_on(cfg)) is None
    # a role mailbox is not evidence of a personal pattern
    assert infer_email("Jane Smith", "acme.example", ["info@acme.example"], _cfg_on(cfg)) is None
    # nor is an anchor on a different domain
    assert infer_email("Jane Smith", "acme.example", ["bob.jones@other.example"], _cfg_on(cfg)) is None


def test_no_mx_means_no_guess(cfg, monkeypatch):
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: False)
    assert infer_email("Jane Smith", "acme.example", ["bob.jones@acme.example"], _cfg_on(cfg)) is None


def test_disabled_by_default(cfg, monkeypatch):
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: True)
    assert cfg.validation.infer_emails is False  # opt-in, never a surprise
    assert infer_email("Jane Smith", "acme.example", ["bob.jones@acme.example"], cfg) is None


def test_unusable_name_or_domain_means_no_guess(cfg, monkeypatch):
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: True)
    assert infer_email("Acme Widgets Ltd", "acme.example", ["b.jones@acme.example"], _cfg_on(cfg)) is None
    assert infer_email("Jane Smith", "", ["b.jones@acme.example"], _cfg_on(cfg)) is None


def test_freemail_domains_are_never_inferred(cfg, monkeypatch):
    """gmail.com has no company pattern — guessing jane.smith@gmail.com would be a real person's
    unrelated mailbox."""
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: True)
    assert infer_email("Jane Smith", "gmail.com", ["bob.jones@gmail.com"], _cfg_on(cfg)) is None


def test_multiple_anchors_agreeing_raise_confidence(cfg, monkeypatch):
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: True)
    one = infer_email("Jane Smith", "acme.example", ["bob.jones@acme.example"], _cfg_on(cfg))
    two = infer_email("Jane Smith", "acme.example",
                      ["bob.jones@acme.example", "amy.wong@acme.example"], _cfg_on(cfg))
    assert two["confidence"] > one["confidence"]


def test_inferred_email_is_syntactically_clean(cfg, monkeypatch):
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: True)
    out = infer_email("Ana de la Cruz", "acme.example", ["b.jones@acme.example"], _cfg_on(cfg))
    assert out["email"] == "a.delacruz@acme.example"  # f.last, particles folded, ascii-safe


def test_departmental_addresses_are_not_person_anchors(cfg, monkeypatch):
    """Found live in the Guildford campaign: 'experienced.hire@' and 'new.business@' wear a
    first.last shape but are departmental — anchoring on them would invent people."""
    monkeypatch.setattr("leadforge.enrich.infer_email._mx_ok", lambda d, t: True)
    for bad in ("experienced.hire@acme.example", "new.business@acme.example",
                "customer.service@acme.example"):
        assert infer_email("Jane Smith", "acme.example", [bad], _cfg_on(cfg)) is None
    # a real person on the same domain still anchors fine
    ok = infer_email("Jane Smith", "acme.example",
                     ["experienced.hire@acme.example", "duncan.sweetland@acme.example"], _cfg_on(cfg))
    assert ok["email"] == "jane.smith@acme.example"
