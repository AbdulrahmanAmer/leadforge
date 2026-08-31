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
