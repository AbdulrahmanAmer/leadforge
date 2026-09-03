"""v0.3 unit C2 — runner.py persistence honesty, email affinity, GBP facts, dm origin.

No network: validate_email is monkeypatched where a test doesn't care about its own tiering.
"""
import json

from leadforge import db
from leadforge.enrich import runner
from leadforge.enrich.extract import PersonCandidate
from leadforge.models import Business, Person


def _biz_row(conn, biz_id, name, domain=None, website=None, phone_e164=None, address_country=None):
    db.upsert_business(conn, Business(id=biz_id, name=name, name_norm=name.casefold(), domain=domain,
                                      website=website, phone_e164=phone_e164, address_country=address_country,
                                      source="gosom", dedupe_key=f"dk:{biz_id}"))
    return conn.execute("SELECT * FROM businesses WHERE id=?", (biz_id,)).fetchone()


def _counts():
    return {"sites_crawled": 0, "contacts": 0, "dm_candidates": 0, "needs_browser": 0, "emails_valid": 0}


def _res(ok, emails=None, phones=None, error=""):
    return {"ok": ok, "emails": emails or {}, "phones": phones or [], "socials": {}, "people": [],
            "signals": {}, "needs_browser": False, "pages": 1 if ok else 0, "error": error}


# --- crawled_at / attempted_at honesty -----------------------------------------------------------
def test_crawl_failure_leaves_crawled_at_absent_and_attempted_at_set(cfg, conn):
    b = _biz_row(conn, "b1", "Failing Site", domain="failing.test", website="http://failing.test")
    runner._persist(conn, cfg, b, _res(False, error="home unreachable (timeout)"), _counts())
    enrich = json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()["enrich_json"])
    assert "crawled_at" not in enrich
    assert enrich["attempted_at"]
    assert enrich["error"] == "home unreachable (timeout)"


def test_crawl_success_sets_crawled_at_not_attempted_at(cfg, conn, monkeypatch):
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    b = _biz_row(conn, "b1", "Working Site", domain="working.test", website="http://working.test")
    runner._persist(conn, cfg, b, _res(True), _counts())
    enrich = json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()["enrich_json"])
    assert enrich["crawled_at"]
    assert "attempted_at" not in enrich


def test_crawl_stage_exception_sets_attempted_at_not_crawled_at(cfg, conn, monkeypatch):
    """The except-branch in _crawl_stage (a future that raised) must be as honest as _persist."""
    _biz_row(conn, "b1", "Boom Site", domain="boom.test", website="http://boom.test")

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner, "_process_one", boom)
    runner._crawl_stage(conn, cfg, 10, _counts())
    enrich = json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()["enrich_json"])
    assert "crawled_at" not in enrich
    assert enrich["attempted_at"]
    assert "kaboom" in enrich["error"]


# --- email evidence: ref_id + real snippet, not the bare address ---------------------------------
def test_email_evidence_ref_id_matches_contact_and_snippet_longer_than_address(cfg, conn, monkeypatch):
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("role", {}))
    b = _biz_row(conn, "b1", "Abbey Service Centre", domain="abbeyservice.co.uk",
                website="http://abbeyservice.co.uk")
    email = "info@abbeyservice.co.uk"
    text = ("Abbey Service Centre has looked after local drivers since 1990. "
            "For bookings or questions email info@abbeyservice.co.uk and we reply within a day.")
    from leadforge.enrich.extract import email_context
    res = _res(True, emails={email: {"label": "role", "url": "http://abbeyservice.co.uk",
                                     "context": email_context(text, email)}})
    runner._persist(conn, cfg, b, res, _counts())
    contact = conn.execute("SELECT * FROM contacts WHERE business_id='b1' AND value=?", (email,)).fetchone()
    assert contact is not None
    ev = conn.execute("SELECT * FROM evidence WHERE business_id='b1' AND fact='email_found'").fetchone()
    assert ev["ref_id"] == contact["id"]
    assert len(ev["snippet"]) > len(email)


