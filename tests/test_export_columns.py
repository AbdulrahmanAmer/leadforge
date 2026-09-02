"""v0.3 D — export truth: the new default-profile columns are present, non-blank (zero-blank rule),
Next Action is phone-first, and a freemail address never outranks an own-domain one in the Email
column (docs/09 §D acceptance)."""
import csv

from leadforge import db
from leadforge.config import load_config
from leadforge.export import ACCOUNT_COLUMNS, DEFAULT_EXTRA_COLUMNS, export_run
from leadforge.models import ICP, Business, Contact, Person, Score, ScoreFactor


def _icp():
    return ICP.model_validate({"campaign": "t", "offer": {"what": "web design"},
                               "target": {"categories": ["auto repair shop"],
                                          "geography": {"areas": ["Houston, TX"], "country": "US"}}})


def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    rid = db.create_run(conn, "icp.yaml", "h")
    return cfg, conn, rid


def _score(conn, rid, bid):
    db.save_score(conn, Score(business_id=bid, run_id=rid, total=70, tier="B",
                              factors=[ScoreFactor(factor="x", group="fit", weight=1, score=1,
                                                   points=1, why="w")]))


def _csv_rows(arts):
    path = next(a for a in arts if a.endswith(".csv"))
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def test_new_columns_present_and_never_blank(tmp_path, monkeypatch):
    cfg, conn, rid = _bootstrap(tmp_path, monkeypatch)
    db.upsert_business(conn, Business(id="b1", name="Bare Garage", source="gosom", dedupe_key="dk-b1"))
    _score(conn, rid, "b1")
    arts = export_run(conn, _icp(), rid, cfg.exports_dir, ["csv"], cfg=cfg)
    row = _csv_rows(arts)[0]
    for col in DEFAULT_EXTRA_COLUMNS:
        assert col in row, f"missing column {col}"
        assert row[col] not in ("", None), f"{col} is blank on a bare-minimum row"
    # a bare business with no score-side meta factors still gets an honest placeholder, not a crash
    assert row["Contactability"] == "not computed"
    assert row["Status"] == "not computed"
    assert row["Entity Type"] == "unchecked"
    assert row["Chain"] == "-"


def test_account_fit_export_unaffected_by_the_new_columns(tmp_path, monkeypatch):
    """account_fit rows keep their own column set — the v0.3 D columns are additive to the default
    profile only, per the unit brief ('Keep account_fit untouched except sharing helpers')."""
    cfg, conn, rid = _bootstrap(tmp_path, monkeypatch)
    icp = ICP.model_validate({"campaign": "t", "offer": {"what": "x"},
                              "target": {"categories": ["manufacturer"],
                                         "geography": {"areas": ["Dubai"], "country": "AE"}},
                              "scoring": {"profile": "account_fit"}})
    db.upsert_business(conn, Business(id="b1", name="Acme", source="gosom", dedupe_key="dk-b1"))
    db.save_score(conn, Score(business_id="b1", run_id=rid, total=70, tier="B",
                              factors=[ScoreFactor(factor="status", group="meta", weight=0, score=0,
                                                   points=0, why="NEW")]))
    arts = export_run(conn, icp, rid, cfg.exports_dir, ["csv"], cfg=cfg)
    row = _csv_rows(arts)[0]
    for col in ("Fit", "Next Action", "Entity Type", "Lawful Basis (Email)"):
        assert col not in row
    assert "Status" in row and row["Status"] == "NEW"
    assert set(ACCOUNT_COLUMNS) == set(row.keys())


def test_next_action_is_phone_first(tmp_path, monkeypatch):
    cfg, conn, rid = _bootstrap(tmp_path, monkeypatch)
    db.upsert_business(conn, Business(id="named", name="Named Garage", source="gosom", dedupe_key="dk-named",
                                      phone_e164="+441483123456"))
    db.add_person(conn, Person(business_id="named", name="Jo Owner", title="Owner", is_dm=1,
                               dm_confidence=0.9, labeled_by="agent"))
    _score(conn, rid, "named")

    db.upsert_business(conn, Business(id="switch", name="Switch Garage", source="gosom",
                                      dedupe_key="dk-switch", phone_e164="+441483123457"))
    _score(conn, rid, "switch")

    db.upsert_business(conn, Business(id="mail", name="Mail Garage", source="gosom", dedupe_key="dk-mail",
                                      domain="mailgarage.example"))
    db.add_contact(conn, Contact(business_id="mail", kind="email", value="info@mailgarage.example",
                                 tier="valid", affinity="own_domain"))
    _score(conn, rid, "mail")

    arts = export_run(conn, _icp(), rid, cfg.exports_dir, ["csv"], cfg=cfg)
    rows = {r["Business"]: r for r in _csv_rows(arts)}
    # a validated phone always wins Next Action even when a DM is present (phone-first, owner decision 2)
    assert rows["Named Garage"]["Next Action"] == "CALL - named contact"
    assert rows["Switch Garage"]["Next Action"] == "CALL - ask for the owner"
    assert rows["Mail Garage"]["Next Action"] == "EMAIL - eligible"


def test_email_confidence_agrees_with_lawful_basis_on_legacy_rows(tmp_path, monkeypatch):
    """Real-data proof caught this: a pre-v0.3 contact (no `affinity` stored) showed Lawful Basis
    'b2b_legitimate_interest' but Email Confidence 'none' for the SAME address — two readings of one
    fact. Both must now agree that a legacy own-domain role address is exactly that."""
    cfg, conn, rid = _bootstrap(tmp_path, monkeypatch)
    db.upsert_business(conn, Business(id="legacy", name="Legacy Garage", source="gosom",
                                      dedupe_key="dk-legacy", domain="legacygarage.example"))
    db.add_contact(conn, Contact(business_id="legacy", kind="email", value="info@legacygarage.example",
                                 tier="role"))  # affinity left unset, as every pre-v0.3 row is
    _score(conn, rid, "legacy")
    arts = export_run(conn, _icp(), rid, cfg.exports_dir, ["csv"], cfg=cfg)
    row = _csv_rows(arts)[0]
    assert row["Email Confidence"] == "own domain, role mailbox"
    assert row["Lawful Basis (Email)"] == "b2b_legitimate_interest"


def test_freemail_never_outranks_own_domain_in_the_email_column(tmp_path, monkeypatch):
    """The v0.2 sheet exported a font designer's gmail above a real info@ three times — this is the
    regression test for the fix: a freemail 'valid' address must never beat an own-domain 'role' one."""
    cfg, conn, rid = _bootstrap(tmp_path, monkeypatch)
    db.upsert_business(conn, Business(id="dual", name="Dual Garage", source="gosom", dedupe_key="dk-dual",
                                      domain="dualgarage.example"))
    db.add_contact(conn, Contact(business_id="dual", kind="email", value="jane.font@gmail.com",
                                 tier="valid", affinity="freemail_linked"))
    db.add_contact(conn, Contact(business_id="dual", kind="email", value="info@dualgarage.example",
                                 tier="role", affinity="own_domain"))
    _score(conn, rid, "dual")
    arts = export_run(conn, _icp(), rid, cfg.exports_dir, ["csv"], cfg=cfg)
    row = _csv_rows(arts)[0]
    assert row["Email"] == "info@dualgarage.example"
    assert row["Email Tier"] == "role"
    assert row["Email Confidence"] == "own domain, role mailbox"
