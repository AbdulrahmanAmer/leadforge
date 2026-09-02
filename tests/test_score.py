import json

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


def _row(conn, business_id):
    return next(r for r in db.all_businesses(conn) if r["id"] == business_id)


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


def test_dm_and_contact_raise_contactability_not_fit(conn, sample_icp):
    """v0.3: reachability (DM/email/phone) no longer blends into `total`/fit — it is graded
    separately as the `contactability` meta factor, so it must NOT move `total` at all."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn)
    scorer = Scorer(conn, sample_icp, run_id)
    before = scorer.score_business(db.all_businesses(conn)[0])
    before_contactability = next(f for f in before.factors if f.factor == "contactability").points

    db.add_person(conn, Person(business_id="biz_x", name="Joe A", title="Owner", is_dm=1, dm_confidence=0.9,
                               labeled_by="agent"))
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="joe@indie.com", label="personal",
                                 tier="valid", affinity="freemail_unlinked"))
    after = scorer.score_business(db.all_businesses(conn)[0])
    after_contactability = next(f for f in after.factors if f.factor == "contactability").points
    assert after.total == before.total          # fit is untouched by reachability
    assert after_contactability > before_contactability  # but contactability rose (DM identified)


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


# --- v0.3 D: category alias truth table (docs/09 §D acceptance table) -------------------------
def test_category_alias_truth_table(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    sample_icp.target.categories = ["auto repair shop", "car garage", "MOT centre"]
    scorer = Scorer(conn, sample_icp, run_id)
    cases = {
        "Car inspection station": 1.0,
        "Car repair and maintenance service": 1.0,
        "MOT centre": 1.0,
        "Auto body shop": 1.0,
        "Mechanic": 1.0,
        "Tire shop": 0.6,          # adjacent, not the same trade as general repair/MOT — documented
        "Car dealer": 0.6,         # "0.6 at most" per the acceptance table
        "Community centre": 0.1,   # 'centre' is a stopword — must NOT fuzzy-match "MOT centre"
    }
    for i, (cat, expected) in enumerate(cases.items()):
        bid = f"biz_cat_{i}"
        _seed_business(conn, id=bid, place_id=bid, dedupe_key=f"pid:{bid}", category=cat, categories=[cat])
        score, why = scorer._f_industry_match(_row(conn, bid), {})
        assert score == expected, f"{cat}: expected {expected}, got {score} ({why})"


# --- v0.3 D: contactability weight table (docs/09 §D) ------------------------------------------
def test_contactability_dm_only(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164=None)  # no phone -> isolate the DM bonus
    db.add_person(conn, Person(business_id="biz_x", name="Jo Owner", title="Owner", is_dm=1,
                               dm_confidence=0.9, labeled_by="agent"))
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert next(f for f in s.factors if f.factor == "contactability").points == 30


def test_contactability_validated_phone_only(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164="+17135550100")
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert next(f for f in s.factors if f.factor == "contactability").points == 25


def test_contactability_email_tiers(conn, sample_icp):
    cases = [
        ("own_domain", "valid", 30),
        ("own_domain", "role", 22),
        ("freemail_linked", "valid", 20),
        ("", "inferred", 8),
        ("freemail_unlinked", "risky", 0),
    ]
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    for i, (affinity, tier, expected) in enumerate(cases):
        bid = f"biz_email_{i}"
        _seed_business(conn, id=bid, place_id=bid, dedupe_key=f"pid:{bid}", phone_e164=None)
        db.add_contact(conn, Contact(business_id=bid, kind="email", value=f"c{i}@example.com",
                                     tier=tier, affinity=affinity))
        s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, bid))
        assert next(f for f in s.factors if f.factor == "contactability").points == expected, \
            f"{affinity}/{tier}: expected {expected}"


def test_contactability_registry_and_phone_confirmed_and_mobile(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164=None)
    conn.execute("UPDATE businesses SET enrich_json=? WHERE id='biz_x'", (json.dumps({
        "registry_profile": {"company_number": "123", "company_status": "active"},
        "signals": {"phone_confirmed": True},
    }),))
    db.add_contact(conn, Contact(business_id="biz_x", kind="phone", value="+447890123456"))  # UK mobile
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert next(f for f in s.factors if f.factor == "contactability").points == 5 + 5 + 3


def test_contactability_combined_matches_the_arithmetic(conn, sample_icp):
    """DM(30) + own-domain valid email(30) + validated phone(25) + registry-active(5)
    + phone_confirmed(5) + mobile contact(3) = 98 — never hits the 100 cap under this weight table."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164="+17135550100")
    db.add_person(conn, Person(business_id="biz_x", name="Jo Owner", title="Owner", is_dm=1,
                               dm_confidence=0.9, labeled_by="agent"))
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="info@indieauto.com",
                                 tier="valid", affinity="own_domain"))
    db.add_contact(conn, Contact(business_id="biz_x", kind="phone", value="+447890123456"))
    conn.execute("UPDATE businesses SET enrich_json=? WHERE id='biz_x'", (json.dumps({
        "registry_profile": {"company_number": "123", "company_status": "active"},
        "signals": {"phone_confirmed": True},
    }),))
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    contact = next(f for f in s.factors if f.factor == "contactability")
    assert contact.points == 98
    status = next(f for f in s.factors if f.factor == "status")
    assert status.why == "READY"  # tier A/B (fit unaffected) + contactability >= 50


