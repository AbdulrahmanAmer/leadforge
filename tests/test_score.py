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


def test_contactability_backfills_affinity_before_ranking_not_after(conn, sample_icp):
    """Fresh-context review blocker: 233/233 contact rows on the live campaign DB store affinity ''.
    Ranking those raw rows falls through to tier order alone and a stranger's freemail 'valid' address
    outranks the business's own 'role' mailbox — scoring it as a mere 20-pt linked-freemail hit
    instead of the 22-pt own-domain role hit it actually is. Affinity must be filled BEFORE ranking."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164=None, domain="indieauto.example")
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="stranger@gmail.com",
                                 tier="valid"))  # affinity unset, as every pre-v0.3 row is
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="info@indieauto.example",
                                 tier="role"))  # affinity unset here too
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    contact = next(f for f in s.factors if f.factor == "contactability")
    assert contact.points == 22, f"expected own-domain role (22), got {contact.points} ({contact.why})"
    assert "own-domain role email" in contact.why


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


def test_hooks_never_fire_on_a_phantom_crawl_even_with_signals_populated(conn, sample_icp):
    """v0.3 polish: `crawled_at` stamped AND `signals` fully populated (as a phantom zero-page crawl
    can still leave stale defaults in `signals` from a prior attempt) but `pages` is 0 — checking
    `crawled_at` + 'key present' was NOT enough; `pages` must be > 0 too, or Site Status must not
    read 'live' and hooks must not fire on a page that was never actually fetched."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    sample_icp.qualify.soft = ["phone_only_booking", "weak_social_presence", "stale_site", "hiring"]
    _seed_business(conn, website="https://x.example")
    conn.execute("UPDATE businesses SET enrich_json=? WHERE id='biz_x'", (json.dumps({
        "crawled_at": "2026-01-01T00:00:00Z", "pages": 0, "socials": {},
        "signals": {"booking_hint": False, "stale_site": True, "careers": True, "copyright_year": 2018},
    }),))
    scorer = Scorer(conn, sample_icp, run_id)
    row = _row(conn, "biz_x")
    hits = scorer._need_hits(row, json.loads(row["enrich_json"]))
    assert hits == []


def test_hooks_fire_with_real_evidence_and_interpolate_year(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    sample_icp.qualify.soft = ["phone_only_booking", "weak_social_presence", "stale_site"]
    _seed_business(conn, website="https://x.example")
    conn.execute("UPDATE businesses SET enrich_json=? WHERE id='biz_x'", (json.dumps({
        "crawled_at": "2026-01-01T00:00:00Z", "pages": 3, "socials": {},
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


def test_score_run_accepts_optional_cfg_backward_compatibly(conn, sample_icp):
    """v0.3 polish finding 4: score_run/score_run_default/Scorer take an optional cfg (default None
    -> the built-in policy) without breaking every existing call site that doesn't pass one."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn)
    counts_no_cfg = score_run(conn, sample_icp, run_id)  # every pre-polish call site does this
    assert counts_no_cfg["scored"] == 1
    from leadforge.config import Config
    counts_with_cfg = score_run(conn, sample_icp, run_id, Config())  # re-score the same run with a cfg
    assert counts_with_cfg["scored"] == 1


# --- v0.3 polish finding 2: Status is truthful for the phone-first design ----------------------
def test_status_dq_when_hard_disqualified(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164=None)  # sample_icp.qualify.hard == ["no_phone"]
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert next(f for f in s.factors if f.factor == "status").why == "DQ"


def test_status_call_only_when_phone_validated_regardless_of_eligible_email(conn, sample_icp):
    """Regression for the exact bug in the finding: the old gate was
    `phone_ok and not eligibility['eligible']`, so a not-READY business with BOTH a validated phone
    AND an eligible email fell through to the else branch and was mislabeled RESEARCH — even though
    it was immediately callable. A validated phone means CALL_ONLY no matter what the email side
    looks like."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, category="community centre", categories=["community centre"],  # weak fit -> tier C
                   phone_e164="+17135550100", domain="indieauto.com")
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="info@indieauto.com",
                                 tier="valid", affinity="own_domain"))  # eligible (own-domain, valid, US)
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert s.tier == "C"  # not READY on fit alone
    assert next(f for f in s.factors if f.factor == "status").why == "CALL_ONLY"


def test_status_research_when_neither_channel_exists(conn, sample_icp):
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    sample_icp.qualify.hard = []  # isolate the phone/email gap from the separate no_phone DQ rule
    _seed_business(conn, category="community centre", categories=["community centre"], phone_e164=None)
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert s.tier != "DQ"
    assert next(f for f in s.factors if f.factor == "status").why == "RESEARCH"


def test_status_research_even_with_an_eligible_email_when_phone_not_validated(conn, sample_icp):
    """Phone-first design decision (documented, not just incidental): an eligible email without a
    validated phone still reads RESEARCH, not a 5th status — CALL_ONLY requires an actual number to
    dial, so the lead needs a human to find/confirm one before this phone-first workflow can act."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    sample_icp.qualify.hard = []
    _seed_business(conn, category="community centre", categories=["community centre"], phone_e164=None,
                   domain="indieauto.com")
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="info@indieauto.com",
                                 tier="valid", affinity="own_domain"))
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert next(f for f in s.factors if f.factor == "status").why == "RESEARCH"


def test_status_ready_is_still_reachable(conn, sample_icp):
    """The fourth status, for completeness alongside the three above (docs/09 §D §polish 'test all
    four statuses') — full arithmetic already covered by test_contactability_combined_matches_the_arithmetic."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    _seed_business(conn, phone_e164="+17135550100")
    db.add_person(conn, Person(business_id="biz_x", name="Jo Owner", title="Owner", is_dm=1,
                               dm_confidence=0.9, labeled_by="agent"))
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="info@indieauto.com",
                                 tier="valid", affinity="own_domain"))
    s = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))
    assert s.tier in ("A", "B")
    assert next(f for f in s.factors if f.factor == "status").why == "READY"


