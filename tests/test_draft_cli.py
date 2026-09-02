"""v0.3 unit F — the `leadforge draft` CLI end to end: export -> agent writes -> apply (gated) ->
render, plus `check` standalone (docs/09 Wave 2 F acceptance)."""

import json
import subprocess
import sys
from pathlib import Path

from leadforge import db
from leadforge.config import load_config
from leadforge.models import Business, Contact, Person, Score, ScoreFactor

FIXTURES = Path(__file__).parent / "fixtures" / "draft"

ICP_YAML = (
    "campaign: test-camp\n"
    "offer:\n  what: website refresh\n  value_prop: more bookings\n  sender: GainLev\n"
    "target:\n  categories: [auto repair shop]\n  geography:\n    areas: [Leeds]\n    country: GB\n"
    "compliance:\n  region_profile: uk\n"
)


def _run(args, cwd):
    return subprocess.run([sys.executable, "-m", "leadforge", *args], cwd=cwd,
                          capture_output=True, encoding="utf-8")


def _digest(output: str) -> dict:
    lines = [ln for ln in output.splitlines() if ln.startswith("LF_DIGEST ")]
    assert len(lines) == 1, f"expected exactly one digest line, got {len(lines)}: {output!r}"
    return json.loads(lines[0][len("LF_DIGEST "):])