# --- v0.3 D: hooks fire only on real evidence (phantom-crawl fixture) ---------------------------
def test_hooks_never_fire_on_a_phantom_crawl(conn, sample_icp):
    """crawled_at stamped but `signals` has no keys computed (the live campaign's 115 zero-page
    'phantom' crawls) must fire NO signal-based hook, even though the soft qualifiers are enabled."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    sample_icp.qualify.soft = ["phone_only_booking", "weak_social_presence", "stale_site", "hiring"]
    _seed_business(conn, website="https://x.example")
    conn.execute("UPDATE businesses SET enrich_json=? WHERE id='biz_x'",
                (json.dumps({"crawled_at": "2026-01-01T00:00:00Z", "signals": {}}),))
    scorer = Scorer(conn, sample_icp, run_id)
    row = _row(conn, "biz_x")
    hits = scorer._need_hits(row, json.loads(row["enrich_json"]))
    assert hits == []


def test_hooks_fire_with_real_evidence_and_interpolate_year(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    sample_icp.qualify.soft = ["phone_only_booking", "weak_social_presence", "stale_site"]
    _seed_business(conn, website="https://x.example")
    conn.execute("UPDATE businesses SET enrich_json=? WHERE id='biz_x'", (json.dumps({
        "crawled_at": "2026-01-01T00:00:00Z", "socials": {},
        "signals": {"booking_hint": False, "stale_site": True, "copyright_year": 2018},
    }),))
    scorer = Scorer(conn, sample_icp, run_id)
    row = _row(conn, "biz_x")
    enrich = json.loads(row["enrich_json"])
    hits = scorer._need_hits(row, enrich)
    assert set(hits) == {"phone_only_booking", "weak_social_presence", "stale_site"}
    hooks = scorer._hooks(row, {"need_hits": hits, "enrich": enrich})
    joined = " | ".join(hooks)
    assert "No online booking found on their website" in joined
    assert "No social profile is linked from their website" in joined
    assert "2018" in joined and "broken pages" not in joined


# --- v0.3 D: chain penalty + profile registry ---------------------------------------------------
def test_chain_member_penalty_from_db_chain_map(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, id="biz_a", place_id="A", dedupe_key="pid:A", phone_e164="+17135551111",
                   domain="sharedsite.example")
    _seed_business(conn, id="biz_b", place_id="B", dedupe_key="pid:B", phone_e164="+17135552222",
                   domain="sharedsite.example")  # same domain, different phone -> chain by domain
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_a"))
    assert any(f.factor == "negative:chain_member" for f in s.factors)


def test_contactability_infers_affinity_for_legacy_rows_without_affinity(conn, sample_icp):
    """Real-data proof on the live UK campaign DB caught this: a pre-v0.3 contact row (affinity
    column added this release, so old rows store '') for a real own-domain role email scored 0
    contactability points instead of 22, because the code only trusted a set `affinity` value."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164=None, domain="indieauto.com")
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="info@indieauto.com",
                                 tier="role"))  # affinity left unset — legacy shape
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert next(f for f in s.factors if f.factor == "contactability").points == 22


def test_profile_registry_dispatches_default_and_account_fit(conn, sample_icp):
    from leadforge.score import PROFILES

    assert "default" in PROFILES and "account_fit" in PROFILES
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn)
    counts = score_run(conn, sample_icp, run_id)
    assert counts["scored"] == 1  # default dispatch still works through the registry
