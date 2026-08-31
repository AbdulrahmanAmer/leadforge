"""Digest-contract test (U8.1): key commands emit exactly one valid LF_DIGEST line."""
import json
import subprocess
import sys


def _run(args, cwd):
    return subprocess.run([sys.executable, "-m", "leadforge", *args], cwd=cwd,
                          capture_output=True, encoding="utf-8")


def _digest(output: str) -> dict:
    lines = [ln for ln in output.splitlines() if ln.startswith("LF_DIGEST ")]
    assert len(lines) == 1, f"expected exactly one digest line, got {len(lines)}"
    return json.loads(lines[0][len("LF_DIGEST "):])


def test_status_digest_on_empty(tmp_path):
    d = _digest(_run(["--json", "status"], tmp_path).stdout)
    assert d["cmd"] == "status" and d["ok"] is True and "counts" in d


def test_intake_error_digest(tmp_path):
    (tmp_path / "answers.yaml").write_text("campaign: x\n", encoding="utf-8")  # missing required fields
    res = _run(["--json", "intake", "--answers", "answers.yaml"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False and d["cmd"] == "intake" and d["counts"].get("errors", 0) >= 1
    # digest must be self-sufficient: the actual problems, not just a header
    assert any("offer" in w or "categories" in w or "geography" in w for w in d["warnings"])


def test_intake_rejects_missing_country(tmp_path):
    """A campaign without a country would geocode the wrong place — intake must refuse it."""
    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n  geography:\n    areas: ['Houston, TX']\n",
        encoding="utf-8",
    )
    res = _run(["--json", "intake", "--answers", "answers.yaml"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False
    assert "country" in " ".join(d["warnings"]).lower()


def test_intake_warns_on_missing_state(tmp_path):
    """US city with no state is the classic same-name trap — compile, but warn."""
    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n"
        "  geography:\n    country: US\n    areas: [Springfield]\n",
        encoding="utf-8",
    )
    d = _digest(_run(["--json", "intake", "--answers", "answers.yaml"], tmp_path).stdout)
    assert d["ok"] is True
    assert any("state/region" in w for w in d["warnings"])


def test_intake_success_digest(tmp_path):
    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n"
        "  geography:\n    country: US\n    areas: ['Houston, TX']\n",
        encoding="utf-8",
    )
    res = _run(["--json", "intake", "--answers", "answers.yaml"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is True and d["counts"]["errors"] == 0
    assert (tmp_path / "icp.yaml").exists()


def test_plan_digest(tmp_path):
    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n"
        "  geography:\n    country: US\n    areas: ['Houston, TX']\n",
        encoding="utf-8",
    )
    _run(["--json", "intake", "--answers", "answers.yaml"], tmp_path)
    d = _digest(_run(["--json", "plan", "--icp", "icp.yaml"], tmp_path).stdout)
    assert d["cmd"] == "plan" and d["counts"]["queries"] >= 1


# --- U8.1: every command emits exactly one valid LF_DIGEST line (in-process, offline) --------
import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from leadforge.cli import app  # noqa: E402
from leadforge.models import RawListing  # noqa: E402
from leadforge.util import now_iso  # noqa: E402

REQUIRED_KEYS = {"ok", "cmd", "run", "counts", "warnings", "artifacts", "next"}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.available", lambda self: (True, "mock"))
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.fetch", lambda self, q, limit=None: [
        RawListing(provider="gosom", fetched_at=now_iso(), data={
            "title": "Gamma Garage", "address": "3 C St, Houston, TX 77003", "phone": "713-555-0300",
            "web_site": "http://gamma-garage.test", "review_rating": "4.1", "review_count": "15",
            "place_id": "PID_GAMMA"})])
    from leadforge.enrich.crawler import CrawlResult, Page, SiteCrawler
    monkeypatch.setattr(SiteCrawler, "crawl", lambda self, website: CrawlResult(
        ok=True, pages=[Page(website, "<html><body>Owner Gail Gamma. "
                                      "<a href='mailto:gail@gamma-garage.test'>mail</a></body></html>",
                             "Owner Gail Gamma. gail@gamma-garage.test")],
        signals={"https": True}))
    monkeypatch.setattr("leadforge.enrich.runner.validate_email", lambda e, lab, cfg: ("valid", {}))
    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n"
        "  geography:\n    country: US\n    areas: ['Houston, TX']\n", encoding="utf-8")
    return CliRunner()


