"""v0.3 unit F — build_packet: only-evidenced facts, the token budget, the name-gate policy and the
A/B/C grade rules (docs/09 Wave 2 F acceptance)."""

from leadforge import db
from leadforge.config import load_config
from leadforge.draft.packet import build_packet, tokens_est
from leadforge.models import ICP, Business, Contact, Person


def _icp(region="uk"):
    return ICP.model_validate({
        "campaign": "t", "offer": {"what": "web design", "value_prop": "more bookings", "sender": "GainLev"},
        "target": {"categories": ["auto repair shop"], "geography": {"areas": ["Leeds"], "country": "GB"}},
        "compliance": {"region_profile": region},
    })


def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    return cfg, conn


_IDENTITY = {"from_name": "GainLev", "label": "gainlev-main"}


def _biz(conn, **over):
    base = dict(id="b1", name="Acme Garage", category="Car repair", address_city="Leeds",
               source="gosom", dedupe_key="dk-b1")
    base.update(over)
    db.upsert_business(conn, Business(**base))
    return conn.execute("SELECT * FROM businesses WHERE id=?", (base["id"],)).fetchone()


# ------------------------------------------------------------------------- only-evidenced facts
def test_bare_business_yields_no_facts_and_grade_c(tmp_path, monkeypatch):
    """A business with a website (so no_website never fires), no crawl, no registry match and no
    rating/review_count data yields a literally empty fact list — 'insufficient_evidence', not a
    fabricated segment claim."""
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    b = _biz(conn, category=None, address_city=None, website="https://acmegarage.example")
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["facts"] == []
    assert packet["grade"] == "C"


def test_registry_fact_gated_on_similarity_and_active_status(tmp_path, monkeypatch):
    """A registry hit below cfg.registry.min_name_similarity, or not active, never becomes a fact —
    the plain-language guardrail from docs/09 §D/§F, not just a UI label."""
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    low_sim = {"registry_profile": {"legal_name": "Wrong Co Ltd", "incorporated": "2010-01-01",
                                    "company_status": "active", "match_similarity": 0.1}}
    b = _biz(conn, enrich=low_sim)
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert not any(f["k"] in ("legal_name", "incorporated_year", "company_status") for f in packet["facts"])

    dissolved = {"registry_profile": {"legal_name": "Acme Garage Ltd", "incorporated": "2010-01-01",
                                      "company_status": "dissolved", "match_similarity": 0.95}}
    b2 = _biz(conn, id="b2", dedupe_key="dk-b2", enrich=dissolved)
    packet2 = build_packet(conn, cfg, _icp(), b2, None, "gainlev_leadgen", _IDENTITY)
    assert not any(f["k"] == "legal_name" for f in packet2["facts"])