# --- email affinity classification lands in the DB with the right tier --------------------------
def test_freemail_unlinked_lands_risky(cfg, conn, monkeypatch):
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    b = _biz_row(conn, "b1", "Abbey Service Centre", domain="abbeyservice.co.uk",
                website="http://abbeyservice.co.uk")
    email = "impallari@gmail.com"
    res = _res(True, emails={email: {"label": "personal", "url": "http://abbeyservice.co.uk",
                                     "context": f"Font by {email}, thanks!"}})
    counts = _counts()
    runner._persist(conn, cfg, b, res, counts)
    contact = conn.execute("SELECT * FROM contacts WHERE business_id='b1' AND value=?", (email,)).fetchone()
    assert contact["affinity"] == "freemail_unlinked"
    assert contact["tier"] == "risky"
    assert json.loads(contact["meta_json"])["reason"] == "freemail_unlinked"
    assert counts["emails_valid"] == 0  # never counted despite validate_email saying 'valid'


def test_own_domain_email_lands_own_domain_with_validated_tier(cfg, conn, monkeypatch):
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    b = _biz_row(conn, "b1", "Abbey Service Centre", domain="abbeyservice.co.uk",
                website="http://abbeyservice.co.uk")
    email = "info@abbeyservice.co.uk"
    res = _res(True, emails={email: {"label": "role", "url": "http://abbeyservice.co.uk", "context": email}})
    counts = _counts()
    runner._persist(conn, cfg, b, res, counts)
    contact = conn.execute("SELECT * FROM contacts WHERE business_id='b1' AND value=?", (email,)).fetchone()
    assert contact["affinity"] == "own_domain"
    assert contact["tier"] == "valid"
    assert counts["emails_valid"] == 1


# --- v0.3 polish (finding 1): a person named for the FIRST TIME on THIS crawl must still link -----
def test_freemail_linked_to_a_person_first_named_on_this_same_crawl(cfg, conn, monkeypatch):
    """Regression for the insert-order bug: _persist used to read existing people names BEFORE
    inserting this crawl's own res['people'], so a freemail box matching a person named for the
    first time on THIS SAME crawl (nothing in the DB yet) was misclassified freemail_unlinked purely
    because of insert order. The business name ("Abbey Service Centre") shares no token with
    "hoggarth", so a pass here can only come through the person-candidate link path, not the
    business-name link path -- isolating exactly the mechanism this test is regression-covering."""
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    b = _biz_row(conn, "b1", "Abbey Service Centre", domain="abbeyservice.co.uk",
                website="http://abbeyservice.co.uk")
    email = "johnhoggarth@live.co.uk"
    res = _res(True, emails={email: {"label": "personal", "url": "http://abbeyservice.co.uk",
                                     "context": f"Contact John Hoggarth, our workshop manager, at {email}."}})
    res["people"] = [PersonCandidate(name="John Hoggarth", title="Workshop Manager",
                                     snippet="John Hoggarth, our workshop manager", source_url=b["website"])]
    # sanity: nothing pre-exists in the DB for this business — the link must come from THIS crawl
    assert db.people_for(conn, "b1") == []
    counts = _counts()
    runner._persist(conn, cfg, b, res, counts)
    contact = conn.execute("SELECT * FROM contacts WHERE business_id='b1' AND value=?", (email,)).fetchone()
    assert contact["affinity"] == "freemail_linked", (
        f"expected freemail_linked via this crawl's own person candidate, got {contact['affinity']!r}"
    )
    assert contact["tier"] == "valid"  # linked freemail keeps its real tier, unlike freemail_unlinked


# --- v0.3 polish (finding 2): crawler.crawl must be called WITH business_domain in production -----
def test_process_one_passes_business_domain_to_crawler_and_signals_persist(cfg, conn, monkeypatch):
    """Regression: _process_one used to call crawler.crawl(website) with no business_domain, so
    crawl()'s own signals (final_host / offsite_redirect / http_status) were never computed for
    real in production — offsite_redirect stayed permanently absent/False no matter what the site
    actually did. A stub SiteCrawler records what business_domain it was actually given; a
    following _persist call proves the resulting signals reach the DB, not just runner's dict."""
    from leadforge.enrich.crawler import CrawlResult
    from leadforge.enrich.crawler import Page as CrawlerPage
    calls: dict = {}

    class FakeCrawler:
        def __init__(self, cfg_, throttle_):
            pass

        def crawl(self, website, business_domain=None):
            calls["website"] = website
            calls["business_domain"] = business_domain
            page = CrawlerPage(website, "<html><body>Acme Garage</body></html>", "Acme Garage")
            return CrawlResult(ok=True, pages=[page], needs_browser=False,
                               signals={"https": True, "status": 200, "http_status": 200,
                                        "final_host": "acmegarage.test", "offsite_redirect": False},
                               error="")

        def close(self):
            pass

        def _allowed(self, url):
            return True

    monkeypatch.setattr(runner, "SiteCrawler", FakeCrawler)
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    b = _biz_row(conn, "b1", "Acme Garage", domain="acmegarage.test", website="http://acmegarage.test")
    out = runner._process_one(cfg, None, b)
    assert calls["business_domain"] == "acmegarage.test", (
        "crawler.crawl() must be called with business_domain=b['domain'], not omitted — the kwarg "
        f"actually received was {calls.get('business_domain')!r}"
    )
    counts = _counts()
    runner._persist(conn, cfg, b, out, counts)
    enrich = json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()["enrich_json"])
    assert enrich["signals"]["final_host"] == "acmegarage.test"
    assert enrich["signals"]["offsite_redirect"] is False
    assert enrich["signals"]["http_status"] == 200