def _invoke_digest(runner, args):
    res = runner.invoke(app, ["--json", *args])
    assert res.exit_code == 0, f"{args} exited {res.exit_code}: {res.output[-500:]}"
    lines = [ln for ln in res.output.splitlines() if ln.startswith("LF_DIGEST ")]
    assert len(lines) == 1, f"{args}: expected exactly one digest line, got {len(lines)}"
    d = json.loads(lines[0][len("LF_DIGEST "):])
    assert REQUIRED_KEYS <= set(d), f"{args}: digest missing {REQUIRED_KEYS - set(d)}"
    return d


def test_every_command_digest_contract(offline):
    runner = offline
    assert _invoke_digest(runner, ["intake", "--answers", "answers.yaml"])["cmd"] == "intake"
    assert _invoke_digest(runner, ["plan", "--icp", "icp.yaml"])["cmd"] == "plan"
    assert _invoke_digest(runner, ["discover", "--icp", "icp.yaml", "--limit", "5"])["cmd"] == "discover"
    assert _invoke_digest(runner, ["enrich", "--limit", "5"])["cmd"] == "enrich"
    assert _invoke_digest(runner, ["dm", "export", "--max", "5"])["cmd"] == "dm export"
    d = _invoke_digest(runner, ["score", "--icp", "icp.yaml"])
    assert d["cmd"] == "score"
    assert _invoke_digest(runner, ["export", "--icp", "icp.yaml"])["cmd"] == "export"
    assert _invoke_digest(runner, ["status"])["cmd"] == "status"
    assert _invoke_digest(runner, ["suppress", "add", "domain:spam.test"])["cmd"] == "suppress"
    assert _invoke_digest(runner, ["suppress", "list"])["cmd"] == "suppress"


def test_run_command_digest_contract(offline):
    runner = offline
    _invoke_digest(runner, ["intake", "--answers", "answers.yaml"])
    d = _invoke_digest(runner, ["run", "--icp", "icp.yaml", "--limit", "5"])
    assert d["cmd"] == "run" and d["run"]


def test_dm_apply_digest_contract(offline, tmp_path):
    runner = offline
    _invoke_digest(runner, ["intake", "--answers", "answers.yaml"])
    _invoke_digest(runner, ["run", "--icp", "icp.yaml", "--limit", "5"])
    from leadforge import db
    from leadforge.config import load_config
    conn = db.connect(load_config(tmp_path).db_path)
    pending = db.dm_pending(conn, 5)
    conn.close()
    if pending:
        (tmp_path / "dm_labels.ndjson").write_text(
            json.dumps({"biz": pending[0]["id"], "pick": 0, "confidence": 0.9}) + "\n", encoding="utf-8")
        d = _invoke_digest(runner, ["dm", "apply", "--in", "dm_labels.ndjson"])
        assert d["cmd"] == "dm apply"


def test_config_set_get_roundtrip(offline):
    runner = offline
    d = _invoke_digest(runner, ["config", "set", "registry.companies_house_key", "test-key-123"])
    assert d["ok"] is True
    from leadforge.config import load_config
    assert load_config(".").registry.companies_house_key == "test-key-123"
    d2 = _invoke_digest(runner, ["config", "get", "registry.companies_house_key"])
    assert d2["ok"] is True


def test_intake_warns_uk_without_registry_key(offline):
    runner = offline
    d = _invoke_digest(runner, ["intake", "--answers", "answers.yaml"])
    # answers.yaml in this fixture is US — no UK warning
    assert not any("Companies House" in w for w in d["warnings"])
    import pathlib
    pathlib.Path("answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [accounting firm]\n"
        "  geography:\n    country: GB\n    areas: [Guildford]\n", encoding="utf-8")
    d2 = _invoke_digest(runner, ["intake", "--answers", "answers.yaml"])
    assert any("Companies House" in w for w in d2["warnings"])


def test_version_digest_contract(offline):
    d = _invoke_digest(offline, ["version"])
    assert d["cmd"] == "version" and d["ok"] is True


def test_doctor_digest_contract(offline, monkeypatch):
    # no --fix: checks may fail (no binary in tmp workspace) but the digest contract must hold
    res = offline.invoke(app, ["--json", "doctor"])
    lines = [ln for ln in res.output.splitlines() if ln.startswith("LF_DIGEST ")]
    assert len(lines) == 1
    d = json.loads(lines[0][len("LF_DIGEST "):])
    assert REQUIRED_KEYS <= set(d) and d["cmd"] == "doctor"
    assert set(d["counts"]) >= {"checks", "fixed", "failed"}
