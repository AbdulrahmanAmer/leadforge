"""WE SCORE account-intel profile + account_fit scorer (v0.1.1)."""
import json

from leadforge import db
from leadforge.config import load_config
from leadforge.enrich import profile as prof
from leadforge.models import ICP, Business, Contact, Person


def test_detect_tech_tri_state():
    html = '<script src="https://js.hs-scripts.com/123.js"></script> we run Odoo for our back office'
    out = prof.detect_tech(html, "we run Odoo and a WMS for warehouse management system needs",
                           ["acme-com.mail.protection.outlook.com"])
    assert out["microsoft_365"]["value"] == "yes" and out["microsoft_365"]["state"] == "CONFIRMED"
    assert out["crm"]["value"] == "yes" and out["crm"]["name"] == "hubspot"
    assert out["erp"]["value"] == "yes" and out["erp"]["name"] == "odoo"
    assert "wms" in out["other"]


def test_unknown_is_never_no():
    out = prof.detect_tech("", "", None)
    for key in ("microsoft_365", "crm", "erp"):
        assert out[key]["state"] == "UNKNOWN" and out[key]["value"] is None


def test_departments_and_employees():
    text = ("Our Operations team works with the IT department daily. "
            "Head of Finance reports monthly. We are a team of 120 people.")
    depts = prof.detect_departments(text)
    assert "operations" in depts and "it" in depts and "finance" in depts
    emp = prof.estimate_employees(text, "http://x/about")
    assert emp["value"] == 120 and emp["state"] == "ESTIMATED"
    assert prof.employee_range(120) == "50-500"
    assert prof.employee_range(30) == "20-49"
    assert prof.employee_range(None) == "unknown"


def test_triggers_and_freshness():
    text = "In 2026 we announced a factory expansion and a new production line in Dubai."
    trigs = prof.find_triggers(text, "manufacturing", "http://x/news")
    assert trigs and trigs[0]["strength"] == "strong"
    assert trigs[0]["date"] == "2026-01-01"
    assert prof.trigger_freshness("2026-08-01", "2026-08-31") == "VERY_STRONG"
    assert prof.trigger_freshness("2026-01-01", "2026-08-31") == "MEDIUM"
    assert prof.trigger_freshness(None, "2026-08-31") == "UNKNOWN"
    assert prof.trigger_freshness("2020-01-01", "2026-08-31") == "LOW"


def _icp():
    return ICP.model_validate({
        "campaign": "t", "offer": {"what": "systems integration"},
        "target": {"categories": ["manufacturer"],
                   "geography": {"areas": ["Dubai"], "country": "AE"}},
        "scoring": {"profile": "account_fit"}})


def _seed(tmp_path, monkeypatch, profile_json, category="Manufacturer"):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    db.upsert_business(conn, Business(id="b1", run_id="r", name="Acme Factory", source="gosom",
                                      category=category, phone_e164="+97141234567"))
    conn.execute("UPDATE businesses SET enrich_json=? WHERE id='b1'",
                 (json.dumps({"profile": profile_json, "socials": {}}),))
    conn.commit()
    return conn


def test_account_fit_strong_prospect(tmp_path, monkeypatch):
    from leadforge.score import score_account_fit
    profile_json = {
        "employee_count": {"value": 200, "state": "ESTIMATED", "source": "u"},
        "employee_range": "50-500",
        "revenue": {"value": None, "state": "UNKNOWN", "source": ""},
        "departments": ["operations", "it", "finance"],
        "tech": {"microsoft_365": {"value": "yes", "state": "CONFIRMED", "source": "mx"},
                 "crm": {"value": "yes", "state": "CONFIRMED", "source": "x", "name": "hubspot"},
                 "erp": {"value": None, "state": "UNKNOWN", "source": ""}, "other": ["wms"]},
        "triggers": [{"category": "manufacturing", "text": "factory expansion announced",
                      "url": "http://x", "strength": "strong", "date": None}],
    }
    conn = _seed(tmp_path, monkeypatch, profile_json)
    db.add_person(conn, Person(business_id="b1", name="Op Manager", title="Operations Director",
                               labeled_by="registry", is_dm=1, dm_confidence=0.9))
    db.add_contact(conn, Contact(business_id="b1", kind="email", value="ops@acme.ae",
                                 label="role", tier="valid"))
    b = db.all_businesses(conn)[0]
    s = score_account_fit(conn, _icp(), "r", b)
    # 15 industry + 15 size + 0 revenue(unknown) + 15 growth + 10 complexity + 10 tech + 20 trigger = 85
    assert s.total == 85.0 and s.tier == "A"
    meta = {f.factor: f for f in s.factors if f.group == "meta"}
    assert meta["contactability"].points == 65  # DM 30 + valid email 30 + switchboard 5
    assert meta["data_confidence"].points > 50
    assert meta["status"].why == "READY_FOR_OUTREACH"


def test_account_fit_unknowns_do_not_disqualify(tmp_path, monkeypatch):
    from leadforge.score import score_account_fit
    conn = _seed(tmp_path, monkeypatch, {})  # nothing known
    b = db.all_businesses(conn)[0]
    s = score_account_fit(conn, _icp(), "r", b)
    assert s.tier != "DQ"  # unknown employees never disqualify
    meta = {f.factor: f for f in s.factors if f.group == "meta"}
    assert meta["data_confidence"].points < 40


def test_account_fit_confirmed_tiny_company_is_dq(tmp_path, monkeypatch):
    from leadforge.score import score_account_fit
    conn = _seed(tmp_path, monkeypatch, {
        "employee_count": {"value": 8, "state": "ESTIMATED", "source": "u"},
        "employee_range": "<20"})
    b = db.all_businesses(conn)[0]
    s = score_account_fit(conn, _icp(), "r", b)
    assert s.tier == "DQ"
    meta = {f.factor: f for f in s.factors if f.group == "meta"}
    assert meta["status"].why.startswith("DISQUALIFIED")


def test_account_fit_wrong_industry_manual_review(tmp_path, monkeypatch):
    from leadforge.score import score_account_fit
    conn = _seed(tmp_path, monkeypatch, {"employee_range": "50-500",
                                         "employee_count": {"value": 100, "state": "ESTIMATED", "source": "u"}},
                 category="Pet groomer")
    b = db.all_businesses(conn)[0]
    s = score_account_fit(conn, _icp(), "r", b)
    meta = {f.factor: f for f in s.factors if f.group == "meta"}
    assert meta["status"].why == "MANUAL_REVIEW"


def test_wescore_example_icp_compiles(tmp_path, monkeypatch):
    import shutil as sh
    from pathlib import Path

    from leadforge.intake import compile_icp
    monkeypatch.chdir(tmp_path)
    src = Path(__file__).parent.parent / "config" / "icp.wescore.example.yaml"
    sh.copy(src, tmp_path / "answers.yaml")
    icp, warns = compile_icp(tmp_path / "answers.yaml", tmp_path / "icp.yaml")
    assert icp.scoring.profile == "account_fit"
