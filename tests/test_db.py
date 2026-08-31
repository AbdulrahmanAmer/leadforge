from leadforge import db
from leadforge.models import Business, Contact, Person


def _biz(**kw):
    base = dict(id="biz_1", name="Joe's Auto", name_norm="joes auto", dedupe_key="pid:ABC",
                place_id="ABC", source="gosom")
    base.update(kw)
    return Business(**base)


def test_upsert_dedupes_by_place_id(conn):
    b1 = _biz(website="https://joes.com", domain="joes.com")
    b2 = _biz(id="biz_2", phone_e164="+13334445555")  # same place_id, different missing fields
    id1, created1 = db.upsert_business(conn, b1)
    id2, created2 = db.upsert_business(conn, b2)
    assert created1 is True and created2 is False
    assert id1 == id2
    row = conn.execute("SELECT * FROM businesses").fetchall()
    assert len(row) == 1
    merged = row[0]
    assert merged["website"] == "https://joes.com"  # kept from first
    assert merged["phone_e164"] == "+13334445555"    # filled from second


def test_merge_unions_categories(conn):
    db.upsert_business(conn, _biz(categories=["auto repair"]))
    db.upsert_business(conn, _biz(id="biz_2", categories=["transmission shop"]))
    import json
    cats = json.loads(conn.execute("SELECT categories_json FROM businesses").fetchone()["categories_json"])
    assert set(cats) == {"auto repair", "transmission shop"}


def test_suppression_blocks(conn):
    db.suppress(conn, "domain", "spam.com")
    assert db.is_suppressed(conn, "spam.com") is True
    assert db.is_suppressed(conn, "good.com") is False


def test_contacts_and_people_roundtrip(conn):
    db.upsert_business(conn, _biz())
    db.add_contact(conn, Contact(business_id="biz_1", kind="email", value="a@joes.com", label="personal", tier="valid"))
    db.add_contact(conn, Contact(business_id="biz_1", kind="email", value="a@joes.com", label="personal", tier="risky"))
    contacts = db.contacts_for(conn, "biz_1")
    assert len(contacts) == 1 and contacts[0]["tier"] == "risky"  # upsert updated tier

    db.add_person(conn, Person(business_id="biz_1", name="Joe A", title="Owner"))
    pending = db.dm_pending(conn, 10)
    assert len(pending) == 1


def test_run_lifecycle(conn):
    run_id = db.create_run(conn, "icp.yaml", "hash123")
    db.set_stage(conn, run_id, "discovering", businesses=5)
    row = db.latest_run(conn, "hash123")
    assert row["stage"] == "discovering"
    import json
    assert json.loads(row["stats_json"])["businesses"] == 5