def _ndjson(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _seed(tmp_path, *, business=None, person=None, contact=None, tier="A", identity=None):
    (tmp_path / "icp.yaml").write_text(ICP_YAML, encoding="utf-8")
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    rid = db.create_run(conn, "icp.yaml", "h")
    base = dict(id="b1", name="Acme Garage", category="Car repair", address_city="Leeds",
               source="gosom", dedupe_key="dk-b1")
    base.update(business or {})
    db.upsert_business(conn, Business(**base))
    if person:
        db.add_person(conn, Person(**person))
    if contact:
        db.add_contact(conn, Contact(**contact))
    if identity:
        conn.execute(
            "INSERT INTO sending_identities(label,from_name,postal_address,privacy_url,unsubscribe_mailto,"
            "unsubscribe_url,created_at) VALUES(?,?,?,?,?,?,?)",
            (identity["label"], identity["from_name"], identity.get("postal_address", ""),
             identity.get("privacy_url", ""), identity.get("unsubscribe_mailto", ""),
             identity.get("unsubscribe_url", ""), "2026-01-01T00:00:00Z"),
        )
    db.save_score(conn, Score(business_id=base["id"], run_id=rid, total=85, tier=tier,
                              factors=[ScoreFactor(factor="x", group="fit", weight=1, score=1, points=1, why="w")]))
    conn.commit()
    conn.close()
    return rid


# ------------------------------------------------------------------------- export
def test_export_standalone_run_mode_creates_minimal_targets(tmp_path):
    rid = _seed(tmp_path, tier="A")
    res = _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "gainlev_leadgen",
               "--run", rid, "--tier", "A"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is True, res.stdout + res.stderr
    assert d["counts"]["targets"] == 1
    lines = _ndjson(tmp_path / "drafts_packets.ndjson")
    assert len(lines) == 2  # header + 1 packet
    assert "instructions" in lines[0]
    assert lines[1]["packet"]["purpose"] == "gainlev_leadgen"

    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    targets = conn.execute("SELECT * FROM outreach_targets WHERE campaign='test-camp'").fetchall()
    assert len(targets) == 1 and targets[0]["state"] == "enrolled"


def test_export_rejects_unknown_purpose(tmp_path):
    _seed(tmp_path, tier="A")
    res = _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "not_a_purpose"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False
    assert "purpose" in " ".join(d["warnings"]).lower()


# ------------------------------------------------------------------------- apply
def test_apply_stores_a_clean_draft_and_advances_the_target(tmp_path):
    rid = _seed(tmp_path, business={"website": None}, tier="A")
    _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "gainlev_leadgen",
         "--run", rid, "--tier", "A"], tmp_path)
    lines = _ndjson(tmp_path / "drafts_packets.ndjson")
    target_id = lines[1]["target"]
    (tmp_path / "drafts.ndjson").write_text(json.dumps({
        "target": target_id, "subject": "Quick note about Acme Garage",
        "observation": "Noticed you don't have a business website live yet.",
        "used_fact": "no_website",
    }) + "\n", encoding="utf-8")

    res = _run(["--json", "draft", "apply", "--in", "drafts.ndjson"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is True, res.stdout + res.stderr
    assert d["counts"] == {"applied": 1, "rejected": 0, "insufficient_evidence": 0, "skipped": 0}

    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    t = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (target_id,)).fetchone()
    assert t["state"] == "drafted"
    m = conn.execute("SELECT * FROM messages WHERE target_id=?", (target_id,)).fetchone()
    assert m["state"] == "drafted"
    assert m["used_fact"] == "no_website"
    assert m["draft_hash"]
    assert "Noticed you don't have a business website" in m["body_text"]


def test_apply_rejects_a_fabrication_and_leaves_target_enrolled(tmp_path):
    rid = _seed(tmp_path, business={"website": None}, tier="A")
    _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "gainlev_leadgen",
         "--run", rid, "--tier", "A"], tmp_path)
    lines = _ndjson(tmp_path / "drafts_packets.ndjson")
    target_id = lines[1]["target"]
    (tmp_path / "drafts.ndjson").write_text(json.dumps({
        "target": target_id, "subject": "Quick note",
        "observation": "We've already helped 47 garages just like yours grow this year.",
        "used_fact": "no_website",
    }) + "\n", encoding="utf-8")

    res = _run(["--json", "draft", "apply", "--in", "drafts.ndjson"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is True  # apply itself succeeds; the INDIVIDUAL draft is what gets rejected
    assert d["counts"]["rejected"] == 1 and d["counts"]["applied"] == 0

    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    t = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (target_id,)).fetchone()
    assert t["state"] == "enrolled"  # unchanged — free to redo
    m = conn.execute("SELECT * FROM messages WHERE target_id=?", (target_id,)).fetchone()
    assert m["state"] == "rejected"
    reasons = json.loads(m["gate_json"])["reasons"]
    assert any(r.startswith("NUMBER") for r in reasons)


def test_apply_abstain_counts_insufficient_evidence_and_leaves_target_enrolled(tmp_path):
    rid = _seed(tmp_path, business={"website": None}, tier="A")
    _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "gainlev_leadgen",
         "--run", rid, "--tier", "A"], tmp_path)
    lines = _ndjson(tmp_path / "drafts_packets.ndjson")
    target_id = lines[1]["target"]
    (tmp_path / "drafts.ndjson").write_text(json.dumps({"target": target_id, "abstain": True}) + "\n",
                                            encoding="utf-8")

    res = _run(["--json", "draft", "apply", "--in", "drafts.ndjson"], tmp_path)
    d = _digest(res.stdout)
    assert d["counts"]["insufficient_evidence"] == 1
    assert d["counts"]["applied"] == 0 and d["counts"]["rejected"] == 0

    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    t = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (target_id,)).fetchone()
    assert t["state"] == "enrolled"
    assert conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 0