def test_registry_fact_present_when_gate_clears(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    good = {"registry_profile": {"legal_name": "Acme Garage Ltd", "incorporated": "2015-05-01",
                                 "company_status": "active", "match_similarity": 0.9}}
    b = _biz(conn, enrich=good)
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    keys = {f["k"]: f["v"] for f in packet["facts"]}
    assert keys["legal_name"] == "Acme Garage Ltd"
    assert keys["incorporated_year"] == "2015"
    assert keys["company_status"] == "active"


def test_hooks_require_a_real_non_phantom_crawl(tmp_path, monkeypatch):
    """A 'phantom' crawl (crawled_at stamped but 0 pages fetched — 115 of these in the live campaign,
    docs/09) must not manufacture site_stale/hiring/no_social_link/phone_confirmed facts."""
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    phantom = {"crawled_at": "2026-01-01T00:00:00Z", "pages": 0,
              "signals": {"stale_site": True, "copyright_year": 2018, "careers": True, "phone_confirmed": True},
              "socials": {}}
    b = _biz(conn, enrich=phantom, website="https://acmegarage.example")
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    site_keys = {"site_stale", "hiring", "no_social_link", "phone_confirmed"}
    assert not (site_keys & {f["k"] for f in packet["facts"]})


def test_no_website_fact_only_when_website_missing(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    b_no_site = _biz(conn, website=None)
    packet = build_packet(conn, cfg, _icp(), b_no_site, None, "gainlev_leadgen", _IDENTITY)
    assert any(f["k"] == "no_website" for f in packet["facts"])

    b_with_site = _biz(conn, id="b2", dedupe_key="dk-b2", website="https://acmegarage.example")
    packet2 = build_packet(conn, cfg, _icp(), b_with_site, None, "gainlev_leadgen", _IDENTITY)
    assert not any(f["k"] == "no_website" for f in packet2["facts"])


def test_gbp_booking_link_is_evidence_even_with_no_site_crawl(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    gbp = {"gbp": {"booking_links": ["https://gbp.example/book"], "appointments": "none",
                   "review_names": [], "reply_signatures": []}}
    b = _biz(conn, website=None, enrich=gbp)
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    booking = next(f for f in packet["facts"] if f["k"] == "booking")
    assert booking["src"] == "gbp"


# ------------------------------------------------------------------------- name policy
def test_registry_name_without_corroboration_stays_company_greeting(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    good_registry = {"registry_profile": {"legal_name": "Acme Garage Ltd", "incorporated": "2015-05-01",
                                          "company_status": "active", "match_similarity": 0.9}}
    b = _biz(conn, enrich=good_registry)
    db.add_person(conn, Person(business_id="b1", name="Zed Quibble", title="Director", is_dm=1,
                               dm_confidence=0.9, labeled_by="registry", origin="registry"))
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["greeting"] == "Hello,"
    assert not any(f["k"] == "dm_name" for f in packet["facts"])


def test_registry_name_corroborated_by_email_local_part_unlocks_greeting(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    good_registry = {"registry_profile": {"legal_name": "Acme Garage Ltd", "incorporated": "2015-05-01",
                                          "company_status": "active", "match_similarity": 0.9}}
    b = _biz(conn, enrich=good_registry, domain="acmegarage.co.uk")
    db.add_person(conn, Person(business_id="b1", name="Smith, Sarah", title="Director", is_dm=1,
                               dm_confidence=0.9, labeled_by="registry", origin="registry"))
    cid = db.add_contact(conn, Contact(business_id="b1", kind="email", value="sarah@acmegarage.co.uk",
                                       tier="valid", affinity="own_domain"))
    contact_row = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    packet = build_packet(conn, cfg, _icp(), b, contact_row, "gainlev_leadgen", _IDENTITY)
    assert packet["greeting"] == "Hi Sarah,"
    dm_fact = next(f for f in packet["facts"] if f["k"] == "dm_name")
    assert dm_fact["v"] == "Sarah Smith"


def test_heuristic_site_sourced_name_needs_no_registry_gate(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    b = _biz(conn)
    db.add_person(conn, Person(business_id="b1", name="Jo Owner", title="Owner", is_dm=1,
                               dm_confidence=0.9, labeled_by="agent", origin="heuristic"))
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["greeting"] == "Hi Jo,"


def test_name_policy_never_forces_company_greeting(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    cfg.draft.name_policy = "never"
    b = _biz(conn)
    db.add_person(conn, Person(business_id="b1", name="Jo Owner", title="Owner", is_dm=1,
                               dm_confidence=0.9, labeled_by="agent", origin="heuristic"))
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["greeting"] == "Hello,"
    assert not any(f["k"] == "dm_name" for f in packet["facts"])


def test_name_policy_always_bypasses_the_corroboration_gate(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    cfg.draft.name_policy = "always"
    good_registry = {"registry_profile": {"legal_name": "Acme Garage Ltd", "incorporated": "2015-05-01",
                                          "company_status": "active", "match_similarity": 0.9}}
    b = _biz(conn, enrich=good_registry)
    db.add_person(conn, Person(business_id="b1", name="Zed Quibble", title="Director", is_dm=1,
                               dm_confidence=0.9, labeled_by="registry", origin="registry"))
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["greeting"] == "Hi Zed,"


# ------------------------------------------------------------------------- grade rules
def test_grade_a_requires_allowed_name_and_a_distinctive_fact(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    enrich = {"crawled_at": "2026-01-01T00:00:00Z", "pages": 2,
             "signals": {"stale_site": True, "copyright_year": 2018}}
    b = _biz(conn, enrich=enrich, domain="acmegarage.co.uk")
    db.add_person(conn, Person(business_id="b1", name="Jo Owner", title="Owner", is_dm=1,
                               dm_confidence=0.9, labeled_by="agent", origin="heuristic"))
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["grade"] == "A"


def test_grade_b_is_a_distinctive_fact_without_an_allowed_name(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    enrich = {"crawled_at": "2026-01-01T00:00:00Z", "pages": 2,
             "signals": {"stale_site": True, "copyright_year": 2018}}
    b = _biz(conn, enrich=enrich)
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["grade"] == "B"
    assert packet["greeting"] == "Hello,"


def test_grade_c_is_segment_facts_only(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    b = _biz(conn, website=None)
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["grade"] == "C"
    assert any(f["k"] == "no_website" for f in packet["facts"])
    assert not (set(f["k"] for f in packet["facts"]) & {
        "legal_name", "booking", "site_stale", "hiring", "phone_confirmed", "gbp_appointments", "review_name",
    })


# ------------------------------------------------------------------------- token budget
def test_packet_stays_within_the_configured_token_budget(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    enrich = {
        "crawled_at": "2026-01-01T00:00:00Z", "pages": 3,
        "signals": {"stale_site": True, "copyright_year": 2018, "careers": True, "phone_confirmed": True,
                   "booking_hint": True, "booking_source": "regex"},
        "socials": {},
        "registry_profile": {"legal_name": "Acme Garage Ltd", "incorporated": "2015-05-01",
                             "company_status": "active", "match_similarity": 0.9},
        "gbp": {"appointments": "recommended", "booking_links": [], "review_names": ["Dave"], "reply_signatures": []},
    }
    b = _biz(conn, enrich=enrich, rating=4.6, review_count=88, domain="acmegarage.co.uk")
    db.add_person(conn, Person(business_id="b1", name="Smith, Sarah", title="Director", is_dm=1,
                               dm_confidence=0.9, labeled_by="registry", origin="registry"))
    cid = db.add_contact(conn, Contact(business_id="b1", kind="email", value="sarah@acmegarage.co.uk",
                                       tier="valid", affinity="own_domain"))
    contact_row = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()

    cfg.draft.packet_max_tokens = 120  # tiny relative to the ~345-token full packet, forces real trimming
    packet = build_packet(conn, cfg, _icp(), b, contact_row, "gainlev_leadgen", _IDENTITY)
    assert tokens_est(packet) <= 120
    # dm_name is dropped LAST (it is not in the drop-priority list at all) — a trimmed packet must
    # never keep a low-value baseline fact while silently discarding the name-gate result.
    assert any(f["k"] == "dm_name" for f in packet["facts"])
    assert not any(f["k"] == "category" for f in packet["facts"])  # baseline facts go first


def test_packet_never_drops_offer_sender_or_purpose_regardless_of_budget(tmp_path, monkeypatch):
    cfg, conn = _bootstrap(tmp_path, monkeypatch)
    b = _biz(conn)
    cfg.draft.packet_max_tokens = 1  # impossible budget
    packet = build_packet(conn, cfg, _icp(), b, None, "gainlev_leadgen", _IDENTITY)
    assert packet["offer"] == {"what": "web design", "value_prop": "more bookings"}
    assert packet["sender"] == {"from_name": "GainLev", "label": "gainlev-main"}
    assert packet["purpose"] == "gainlev_leadgen"
