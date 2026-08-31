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


# --- v0.1.4: geography is actually checked against the ICP ------------------------------------
def test_geography_match_checks_icp_areas(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn)  # Houston — inside "Houston, TX"
    s = Scorer(conn, sample_icp, run_id).score_business(db.all_businesses(conn)[0])
    geo = next(f for f in s.factors if f.factor == "geography_match")
    assert geo.score == 1.0
    assert "target area" in geo.why  # the explanation names a check that actually ran


def test_geography_mismatch_scores_low_and_says_so(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, address_city="Dallas", address_full="1 Main St, Dallas, TX 75201")
    s = Scorer(conn, sample_icp, run_id).score_business(db.all_businesses(conn)[0])
    geo = next(f for f in s.factors if f.factor == "geography_match")
    assert geo.score < 0.5
    assert "target" in geo.why


def test_wrong_country_gets_out_of_area_penalty(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, address_city="Birmingham", address_country="GB")
    s = Scorer(conn, sample_icp, run_id).score_business(db.all_businesses(conn)[0])
    assert any(f.factor == "negative:out_of_area" for f in s.factors)


def test_same_country_alias_never_penalized(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, address_country="USA")  # campaign says "US" — same place, different spelling
    s = Scorer(conn, sample_icp, run_id).score_business(db.all_businesses(conn)[0])
    assert not any(f.factor == "negative:out_of_area" for f in s.factors)


def test_chain_and_competitor_matching_is_word_bounded(conn, sample_icp):
    """v0.1.4: substring matching DQ'd innocents — 'group' in 'Grouper', 'inc' in 'Vincent',
    competitor 'ace' in 'Palace'."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, name="Grouper & Vincent Auto", name_norm="grouper vincent auto")
    s = Scorer(conn, sample_icp, run_id).score_business(db.all_businesses(conn)[0])
    assert not any(f.factor == "negative:franchise_or_chain" for f in s.factors)
    assert next(f for f in s.factors if f.factor == "business_model").score > 0.3

    _seed_business(conn, id="biz_p", place_id="P2", dedupe_key="pid:P2",
                   name="Palace Garage", name_norm="palace garage")
    rows = {r["id"]: r for r in db.all_businesses(conn)}
    sample_icp.qualify.hard = ["competitor:ace"]
    assert Scorer(conn, sample_icp, run_id).score_business(rows["biz_p"]).tier != "DQ"
    sample_icp.qualify.hard = ["competitor:palace"]  # the real competitor still DQs
    assert Scorer(conn, sample_icp, run_id).score_business(rows["biz_p"]).tier == "DQ"


# --- v0.1.2 export resolutions ---------------------------------------------------------------
def test_export_cells_are_always_resolved(tmp_path, monkeypatch):
    import json as _json

    from leadforge import db
    from leadforge.export import _format_hours, export_run
    from leadforge.models import ICP, Business, Score, ScoreFactor
    monkeypatch.chdir(tmp_path)
    from leadforge.config import load_config
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    rid = db.create_run(conn, "icp.yaml", "h")
    db.upsert_business(conn, Business(id="b1", run_id=rid, name="No Web Garage", source="gosom",
                                      phone_e164="+441483123456",
                                      hours={"Monday": ["9 AM-6 PM"], "Sunday": ["Closed"]}))
    db.save_score(conn, Score(business_id="b1", run_id=rid, total=50, tier="B",
                              factors=[ScoreFactor(factor="x", group="fit", weight=1, score=1, points=1, why="w")]))
    icp = ICP.model_validate({"campaign": "t", "offer": {"what": "x"},
                              "target": {"categories": ["garage"],
                                         "geography": {"areas": ["Guildford"], "country": "GB"}}})
    arts = export_run(conn, icp, rid, cfg.exports_dir, ["csv"])
    import csv
    row = next(csv.DictReader(open([a for a in arts if a.endswith(".csv")][0], encoding="utf-8-sig")))
    assert row["Website"].startswith("NONE")
    assert row["Email"] == "no website to crawl"
    assert row["DM Name"].startswith("not identified")
    assert row["Call Readiness"] == "READY - ask switchboard"
    assert row["Opening Hours"].startswith("Mon 9")
    assert row["Company No"] == "not looked up"
    assert _format_hours(None) == "-"
    assert _format_hours(_json.dumps({})) == "-"