def test_apply_without_in_or_packets_file_fails_closed(tmp_path):
    _seed(tmp_path, tier="A")
    res = _run(["--json", "draft", "apply", "--in", "nope.ndjson"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False


# ------------------------------------------------------------------------- render
def test_render_writes_to_subject_preamble_and_body(tmp_path):
    rid = _seed(tmp_path, business={"website": None,
                                    "enrich": {"crawled_at": "2026-01-01T00:00:00Z", "pages": 1}},
               contact={"business_id": "b1", "kind": "email", "value": "info@acmegarage.example",
                       "tier": "valid", "affinity": "own_domain"}, tier="A")
    _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "gainlev_leadgen",
         "--run", rid, "--tier", "A"], tmp_path)
    lines = _ndjson(tmp_path / "drafts_packets.ndjson")
    target_id = lines[1]["target"]
    (tmp_path / "drafts.ndjson").write_text(json.dumps({
        "target": target_id, "subject": "Quick note about Acme Garage",
        "observation": "Noticed you don't have a business website live yet.",
        "used_fact": "no_website",
    }) + "\n", encoding="utf-8")
    _run(["--json", "draft", "apply", "--in", "drafts.ndjson"], tmp_path)

    res = _run(["--json", "draft", "render", "--campaign", "test-camp", "--out", "drafts_out"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is True and d["counts"]["rendered"] == 1
    files = list((tmp_path / "drafts_out").glob("*.txt"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("To: info@acmegarage.example\nSubject: Quick note about Acme Garage\n\n")
    assert "Noticed you don't have a business website live yet." in text


def test_render_never_addresses_an_unlinked_freemail_contact(tmp_path):
    """A freemail box classified `freemail_unlinked` (real archetype from the live campaign DB: a
    font-license credit's gmail address, sharing no token with the business name — C1's
    classify_email_affinity would call this exactly, once it runs on the row) must never become a
    message's To: — _best_contact only picks an own-domain or affinity-LINKED-freemail address,
    never a merely best-RANKED one (rank_email_contacts still surfaces it for export.py's own display
    column, which is right for a human deciding, wrong for something about to compose a send).
    Stored (not blank) affinity here matters: a blank one is a legacy pre-v0.3 row and gets the
    coarser fallback_email_affinity guess instead (score.py) — see the DB-round-trip variant below."""
    rid = _seed(tmp_path, business={"website": None,
                                    "enrich": {"crawled_at": "2026-01-01T00:00:00Z", "pages": 1}},
               contact={"business_id": "b1", "kind": "email", "value": "unrelated.person@gmail.com",
                       "tier": "valid", "affinity": "freemail_unlinked"},
               tier="A")
    _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "gainlev_leadgen",
         "--run", rid, "--tier", "A"], tmp_path)
    lines = _ndjson(tmp_path / "drafts_packets.ndjson")
    target_id = lines[1]["target"]
    (tmp_path / "drafts.ndjson").write_text(json.dumps({
        "target": target_id, "subject": "Quick note about Acme Garage",
        "observation": "Noticed you don't have a business website live yet.",
        "used_fact": "no_website",
    }) + "\n", encoding="utf-8")
    _run(["--json", "draft", "apply", "--in", "drafts.ndjson"], tmp_path)

    _run(["--json", "draft", "render", "--campaign", "test-camp", "--out", "drafts_out"], tmp_path)
    text = next((tmp_path / "drafts_out").glob("*.txt")).read_text(encoding="utf-8")
    assert "unrelated.person@gmail.com" not in text
    assert text.startswith("To: Acme Garage\n")


# ------------------------------------------------------------------------- check
def test_check_without_in_names_the_missing_option(tmp_path):
    res = _run(["--json", "draft", "check"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False
    assert "--in" in " ".join(d["warnings"])


def test_check_reports_counts_without_storing_anything(tmp_path):
    rid = _seed(tmp_path, business={"website": None}, tier="A")
    _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "gainlev_leadgen",
         "--run", rid, "--tier", "A"], tmp_path)
    lines = _ndjson(tmp_path / "drafts_packets.ndjson")
    target_id = lines[1]["target"]
    (tmp_path / "drafts.ndjson").write_text(json.dumps({
        "target": target_id, "subject": "Quick note about Acme Garage",
        "observation": "Noticed you don't have a business website live yet.",
        "used_fact": "no_website",
    }) + "\n", encoding="utf-8")

    res = _run(["--json", "draft", "check", "--in", "drafts.ndjson"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is True
    assert d["counts"] == {"checked": 1, "ok": 1, "failed": 0}

    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 0
    t = conn.execute("SELECT state FROM outreach_targets WHERE id=?", (target_id,)).fetchone()
    assert t["state"] == "enrolled"


# ------------------------------------------------------------------------- three worked examples
def _worked(tmp_path, name, *, business, person=None, contact=None, draft_subject, draft_observation, used_fact):
    rid = _seed(tmp_path, business=business, person=person, contact=contact, tier="A",
               identity={"label": "gainlev-main", "from_name": "GainLev",
                        "postal_address": "1 Example Street, Leeds, LS1 1AA",
                        "privacy_url": "https://gainlev.example/privacy"})
    _run(["--json", "draft", "export", "--campaign", "test-camp", "--purpose", "gainlev_leadgen",
         "--run", rid, "--tier", "A"], tmp_path)
    lines = _ndjson(tmp_path / "drafts_packets.ndjson")
    target_id = lines[1]["target"]
    (tmp_path / "drafts.ndjson").write_text(json.dumps({
        "target": target_id, "subject": draft_subject, "observation": draft_observation, "used_fact": used_fact,
    }) + "\n", encoding="utf-8")
    apply_res = _run(["--json", "draft", "apply", "--in", "drafts.ndjson"], tmp_path)
    render_res = _run(["--json", "draft", "render", "--campaign", "test-camp", "--out", "drafts_out"], tmp_path)
    return apply_res, render_res, target_id


def test_worked_example_registry_matched_ltd_with_own_domain_email(tmp_path):
    """Archetype 1: a registry-matched active Ltd, corroborated director name, own-domain email —
    grade A, personal greeting."""
    enrich = {"registry_profile": {"legal_name": "Acme Garage Ltd", "incorporated": "2015-05-01",
                                   "company_status": "active", "match_similarity": 0.9}}
    apply_res, render_res, target_id = _worked(
        tmp_path, "registry_ltd",
        business={"domain": "acmegarage.co.uk", "website": "https://acmegarage.co.uk", "enrich": enrich},
        person={"business_id": "b1", "name": "Smith, Sarah", "title": "Director", "is_dm": 1,
               "dm_confidence": 0.9, "labeled_by": "registry", "origin": "registry"},
        contact={"business_id": "b1", "kind": "email", "value": "sarah@acmegarage.co.uk",
                "tier": "valid", "affinity": "own_domain"},
        draft_subject="Quick note about Acme Garage Ltd",
        draft_observation="Saw Acme Garage Ltd has been trading since 2015 - impressive run.",
        used_fact="incorporated_year",
    )
    assert _digest(apply_res.stdout)["counts"]["applied"] == 1, apply_res.stdout
    assert _digest(render_res.stdout)["counts"]["rendered"] == 1
    got = (tmp_path / "drafts_out" / f"{target_id}_1.txt").read_text(encoding="utf-8")
    expected = (FIXTURES / "expected_registry_ltd.txt").read_text(encoding="utf-8")
    assert got == expected


def test_worked_example_no_website_phone_only_garage(tmp_path):
    """Archetype 2: no website at all (segment fact only) — grade C, company-level greeting, still a
    legitimate send when the one segment fact genuinely supports the observation."""
    apply_res, render_res, target_id = _worked(
        tmp_path, "no_website",
        business={"website": None, "phone_e164": "+441132345678"},
        draft_subject="Quick note about Acme Garage",
        draft_observation="Noticed you don't have a business website live yet.",
        used_fact="no_website",
    )
    assert _digest(apply_res.stdout)["counts"]["applied"] == 1, apply_res.stdout
    assert _digest(render_res.stdout)["counts"]["rendered"] == 1
    got = (tmp_path / "drafts_out" / f"{target_id}_1.txt").read_text(encoding="utf-8")
    expected = (FIXTURES / "expected_no_website.txt").read_text(encoding="utf-8")
    assert got == expected


def test_worked_example_stale_site_garage(tmp_path):
    """Archetype 3: a real crawl with a stale copyright year, no registry match, no DM — grade B,
    company-level greeting, one distinctive fact."""
    enrich = {"crawled_at": "2026-01-01T00:00:00Z", "pages": 2,
             "signals": {"stale_site": True, "copyright_year": 2018}}
    apply_res, render_res, target_id = _worked(
        tmp_path, "stale_site",
        business={"website": "https://acmegarage.example", "enrich": enrich},
        draft_subject="Quick note about Acme Garage",
        draft_observation="Noticed your site footer still says 2018 - happy to refresh it.",
        used_fact="site_stale",
    )
    assert _digest(apply_res.stdout)["counts"]["applied"] == 1, apply_res.stdout
    assert _digest(render_res.stdout)["counts"]["rendered"] == 1
    got = (tmp_path / "drafts_out" / f"{target_id}_1.txt").read_text(encoding="utf-8")
    expected = (FIXTURES / "expected_stale_site.txt").read_text(encoding="utf-8")
    assert got == expected
