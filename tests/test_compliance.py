"""U9.2 — the compliance gate is pure and decides exactly what docs/07 + docs/09 say it decides."""

from __future__ import annotations

import json
import sqlite3

from leadforge import compliance as c


def _row(**kw) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = {"domain": None, "enrich_json": "{}", "phone_e164": None}
    cols.update(kw)
    keys = ", ".join(cols)
    conn.execute(f"CREATE TABLE t({keys})")
    conn.execute(f"INSERT INTO t VALUES({', '.join('?' * len(cols))})", list(cols.values()))
    return conn.execute("SELECT * FROM t").fetchone()


def _contact(value, tier, affinity=""):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE c(kind, value, tier, affinity)")
    conn.execute("INSERT INTO c VALUES('email', ?, ?, ?)", (value, tier, affinity))
    return conn.execute("SELECT * FROM c").fetchone()


def test_entity_type_ladder():
    active = _row(enrich_json=json.dumps({"registry_profile": {"company_number": "1", "company_status": "active"}}))
    dissolved = _row(enrich_json=json.dumps({"registry_profile": {"company_number": "1", "company_status": "dissolved"}}))
    unmatched = _row(enrich_json=json.dumps({"registry_checked": True}))
    assert c.entity_type(active) == c.ENTITY_CORPORATE_ACTIVE
    assert c.entity_type(dissolved) == c.ENTITY_CORPORATE_INACTIVE
    assert c.entity_type(unmatched) == c.ENTITY_UNMATCHED
    assert c.entity_type(_row()) == c.ENTITY_UNCHECKED
    # pre-v0.3 rows: officers stored but no profile -> corporate, status unknown
    assert c.entity_type(_row(), people=[{"labeled_by": "registry"}]) == c.ENTITY_CORPORATE_UNKNOWN


def test_lawful_basis_uk_rules():
    lb = c.lawful_basis_email
    assert lb(c.ENTITY_CORPORATE_ACTIVE, "info@x.co.uk", "role", "own_domain", "uk") == c.BASIS_B2B
    assert lb(c.ENTITY_CORPORATE_INACTIVE, "info@x.co.uk", "role", "own_domain", "uk") == c.BASIS_CONFIRM
    # owner decision 5: a linked freemail box at an unmatched business is mailable by default...
    assert lb(c.ENTITY_UNMATCHED, "joe@gmail.com", "valid", "freemail_linked", "uk") == c.BASIS_B2B
    # ...but never an unlinked one, and never with require_corporate on
    assert lb(c.ENTITY_UNMATCHED, "x@gmail.com", "valid", "freemail_unlinked", "uk") == c.BASIS_NONE
    assert lb(c.ENTITY_UNMATCHED, "joe@gmail.com", "valid", "freemail_linked", "uk", require_corporate=True) == c.BASIS_CONSENT
    # non-sendable tiers are never a basis for anything
    for tier in ("inferred", "risky", "unknown", "invalid"):
        assert lb(c.ENTITY_CORPORATE_ACTIVE, "a@x.co.uk", tier, "own_domain", "uk") == c.BASIS_NONE
    # freemail policy 'none' = own-domain only
    assert lb(c.ENTITY_CORPORATE_ACTIVE, "joe@gmail.com", "valid", "freemail_linked", "uk", freemail_policy="none") == c.BASIS_NONE
    # US: opt-out model, entity type irrelevant
    assert lb(c.ENTITY_UNCHECKED, "joe@x.com", "valid", "own_domain", "us") == c.BASIS_B2B


def test_eligibility_prefers_own_domain_and_reports_reasons():
    row = _row(domain="x.co.uk", enrich_json=json.dumps({"registry_profile": {"company_number": "1", "company_status": "active"}}))
    contacts = [_contact("stranger@gmail.com", "valid", "freemail_unlinked"), _contact("info@x.co.uk", "role", "own_domain")]
    e = c.email_eligibility(row, contacts, c.ENTITY_CORPORATE_ACTIVE, "uk")
    assert e["eligible"] and e["email"] == "info@x.co.uk" and e["basis"] == c.BASIS_B2B
    e2 = c.email_eligibility(row, contacts, c.ENTITY_CORPORATE_ACTIVE, "uk", suppressed=True)
    assert not e2["eligible"] and "suppressed" in e2["reasons"]
    e3 = c.email_eligibility(row, [], c.ENTITY_CORPORATE_ACTIVE, "uk")
    assert not e3["eligible"] and e3["email"] is None and "no_sendable_email" in e3["reasons"]


def test_next_action_is_phone_first():
    ok = {"eligible": True, "basis": c.BASIS_B2B}
    no = {"eligible": False, "basis": c.BASIS_NONE}
    confirm = {"eligible": False, "basis": c.BASIS_CONFIRM}
    assert c.next_action(phone_validated=True, has_dm=True, eligibility=ok) == c.NEXT_CALL_NAMED
    assert c.next_action(phone_validated=True, has_dm=False, eligibility=ok) == c.NEXT_CALL_SWITCHBOARD
    assert c.next_action(phone_validated=False, has_dm=True, eligibility=ok) == c.NEXT_EMAIL
    assert c.next_action(phone_validated=False, has_dm=False, eligibility=confirm) == c.NEXT_EMAIL_CONFIRM
    assert c.next_action(phone_validated=False, has_dm=False, eligibility=no) == c.NEXT_RESEARCH
    assert c.next_action(phone_validated=True, has_dm=True, eligibility=ok, tier="DQ") == c.NEXT_DQ
    assert c.next_action(phone_validated=True, has_dm=True, eligibility=ok, outreach_state="sent") == "OUTREACH - sent"


def test_name_gate_requires_active_similar_and_corroborated():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE p(name, labeled_by, origin)")
    conn.execute("INSERT INTO p VALUES('Smith, Jane', 'registry', 'registry')")
    conn.execute("INSERT INTO p VALUES('Bob Lee', 'agent', 'heuristic')")
    reg, heur = conn.execute("SELECT * FROM p ORDER BY rowid").fetchall()
    good = {"registry_profile": {"company_status": "active", "match_similarity": 0.8}}
    assert c.name_allowed(reg, good, corroborations=1)
    assert not c.name_allowed(reg, good, corroborations=0)
    assert not c.name_allowed(reg, {"registry_profile": {"company_status": "dissolved", "match_similarity": 0.9}}, 2)
    assert not c.name_allowed(reg, {"registry_profile": {"company_status": "active", "match_similarity": 0}}, 2)
    assert c.name_allowed(heur, {}, corroborations=0)  # a site-sourced name needs no registry gate



def test_next_action_stays_phone_first_while_a_draft_is_only_drafted():
    """v0.4: autopilot drafts for every A/B target; the sheet must still say CALL first."""
    from leadforge.compliance import NEXT_CALL_NAMED, next_action
    elig = {"eligible": True, "basis": "legitimate_interest"}
    assert next_action(phone_validated=True, has_dm=True, eligibility=elig, outreach_state="enrolled") == NEXT_CALL_NAMED
    drafted = next_action(phone_validated=True, has_dm=True, eligibility=elig, outreach_state="drafted")
    assert drafted.startswith(NEXT_CALL_NAMED) and "draft ready" in drafted
    assert next_action(phone_validated=True, has_dm=True, eligibility=elig, outreach_state="sent") == "OUTREACH - sent"
