import json

from leadforge import db
from leadforge.enrich.extract import classify_phone, extract_structured_people
from leadforge.enrich.metrics import (
    backfill_legacy_observations,
    cost_per_verified_dm,
    coverage_metrics,
    outreach_attribution,
    source_hit_rates,
)
from leadforge.models import Business, Contact, EnrichmentObservation, Evidence, Person


def _business(conn):
    db.upsert_business(conn, Business(id="b1", name="Acme", name_norm="acme", dedupe_key="k1"))


def test_observation_model_carries_provenance_and_schedule():
    obs = EnrichmentObservation(
        business_id="b1", field="email", value="owner@example.test", source="team_page",
        source_url="https://example.test/team", evidence="Jane Doe — owner — owner@example.test",
        confidence=0.92, verification_state="verified", jurisdiction="GB", person_name="Jane Doe",
        observed_at="2026-10-14T00:00:00Z", fresh_until="2027-01-12T00:00:00Z",
        recheck_at="2027-01-13T00:00:00Z",
    )
    assert obs.field == "email"
    assert obs.person_name == "Jane Doe"
    assert obs.verification_state == "verified"


def test_observation_round_trip_supports_multiple_values_per_person(conn):
    _business(conn)
    db.add_person(conn, Person(business_id="b1", name="Jane Doe", title="Owner"))
    person_id = conn.execute("SELECT id FROM people WHERE business_id='b1' AND name='Jane Doe'").fetchone()[0]
    db.add_observation(conn, EnrichmentObservation(business_id="b1", field="email", value="jane@example.test",
                      source="team_page", person_id=person_id, verification_state="verified"))
    db.add_observation(conn, EnrichmentObservation(business_id="b1", field="phone", value="+441234567890",
                      source="vcard", person_id=person_id, verification_state="observed"))
    rows = db.observations_for(conn, "b1")
    assert [json.loads(r["value_json"]) for r in rows] == ["jane@example.test", "+441234567890"]
    assert rows[0]["person_id"] == person_id


def test_phase0_metrics_report_coverage_hit_rate_and_outcome_attribution(conn):
    _business(conn)
    db.add_observation(conn, EnrichmentObservation(business_id="b1", field="person", value="Jane Doe",
                      source="team_page", verification_state="verified"))
    db.add_observation(conn, EnrichmentObservation(business_id="b1", field="email", value="jane@example.test",
                      source="licensed_provider", provider="paid-x", cost=0.25))
    db.add_outcome(conn, "b1", "email", "interested", source="team_page", provider="oss", contact_type="personal")
    assert coverage_metrics(conn)["observations"] == 2
    assert source_hit_rates(conn)[0]["hit_rate"] == 1.0
    assert outreach_attribution(conn)[0]["contact_type"] == "personal"
    assert cost_per_verified_dm(conn) == 0.0


def test_coverage_metrics_separate_person_owned_dm_routes_from_business_contacts(conn):
    from leadforge.enrich.metrics import coverage_metrics

    _business(conn)
    db.add_person(conn, Person(business_id="b1", name="Jane Doe", title="Owner", is_dm=1))
    person_id = conn.execute("SELECT id FROM people WHERE business_id='b1'").fetchone()[0]
    db.add_contact(conn, Contact(business_id="b1", kind="phone", value="+441234567890",
                                 phone_type="direct", tier="valid"))
    db.add_observation(conn, EnrichmentObservation(
        business_id="b1", field="phone", value="+441234567890", source="team_page",
        person_id=person_id, verification_state="observed",
    ))
    metrics = coverage_metrics(conn)
    assert metrics["businesses_with_accepted_dm"] == 1
    assert metrics["accepted_dms_with_person_phone"] == 1
    assert metrics["accepted_dms_with_direct_or_mobile"] == 1
    assert metrics["accepted_dms_with_person_email"] == 0


