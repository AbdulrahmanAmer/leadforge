"""U9E CLI wiring — every `leadforge outreach ...` command ends with exactly one LF_DIGEST line, and
the orchestrator's gate probe (`outreach send --live --i-am gate --json` in an empty workspace) must
answer ok:false without a stack trace, per docs/09-v0.3-build-plan.md's Unit E acceptance line."""

from __future__ import annotations

import json
import subprocess
import sys


def _run(args, cwd):
    return subprocess.run([sys.executable, "-m", "leadforge", *args], cwd=cwd,
                          capture_output=True, encoding="utf-8")


def _digest(output: str) -> dict:
    lines = [ln for ln in output.splitlines() if ln.startswith("LF_DIGEST ")]
    assert len(lines) == 1, f"expected exactly one digest line, got {len(lines)}: {output!r}"
    return json.loads(lines[0][len("LF_DIGEST "):])


def test_gate_probe_live_send_in_empty_workspace_fails_closed(tmp_path):
    """The exact command the orchestrator's gate script runs."""
    res = _run(["outreach", "send", "--live", "--i-am", "gate", "--json"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False
    assert d["cmd"] == "outreach send"


def test_dry_run_send_on_empty_workspace_is_ok_with_zero_candidates(tmp_path):
    res = _run(["outreach", "send", "--campaign", "nope", "--json"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is True
    assert d["counts"]["would_send"] == 0


def test_identity_add_then_list_digest(tmp_path):
    res1 = _run(["outreach", "identity", "add", "--label", "cli-ident", "--from-email", "a@b.com", "--json"], tmp_path)
    d1 = _digest(res1.stdout)
    assert d1["ok"] is True and d1["cmd"] == "outreach identity add"

    res2 = _run(["outreach", "identity", "list", "--json"], tmp_path)
    d2 = _digest(res2.stdout)
    assert d2["ok"] is True and d2["counts"]["identities"] == 1


def test_identity_add_missing_from_email_fails_with_digest(tmp_path):
    res = _run(["outreach", "identity", "add", "--label", "bad", "--from-email", "", "--json"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False


def test_mailbox_add_rejects_secret_looking_config_key(tmp_path):
    _run(["outreach", "identity", "add", "--label", "mid1", "--from-email", "a@b.com", "--json"], tmp_path)
    res = _run(["outreach", "mailbox", "add", "--identity", "mid1", "--address", "a@b.com",
               "--config", "literal_password=hunter2", "--json"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False


def test_plan_no_run_fails_with_digest(tmp_path):
    (tmp_path / "icp.yaml").write_text(
        "campaign: t\noffer:\n  what: x\n  value_prop: y\n"
        "target:\n  categories: [auto repair]\n  geography:\n    country: GB\n    areas: [Leeds]\n",
        encoding="utf-8",
    )
    res = _run(["outreach", "plan", "--campaign", "c1", "--tier", "A", "--identity", "ghost", "--json"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False
    assert "no run found" in " ".join(d["warnings"]).lower()


def test_doctor_unknown_identity_fails_with_digest(tmp_path):
    res = _run(["outreach", "doctor", "--identity", "ghost", "--json"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False


def test_status_and_sync_on_empty_workspace_are_ok(tmp_path):
    d1 = _digest(_run(["outreach", "status", "--json"], tmp_path).stdout)
    assert d1["ok"] is True
    d2 = _digest(_run(["outreach", "sync", "--json"], tmp_path).stdout)
    assert d2["ok"] is True


def test_approve_requires_selector_digest(tmp_path):
    res = _run(["outreach", "approve", "--campaign", "c1", "--approver", "alice", "--json"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False


def test_outcome_add_unknown_business_fails_with_digest(tmp_path):
    res = _run(["outreach", "outcome", "add", "--business", "ghost", "--channel", "phone",
               "--result", "interested", "--json"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False


# ---------------------------------------------------------------------------------------- watched-fail
#   test_gate_probe_live_send_in_empty_workspace_fails_closed: the `if not dry_run: if not
#     cfg.outreach.armed:` guard in outreach/cli.py's send() temporarily removed -> the process instead
#     crashed with an unhandled exception reaching live_send() with no campaign (or printed zero digest
#     lines) -> the single-digest-line assertion failed -> red for the right reason. Restored.
#   test_mailbox_add_rejects_secret_looking_config_key: identity.MAILBOX_CONFIG_KEYS temporarily
#     included "literal_password" -> d["ok"] came back True -> red for the right reason. Restored.
