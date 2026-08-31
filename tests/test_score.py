from leadforge import db
from leadforge.models import Business, Contact, Person
from leadforge.score import Scorer, score_run


def _seed_business(conn, **kw):
    base = dict(id="biz_x", name="Indie Auto", name_norm="indie auto", dedupe_key="pid:X", place_id="X",
                category="auto repair shop", categories=["auto repair shop"], source="gosom",
                phone_e164="+17135550100", website=None, rating=3.5, review_count=80,
                address_city="Houston", first_run_id="run_1")
    base.update(kw)
    b = Business(**base)
    db.upsert_business(conn, b)
    return b


def test_website_missing_is_positive_for_web_offer(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, website=None)  # missing site = need signal for a web-design ICP
    counts = score_run(conn, sample_icp, run_id)
    assert counts["scored"] == 1
    row = db.scores_for_run(conn, run_id)[0]
    import json
    factors = json.loads(row["factors_json"])
    need = next(f for f in factors if f["factor"] == "need_signals")
    assert "website_missing" in need["why"]
    assert row["tier"] in ("A", "B", "C")


def test_hard_dq_no_phone(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164=None)
    score_run(conn, sample_icp, run_id)
    row = db.scores_for_run(conn, run_id)[0]
    assert row["tier"] == "DQ"


def test_dm_and_contact_raise_score(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn)
    scorer = Scorer(conn, sample_icp, run_id)
    before = scorer.score_business(db.all_businesses(conn)[0]).total

    db.add_person(conn, Person(business_id="biz_x", name="Joe A", title="Owner", is_dm=1, dm_confidence=0.9,
                               labeled_by="agent"))
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="joe@indie.com", label="personal",
                                 tier="valid"))
    after = scorer.score_business(db.all_businesses(conn)[0]).total
    assert after > before


def test_weight_override_changes_ranking(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn)
    base = Scorer(conn, sample_icp, run_id).score_business(db.all_businesses(conn)[0]).total
    sample_icp.scoring.weights_override = {"industry_match": 40}
    boosted = Scorer(conn, sample_icp, run_id).score_business(db.all_businesses(conn)[0]).total
    assert boosted != base


def test_every_factor_has_why(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn)
    s = Scorer(conn, sample_icp, run_id).score_business(db.all_businesses(conn)[0])
    assert all(f.why for f in s.factors)
    assert len([f for f in s.factors if f.group != "negative"]) >= 8