def test_legacy_backfill_is_idempotent_and_preserves_person_ownership(conn):
    _business(conn)
    db.add_person(conn, Person(business_id="b1", name="Jane Doe", title="Owner", origin="heuristic"))
    person_id = conn.execute("SELECT id FROM people WHERE business_id='b1'").fetchone()[0]
    db.add_contact(conn, Contact(
        business_id="b1", kind="email", value="jane@example.test", tier="valid", verified_at="2026-10-14T00:00:00Z",
        meta={"url": "https://example.test/team"},
    ))
    db.add_evidence(conn, Evidence(
        business_id="b1", ref_table="people", ref_id=person_id, fact="dm_candidate",
        url="https://example.test/team", snippet="Jane Doe — Owner", observed_at="2026-10-14T00:00:00Z",
    ))
    first = backfill_legacy_observations(conn)
    second = backfill_legacy_observations(conn)
    assert first["inserted"] == 3
    assert second["inserted"] == 0
    rows = db.observations_for(conn, "b1")
    assert any(r["field"] == "email" and r["verification_state"] == "verified" for r in rows)
    assert any(r["person_id"] == person_id for r in rows)


def test_phone_typing_is_conservative_and_supports_all_public_labels():
    assert classify_phone("+441234567890", "switchboard: 24 hours") == "switchboard"
    assert classify_phone("+441234567890", "direct extension 4") == "direct"
    assert classify_phone("+441234567890", business_phone="+441234567890") == "main_line"
    assert classify_phone("not-a-phone") == "unknown"


def test_structured_people_extracts_schema_person_and_vcard_without_text_inference():
    html = '''
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Person",
      "name":"Jane Doe","jobTitle":"Managing Director","telephone":"+44 20 7946 0958",
      "email":"jane.doe@example.test"}</script>
    <div class="vcard"><span class="fn">Sam Smith</span><span class="title">Operations Manager</span>
      <a class="tel" href="tel:+1-212-555-0100">+1 212 555 0100</a>
      <a class="email" href="mailto:sam.smith@example.test">Email Sam</a></div>
    '''
    people = extract_structured_people(html, "https://example.test/team")
    assert {(p.name, p.title) for p in people} == {
        ("Jane Doe", "Managing Director"), ("Sam Smith", "Operations Manager")
    }
    jane = next(p for p in people if p.name == "Jane Doe")
    sam = next(p for p in people if p.name == "Sam Smith")
    assert jane.phones == ("+44 20 7946 0958",)
    assert jane.emails == ("jane.doe@example.test",)
    assert sam.phones == ("+1 212 555 0100",)
    assert sam.emails == ("sam.smith@example.test",)


def test_structured_people_extracts_common_team_card_contacts():
    html = '''
    <section class="team-member">
      <h3>Alex Morgan</h3><div class="role">Managing Director</div>
      <a href="tel:+442012345678">Call Alex</a>
      <a href="mailto:alex@example.test">Email Alex</a>
    </section>
    '''
    people = extract_structured_people(html, "https://example.test/about/team")
    assert len(people) == 1
    assert people[0].name == "Alex Morgan"
    assert people[0].title == "Managing Director"
    assert people[0].phones == ("+442012345678",)
    assert people[0].emails == ("alex@example.test",)


def test_structured_event_roles_can_provide_explicit_person_contacts():
    html = '''
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Event","name":"Trade Expo",
      "organizer":{"@type":"Person","name":"Morgan Lee","jobTitle":"Founder",
        "telephone":"+44 20 7946 0958","email":"morgan@example.test"},
      "review":{"@type":"Review","author":{"@type":"Person","name":"Customer Reviewer"}}}
    </script>
    '''
    people = extract_structured_people(html, "https://example.test/events/trade-expo")
    assert [(p.name, p.context, p.phones, p.emails) for p in people] == [
        ("Morgan Lee", "event", ("+44 20 7946 0958",), ("morgan@example.test",)),
    ]