# --- v0.3 polish (finding 3): mailto-only evidence falls back to anchor + parent text --------------
def test_mailto_only_evidence_snippet_falls_back_to_anchor_parent_text():
    """When the address never appears in the page's extracted TEXT (an icon-only mailto anchor with
    no visible link text, inside a block trafilatura's own text extraction drops as boilerplate),
    the evidence snippet must fall back to the anchor's own text plus its parent element's text —
    not silently re-store the bare address, which proves nothing beyond 'this looks like an email'."""
    from leadforge.enrich import runner as r
    html = ('<div class="contact-block"><p>Have a question? '
            '<a href="mailto:sales@example.test" class="icon-envelope" aria-label="Email"></a> '
            'our friendly team any time.</p></div>')
    email = "sales@example.test"
    text = ""  # simulates the address never surfacing in trafilatura's extracted text
    snippet = r._email_evidence(html, text, email)
    assert snippet != email
    assert len(snippet) > 40, f"evidence snippet too short to prove anything beyond the address: {snippet!r}"
    assert "question" in snippet.casefold()


def test_mailto_only_evidence_snippet_no_matching_anchor_falls_back_to_bare_address():
    """No mailto anchor for this address anywhere in the HTML (e.g. it came from AT_DOT_RE text
    obfuscation, not markup) -> the bare address is the honest answer, not an error."""
    from leadforge.enrich import runner as r
    assert r._email_evidence("<html><body>no mailto here</body></html>", "", "ghost@example.test") \
        == "ghost@example.test"


def test_heuristic_person_row_gets_origin_heuristic(cfg, conn, monkeypatch):
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    b = _biz_row(conn, "b1", "Acme Garage", domain="acmegarage.test", website="http://acmegarage.test")
    res = _res(True)
    res["people"] = [PersonCandidate(name="Pat Owner", title="Owner", snippet="Pat Owner runs the shop.",
                                     source_url="http://acmegarage.test")]
    runner._persist(conn, cfg, b, res, _counts())
    pat = next(p for p in db.people_for(conn, "b1") if p["name"] == "Pat Owner")
    assert pat["origin"] == "heuristic"


# --- GBP facts (site-less path) -------------------------------------------------------------------
def test_gbp_reply_signature_becomes_people_row_origin_gbp(cfg, conn):
    _biz_row(conn, "b1", "Tyler's Garage")
    conn.execute(
        "UPDATE businesses SET enrich_json=?, maps_url=? WHERE id='b1'",
        (json.dumps({"gbp": {"reply_signatures": ["Tyler"], "review_names": [], "booking_links": []}}),
         "https://maps.google.com/?cid=1"),
    )
    conn.commit()
    counts = _counts()
    runner._gbp_stage(conn, cfg, counts)
    people = db.people_for(conn, "b1")
    tyler = next(p for p in people if p["name"] == "Tyler")
    assert tyler["origin"] == "gbp" and tyler["labeled_by"] == "gbp"
    assert tyler["snippet"] == "signed an owner reply on Google"
    assert tyler["source_url"] == "https://maps.google.com/?cid=1"
    assert counts["dm_candidates"] == 1