def test_scorer_threads_freemail_policy_from_cfg_not_the_hardcoded_default(conn, sample_icp):
    """v0.3 polish finding 4: `cfg.validation.freemail_policy` must actually reach the eligibility
    check Status's `why` explains itself with — proof it is really `self.cfg`, not the module-level
    `_DEFAULT_POLICY`, that gets threaded through. Status itself (CALL_ONLY) is unaffected by cfg
    either way — that invariant is finding 2's 'regardless of email eligibility' guarantee."""
    from leadforge.config import Config

    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    sample_icp.qualify.hard = []
    _seed_business(conn, category="community centre", categories=["community centre"],
                   phone_e164="+17135550100", domain="freemailer.example")
    db.add_contact(conn, Contact(business_id="biz_x", kind="email", value="owner@gmail.com",
                                 tier="valid", affinity="freemail_linked"))

    s_default = Scorer(conn, sample_icp, run_id).score_business(_row(conn, "biz_x"))  # cfg=None
    contact_default = next(f for f in s_default.factors if f.factor == "contactability")
    assert "email not eligible" not in contact_default.why  # 'linked' (the built-in default) is eligible

    strict_cfg = Config(validation={"freemail_policy": "none"})
    s_strict = Scorer(conn, sample_icp, run_id, cfg=strict_cfg).score_business(_row(conn, "biz_x"))
    contact_strict = next(f for f in s_strict.factors if f.factor == "contactability")
    status_strict = next(f for f in s_strict.factors if f.factor == "status")
    assert "email not eligible" in contact_strict.why       # cfg was actually threaded through
    assert status_strict.why == "CALL_ONLY"                 # status itself never depends on it


# --- v0.3 polish finding 6: two deterministic e2e-fixture businesses, exact totals -------------
def test_score_two_e2e_fixtures_have_exact_deterministic_totals(conn, sample_icp):
    """Locks in the scoring arithmetic against silent regressions: every factor input is fully
    specified and the expected total is hand-computed against scoring.default.yaml's weights
    (industry 25, need_signals 25, size 10, geography 10, business_model 10, data_confidence 20).

    Fixture A (strong fit): exact category match (25.0) + no website under a soft website_missing
    qualifier (0.7*25=17.5) + in-band reviews (10.0) + in-area address (10.0) + independent name
    (0.8*10=8.0) + place_id only, no registry/crawl (0.4*20=8.0) = 78.5, no negatives -> tier B.

    Fixture B (weak fit): unrelated category (0.1*25=2.5) + no need signal (0.2*25=5.0) + unknown
    review count (0.4*10=4.0) + out-of-area address (0.3*10=3.0) + franchise-y name (0.3*10=3.0,
    plus the -25 franchise_or_chain negative) + place_id only (0.4*20=8.0) = 25.5 - 25 = 0.5 -> tier C."""
    run_id = db.create_run(conn, "icp.yaml", sample_icp.icp_hash())
    bA = Business(id="det_a", name="Deterministic Auto A", name_norm="deterministic auto a",
                 dedupe_key="pid:det_a", place_id="DET_A", category="auto repair shop",
                 categories=["auto repair shop"], source="gosom", phone_e164="+17135550100",
                 website=None, rating=4.2, review_count=80, address_city="Houston", first_run_id="run_1")
    db.upsert_business(conn, bA)
    bB = Business(id="det_b", name="Acme Franchise Group", name_norm="acme franchise group",
                 dedupe_key="pid:det_b", place_id="DET_B", category="community centre",
                 categories=["community centre"], source="gosom", phone_e164="+17135550200",
                 website="https://example.com", rating=3.0, review_count=None,
                 address_city="Dallas", address_full="1 Main St, Dallas, TX 75201", first_run_id="run_1")
    db.upsert_business(conn, bB)
    scorer = Scorer(conn, sample_icp, run_id)
    sA = scorer.score_business(_row(conn, "det_a"))
    sB = scorer.score_business(_row(conn, "det_b"))
    assert sA.total == 78.5 and sA.tier == "B"
    assert sB.total == 0.5 and sB.tier == "C"
