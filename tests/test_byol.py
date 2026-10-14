from leadforge import db
from leadforge.enrich.byol import import_csv
from leadforge.models import Business


def test_byol_requires_unique_match_and_defaults_to_dry_run(conn, tmp_path):
    db.upsert_business(conn, Business(id="b1", name="Acme Garage", name_norm="acmegarage",
                                     address_city="Leeds", domain="acme.test", dedupe_key="k1"))
    path = tmp_path / "contacts.csv"
    path.write_text(
        "business_name,city,person_name,title,email\n"
        "Acme Garage,Leeds,Jane Doe,Owner,jane@acme.test\n"
        "Unknown,Leeds,No Match,Owner,no@example.test\n", encoding="utf-8"
    )
    preview = import_csv(conn, path)
    assert preview["matched"] == 1 and preview["imported"] == 0
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    written = import_csv(conn, path, dry_run=False)
    assert written["imported"] == 1
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1


def test_observations_due_uses_explicit_freshness_schedule(conn):
    from leadforge.models import EnrichmentObservation

    db.upsert_business(conn, Business(id="b1", name="Acme", name_norm="acme", dedupe_key="k1"))
    db.add_observation(conn, EnrichmentObservation(business_id="b1", field="email", value="x@y.test",
                                                   fresh_until="2026-10-01T00:00:00Z"))
    assert len(db.observations_due(conn, "2026-10-14T00:00:00Z")) == 1