def test_gbp_review_name_credited_people_row(cfg, conn):
    _biz_row(conn, "b1", "No Site Autos")
    conn.execute(
        "UPDATE businesses SET enrich_json=? WHERE id='b1'",
        (json.dumps({"gbp": {"reply_signatures": [], "review_names": ["Priya", "Priya", "Priya"],
                             "booking_links": []}}),),
    )
    conn.commit()
    runner._gbp_stage(conn, cfg, _counts())
    priya = next(p for p in db.people_for(conn, "b1") if p["name"] == "Priya")
    assert priya["origin"] == "gbp"
    assert "3 reviews" in priya["snippet"]


def test_gbp_booking_links_set_booking_hint_via_gbp_stage(cfg, conn):
    _biz_row(conn, "b1", "No Site Autos")
    conn.execute(
        "UPDATE businesses SET enrich_json=? WHERE id='b1'",
        (json.dumps({"gbp": {"booking_links": ["https://booking.example/x"],
                             "reply_signatures": [], "review_names": []}}),),
    )
    conn.commit()
    runner._gbp_stage(conn, cfg, _counts())
    enrich = json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()["enrich_json"])
    assert enrich["signals"]["booking_hint"] is True
    assert enrich["signals"]["booking_source"] == "gbp"


def test_gbp_stage_query_skips_businesses_already_processed(cfg, conn):
    """The stage-level guard (WHERE ... NOT EXISTS origin='gbp') must stop re-processing a business on
    the next pass — checked with a DIFFERENT name on the second pass so _apply_gbp's own per-name
    de-dup (which would silently hide a broken stage-level guard) cannot be the thing saving this test."""
    _biz_row(conn, "b1", "Tyler's Garage")
    conn.execute(
        "UPDATE businesses SET enrich_json=? WHERE id='b1'",
        (json.dumps({"gbp": {"reply_signatures": ["Tyler"], "review_names": [], "booking_links": []}}),),
    )
    conn.commit()
    runner._gbp_stage(conn, cfg, _counts())
    assert len(db.people_for(conn, "b1")) == 1
    # a later discover re-run could plausibly change the gbp payload; the stage must still skip
    # this business on its next pass rather than adding "Bob" too
    conn.execute(
        "UPDATE businesses SET enrich_json=? WHERE id='b1'",
        (json.dumps({"gbp": {"reply_signatures": ["Bob"], "review_names": [], "booking_links": []}}),),
    )
    conn.commit()
    runner._gbp_stage(conn, cfg, _counts())
    people = db.people_for(conn, "b1")
    assert len(people) == 1 and people[0]["name"] == "Tyler"


# --- phone_confirmed signal ------------------------------------------------------------------------
def test_phone_confirmed_when_site_phone_matches_maps_phone(cfg, conn, monkeypatch):
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    b = _biz_row(conn, "b1", "Match Co", domain="matchco.test", website="http://matchco.test",
                phone_e164="+441234567890")
    res = _res(True, phones=["+441234567890"])
    runner._persist(conn, cfg, b, res, _counts())
    enrich = json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()["enrich_json"])
    assert enrich["signals"]["phone_confirmed"] is True


def test_phone_not_confirmed_when_site_phone_differs(cfg, conn, monkeypatch):
    monkeypatch.setattr(runner, "validate_email", lambda email, label, c: ("valid", {}))
    b = _biz_row(conn, "b1", "Mismatch Co", domain="mismatchco.test", website="http://mismatchco.test",
                phone_e164="+441234567890")
    res = _res(True, phones=["+449999999999"])
    runner._persist(conn, cfg, b, res, _counts())
    enrich = json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()["enrich_json"])
    assert enrich["signals"]["phone_confirmed"] is False


# --- registry profile persisted on the crawled path too (not just site-less) ---------------------
def test_registry_cross_check_persists_profile_on_crawled_path(cfg, conn, monkeypatch):
    from leadforge.providers.registry import CompaniesHouseRegistry
    b = _biz_row(conn, "b1", "Acme Widgets Ltd", domain="acmewidgets.test", website="http://acmewidgets.test",
                address_country="GB")
    cfg.registry.companies_house_key = "k"
    reg = CompaniesHouseRegistry(cfg)
    profile = {"company_number": "1", "company_status": "active", "legal_name": "ACME WIDGETS LTD",
               "match_similarity": 0.9, "incorporated": "2001-01-01", "sic_codes": ["45200"]}
    monkeypatch.setattr(reg, "lookup_with_profile", lambda biz: ([], profile))
    runner._registry_cross_check(conn, b, _counts(), [reg])
    enrich = json.loads(conn.execute("SELECT enrich_json FROM businesses WHERE id='b1'").fetchone()["enrich_json"])
    assert enrich["registry_checked"] is True
    assert enrich["registry_profile"]["company_number"] == "1"


