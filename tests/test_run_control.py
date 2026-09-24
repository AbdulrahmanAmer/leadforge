"""Layer 1 (run control) tests: `run --detach`, `status --run <id> --json`, `run --resume --from-stage`,
and the single `mark_exported` code path. Offline throughout — no scraping, no `claude` CLI, no long-lived
background process left behind (the detach tests spawn a tiny fake command, never `leadforge run`)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from leadforge import db
from leadforge.config import load_config
from leadforge.util import emit_progress, now_iso, pid_alive, set_progress_file


def _run(args, cwd):
    return subprocess.run([sys.executable, "-m", "leadforge", *args], cwd=cwd,
                          capture_output=True, encoding="utf-8")


def _digest(output: str) -> dict:
    lines = [ln for ln in output.splitlines() if ln.startswith("LF_DIGEST ")]
    assert len(lines) == 1, f"expected exactly one digest line, got {len(lines)}: {output[-500:]}"
    return json.loads(lines[0][len("LF_DIGEST "):])


# --------------------------------------------------------------------------- db helpers
def test_set_run_meta_merges_without_touching_stage(tmp_path):
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, "icp.yaml", "hash1")
    db.set_stage(conn, run_id, "discovering", businesses=3)
    db.set_run_meta(conn, run_id, pid=4242, log_path="somewhere.log", detached=True)
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row["stage"] == "discovering"  # set_run_meta must never change stage
    stats = json.loads(row["stats_json"])
    assert stats["pid"] == 4242 and stats["detached"] is True and stats["businesses"] == 3


def test_mark_exported_sets_stage_exported_and_finished(tmp_path):
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, "icp.yaml", "hash1")
    db.mark_exported(conn, run_id, leads=7)
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row["stage"] == "exported"
    assert row["finished_at"]  # set_stage marks 'exported' as done -> stamps finished_at
    assert json.loads(row["stats_json"])["leads"] == 7


def test_cli_export_and_pipeline_both_land_on_mark_exported():
    """The two former call sites (cli.py export command, pipeline.py run orchestrator) must both go
    through db.mark_exported now — a grep-level proof that the consolidation actually happened, not
    just that behavior looks right (a patch that never applied would still look right)."""
    from leadforge import cli, pipeline

    cli_src = open(cli.__file__, encoding="utf-8").read()
    pipeline_src = open(pipeline.__file__, encoding="utf-8").read()
    assert 'db.mark_exported(conn, run["id"]' in cli_src
    assert "db.mark_exported(conn, run_id" in pipeline_src
    assert 'set_stage(conn, run["id"], "exported"' not in cli_src
    assert 'set_stage(conn, run_id, "exported"' not in pipeline_src


# --------------------------------------------------------------------------- pid_alive
def test_pid_alive_true_for_self():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_false_for_none_or_missing():
    assert pid_alive(None) is False
    # a pid essentially guaranteed not to be a live process on any normal machine
    assert pid_alive(999_999) is False


# --------------------------------------------------------------------------- ensure_run (detach's run-id source)
def test_ensure_run_creates_a_planned_run_with_its_query_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n  geography:\n    country: US\n    areas: ['Houston, TX']\n",
        encoding="utf-8",
    )
    res = _run(["--json", "intake", "--answers", "answers.yaml"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"], d

    from leadforge.intake import load_icp
    from leadforge.pipeline import ensure_run

    cfg = load_config(tmp_path)
    icp = load_icp(tmp_path / "icp.yaml")
    run_id = ensure_run(cfg, icp, tmp_path / "icp.yaml")
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row is not None and row["stage"] == "planned"
    assert conn.execute("SELECT COUNT(*) c FROM queries WHERE run_id=?", (run_id,)).fetchone()["c"] > 0

    # calling it again for the SAME icp must reuse the run, not create a second one
    again = ensure_run(cfg, icp, tmp_path / "icp.yaml")
    assert again == run_id


# --------------------------------------------------------------------------- --detach spawn mechanics
def test_spawn_detached_uses_platform_detach_flags_and_writes_the_log(tmp_path):
    from leadforge.cli import _spawn_detached

    log_path = tmp_path / "fake.log"
    marker = "hello-from-detached-child"
    cmd = [sys.executable, "-c", f"print({marker!r})"]
    pid = _spawn_detached(cmd, log_path, tmp_path)
    assert isinstance(pid, int) and pid > 0
    # the child is short-lived (just prints and exits); give it a moment, then check the log
    deadline = time.time() + 10
    while time.time() < deadline and (not log_path.is_file() or not log_path.read_text(encoding="utf-8").strip()):
        time.sleep(0.1)
    assert marker in log_path.read_text(encoding="utf-8")


def test_spawn_detached_passes_detach_kwargs(tmp_path, monkeypatch):
    """Prove the exact mechanism the task requires, without waiting on a real child process."""
    from leadforge import cli

    captured = {}

    class _FakeProc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        captured["cmd"] = cmd
        return _FakeProc()

    import subprocess as _subprocess
    monkeypatch.setattr(_subprocess, "Popen", fake_popen)
    pid = cli._spawn_detached([sys.executable, "-c", "pass"], tmp_path / "x.log", tmp_path)
    assert pid == 4321
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        assert captured["creationflags"] == DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["start_new_session"] is True


# --------------------------------------------------------------------------- status --run <id> --json
def test_status_reports_pid_alive_and_progress_eta(tmp_path):
    cfg = load_config(tmp_path)
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, "icp.yaml", "hash1")
    db.set_stage(conn, run_id, "enriching")
    db.set_run_meta(conn, run_id, pid=os.getpid())

    set_progress_file(cfg.data_path / "progress.jsonl")
    for i in range(1, 4):
        emit_progress("enrich", i, 10, f"site {i}")
        time.sleep(0.05)

    res = _run(["--json", "status", "--run", run_id], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] and d["run"] == run_id
    assert d["counts"]["stage"] == "enriching"
    assert d["counts"]["pid"] == os.getpid()
    assert d["counts"]["alive"] is True
    assert d["counts"]["progress_stage"] == "enrich"
    assert d["counts"]["progress_done"] == 3
    assert d["counts"]["progress_total"] == 10


def test_status_without_run_flag_keeps_prior_behavior(tmp_path):
    d = _digest(_run(["--json", "status"], tmp_path).stdout)
    assert d["cmd"] == "status" and d["ok"] is True and "counts" in d


# --------------------------------------------------------------------------- --resume --from-stage
def test_run_from_stage_rejects_unknown_stage(tmp_path):
    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n  geography:\n    country: US\n    areas: ['Houston, TX']\n",
        encoding="utf-8",
    )
    _digest(_run(["--json", "intake", "--answers", "answers.yaml"], tmp_path).stdout)
    res = _run(["--json", "run", "--icp", "icp.yaml", "--resume", "--from-stage", "bogus_stage"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False and d["cmd"] == "run"
    assert res.returncode == 2
    assert any("bogus_stage" in w for w in d["warnings"])


def test_run_from_stage_errors_when_no_run_exists(tmp_path):
    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n  geography:\n    country: US\n    areas: ['Houston, TX']\n",
        encoding="utf-8",
    )
    _digest(_run(["--json", "intake", "--answers", "answers.yaml"], tmp_path).stdout)
    res = _run(["--json", "run", "--icp", "icp.yaml", "--resume", "--from-stage", "enriching"], tmp_path)
    d = _digest(res.stdout)
    assert d["ok"] is False
    assert "no resumable run" in " ".join(d["warnings"]).lower()


def test_run_from_stage_forces_stage_then_resumes(tmp_path, monkeypatch):
    """The exact recovery this replaces: `update runs set stage='enriching'` then `run --resume`. Runs
    in-process (CliRunner, like tests/test_cli_contract.py's `offline` fixture) so ensure_ready and the
    providers can be mocked — a subprocess-based invocation cannot be monkeypatched from here and would
    otherwise hit the real doctor/network path."""
    import json as _json

    from typer.testing import CliRunner

    from leadforge.cli import app
    from leadforge.models import RawListing

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda cfg: None)
    monkeypatch.setattr("leadforge.providers.gosom.GosomProvider.available", lambda self: (True, "mock"))
    monkeypatch.setattr(
        "leadforge.providers.gosom.GosomProvider.fetch",
        lambda self, q, limit=None: [
            RawListing(provider="gosom", fetched_at=now_iso(), data={
                "title": "Gamma Garage", "address": "3 C St, Houston, TX 77003", "phone": "713-555-0300",
                "web_site": "http://gamma-garage.test", "review_rating": "4.1", "review_count": "15",
                "place_id": "PID_GAMMA"})],
    )
    from leadforge.enrich.crawler import CrawlResult, SiteCrawler
    monkeypatch.setattr(SiteCrawler, "crawl", lambda self, website: CrawlResult(ok=False, error="unreachable"))

    runner = CliRunner()

    def invoke(args):
        res = runner.invoke(app, ["--json", *args])
        assert res.exit_code == 0, f"{args} exited {res.exit_code}: {res.output[-500:]}"
        lines = [ln for ln in res.output.splitlines() if ln.startswith("LF_DIGEST ")]
        return _json.loads(lines[0][len("LF_DIGEST "):])

    (tmp_path / "answers.yaml").write_text(
        "campaign: t\noffer:\n  what: Web redesign\n"
        "target:\n  categories: [auto repair shop]\n  geography:\n    country: US\n    areas: ['Houston, TX']\n",
        encoding="utf-8",
    )
    assert invoke(["intake", "--answers", "answers.yaml"])["ok"]

    from leadforge.config import load_config as _load_config
    from leadforge.intake import load_icp
    from leadforge.pipeline import ensure_run

    cfg = _load_config(tmp_path)
    icp = load_icp(tmp_path / "icp.yaml")
    run_id = ensure_run(cfg, icp, tmp_path / "icp.yaml")
    conn = db.connect(cfg.db_path)
    db.set_stage(conn, run_id, "exported")  # pretend a bad prior state, as the manual recipe fixes
    conn.close()

    d = invoke(["run", "--icp", "icp.yaml", "--resume", "--from-stage", "enriching"])
    assert d["ok"], d
    # forced into 'enriching' then carried forward by the normal state machine (no businesses to
    # discover — the pipeline moves straight through to whatever's next, never staying 'enriching')
    assert d["counts"]["stage"] != "enriching"


def test_detached_child_command_carries_every_run_option():
    """Regression: the detached child used to be spawned without --from-stage, so
    `run --resume --from-stage scored --detach` resumed the run at its old stage (found in an E2E run)."""
    from leadforge.cli import _detached_run_cmd

    cmd = _detached_run_cmd("py", "icp.yaml", limit=5, skip_dm=True, autopilot=False, from_stage="scored")
    assert cmd[:8] == ["py", "-m", "leadforge", "--json", "run", "--icp", "icp.yaml", "--resume"]
    assert cmd[cmd.index("--from-stage") + 1] == "scored"
    assert cmd[cmd.index("--limit") + 1] == "5"
    assert "--skip-dm" in cmd and "--no-autopilot" in cmd
    bare = _detached_run_cmd("py", "icp.yaml", limit=None, skip_dm=False, autopilot=None, from_stage=None)
    assert "--from-stage" not in bare and "--limit" not in bare