def test_auto_pick_from_cross_check_only_when_profile_active(cfg, conn, monkeypatch):
    from leadforge.providers.registry import CompaniesHouseRegistry
    b = _biz_row(conn, "b1", "Acme Widgets Ltd", domain="acmewidgets.test", website="http://acmewidgets.test",
                address_country="GB")
    cfg.registry.companies_house_key = "k"
    reg = CompaniesHouseRegistry(cfg)
    person = Person(business_id="b1", name="Jane Smith", title="Director", labeled_by="registry")
    from leadforge.models import Evidence
    ev = Evidence(business_id="b1", ref_table="people", fact="registry_officer", url="u", snippet="s")
    dissolved_profile = {"company_number": "1", "company_status": "dissolved"}
    monkeypatch.setattr(reg, "lookup_with_profile", lambda biz: ([(person, ev)], dissolved_profile))
    runner._registry_cross_check(conn, b, _counts(), [reg])
    people = db.people_for(conn, "b1")
    assert all(p["is_dm"] == 0 for p in people)  # dissolved profile -> never auto-picked


# --- dm.py: origin travels through export -> apply ------------------------------------------------
def test_dm_export_ndjson_includes_origin_and_apply_keeps_it(cfg, conn, sample_icp, tmp_path):
    from leadforge.enrich.dm import apply_labels, export_batch
    _biz_row(conn, "b1", "Acme Garage")
    db.add_person(conn, Person(business_id="b1", name="Jane Registry", title="Director",
                               labeled_by="registry", origin="registry"))
    out_path = tmp_path / "batch.ndjson"
    n, _remaining = export_batch(conn, sample_icp, out_path, max_biz=10, tsv=False)
    assert n == 1
    rec = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert rec["candidates"][0]["origin"] == "registry"

    labels = tmp_path / "labels.ndjson"
    labels.write_text(json.dumps({"biz": "b1", "pick": 0, "confidence": 0.9}) + "\n", encoding="utf-8")
    applied = apply_labels(conn, labels)
    assert applied["applied"] == 1
    row = db.people_for(conn, "b1")[0]
    assert row["labeled_by"] == "agent"   # relabeled by the agent's decision...
    assert row["origin"] == "registry"    # ...but origin still shows where the candidate came from


def test_dm_export_tsv_includes_origin_column(cfg, conn, sample_icp, tmp_path):
    from leadforge.enrich.dm import export_batch
    _biz_row(conn, "b1", "Acme Garage")
    db.add_person(conn, Person(business_id="b1", name="Sam Heuristic", title="Owner",
                               labeled_by="heuristic", origin="heuristic"))
    out_path = tmp_path / "batch.tsv"
    export_batch(conn, sample_icp, out_path, max_biz=10, tsv=True)
    cols = out_path.read_text(encoding="utf-8").strip().split("\t")
    assert cols[-1] == "heuristic"


def test_dm_export_origin_falls_back_to_labeled_by_when_blank(cfg, conn, sample_icp, tmp_path):
    """A row inserted before v0.3 (or by db.add_person's own labeled_by fallback) has origin=''."""
    from leadforge.enrich.dm import export_batch
    _biz_row(conn, "b1", "Acme Garage")
    # add_person defaults origin to labeled_by when none is given (see db.add_person)
    db.add_person(conn, Person(business_id="b1", name="Old Row", title="Manager", labeled_by="heuristic"))
    out_path = tmp_path / "batch.ndjson"
    export_batch(conn, sample_icp, out_path, max_biz=10, tsv=False)
    rec = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert rec["candidates"][0]["origin"] == "heuristic"



def test_registry_gate_accepts_country_names_not_only_iso_codes():
    """DVSA and maps_list rows carry 'United Kingdom'; the gate must treat that as GB (2026-09-03 bug)."""
    from leadforge.enrich.runner import _jurisdiction

    assert _jurisdiction("United Kingdom") == "GB"
    assert _jurisdiction("GB") == "GB"
    assert _jurisdiction("gb") == "GB"
    assert _jurisdiction("England") == "GB"
    assert _jurisdiction(None) == ""
