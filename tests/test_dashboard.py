"""v0.3.1 `leadforge dashboard`: a read-only status page split into machine stages (measured pace + ETA)
and human/agent stages, computed from the DB and the feed without writing anything."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request

from leadforge import db
from leadforge.dashboard import build_status, serve
from leadforge.models import Business


def _seed(cfg, with_ts: bool) -> tuple[str, float]:
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, "icp.yaml", "hash")
    started = time.time() - 600.0  # run began 10 minutes ago
    conn.execute("UPDATE runs SET started_at=?, stage='discovering' WHERE id=?",
                 (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), run_id))
    db.add_queries(conn, run_id, [(f"q{i}", {"bbox": [0, 0, 1, 1], "cell_km": 3.0, "depth": 0}) for i in range(10)])
    for i in range(4):
        db.finish_query(conn, i + 1, "done", 110 if i < 3 else 20)
    for i in range(6):
        db.upsert_business(conn, Business(id=f"biz_{i}", name=f"B{i}", name_norm=f"b{i}", dedupe_key=f"na:{i}",
                                          domain=f"b{i}.example" if i < 4 else None, first_run_id=run_id,
                                          last_seen_at="2026-09-03T00:00:00Z", source="gosom"))
    conn.commit()
    feed = cfg.data_path / "progress.jsonl"
    lines = []
    for i in range(5):
        d = {"stage": "discover", "done": i, "total": 10 if i < 3 else 14, "msg": f"q{i}"}
        if with_ts:
            d["ts"] = started + i * 120.0
        lines.append(json.dumps(d))
    # the real feed ends with a line that REPEATS the latest count (the next query's text) — the case that
    # silently dropped the "as of now" pin on 2026-09-03
    tail = {"stage": "discover", "done": 4, "total": 14, "msg": "q5 (next)"}
    if with_ts:
        tail["ts"] = started + 4 * 120.0 + 0.3  # the next-query line follows the completion within a second
    lines.append(json.dumps(tail))
    feed.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_id, started


def test_status_splits_machine_and_human_and_measures_pace_from_feed_ts(cfg):
    run_id, started = _seed(cfg, with_ts=True)
    st = build_status(cfg.data_path, now=started + 480.0)
    assert st["run"] == run_id and st["stage"] == "discovering"
    names = [m["stage"] for m in st["machine"]["stages"]]
    assert names == ["discover", "enrich", "registry", "validate"]
    disc = st["machine"]["stages"][0]
    assert disc["state"] == "running" and disc["done"] == 4 and disc["total"] == 10
    assert disc["pace_source"] == "measured on this run" and abs(disc["pace_s"] - 120.0) < 1e-6
    assert disc["growth"] == 4 and abs(disc["eta_s"] - 6 * 120.0) < 1e-6
    enrich = st["machine"]["stages"][1]
    assert enrich["total"] == 4 and enrich["state"] == "pending" and enrich["pace_source"].startswith("documented")
    assert st["machine"]["eta_s"] > disc["eta_s"]
    assert [h["stage"] for h in st["human"]] == ["dm labeling", "score + export", "drafting", "outreach"]
    assert st["counts"]["queries_saturated"] == 3 and st["counts"]["new_this_run"] == 6


def test_feed_without_timestamps_uses_run_start_as_the_clock(cfg):
    _run_id, started = _seed(cfg, with_ts=False)
    now = started + 480.0
    st = build_status(cfg.data_path, now=now)
    disc = st["machine"]["stages"][0]
    # 4 items between run start and the feed mtime (~now): cumulative average, never this process's uptime
    assert disc["pace_source"] == "measured on this run"
    assert 60.0 < disc["pace_s"] < 200.0


def test_status_is_read_only(cfg):
    _seed(cfg, with_ts=True)
    before = sqlite3.connect(cfg.db_path).execute("SELECT COUNT(*) FROM queries").fetchone()[0]
    mtime = cfg.db_path.stat().st_mtime
    build_status(cfg.data_path)
    assert sqlite3.connect(cfg.db_path).execute("SELECT COUNT(*) FROM queries").fetchone()[0] == before
    assert cfg.db_path.stat().st_mtime == mtime


def test_http_endpoints_serve_json_and_html(cfg):
    _seed(cfg, with_ts=True)
    srv = serve(cfg.data_path, port=0, block=False)
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
        assert body["machine"]["stages"][0]["stage"] == "discover"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            html = r.read().decode("utf-8")
        assert "LeadForge campaign status" in html and "/api/status" in html
    finally:
        srv.shutdown()
        srv.server_close()


def test_no_workspace_is_reported_not_crashed(tmp_path):
    st = build_status(tmp_path / "nowhere")
    assert "error" in st


def test_measured_pace_survives_the_feed_being_truncated_on_restart(cfg):
    """`set_progress_file` wipes the feed on every process start; a resumed run must not fall back to the
    documented default for a stage it already measured (2026-09-03: 26 h ETA from a 22 s/site default)."""
    run_id, started = _seed(cfg, with_ts=True)
    st = build_status(cfg.data_path, now=started + 480.0)
    assert st["machine"]["stages"][0]["pace_source"] == "measured on this run"
    assert json.loads((cfg.data_path / "pace.json").read_text())["discover"]["per_item_s"] == 120.0
    (cfg.data_path / "progress.jsonl").write_text("", encoding="utf-8")  # a restart
    st2 = build_status(cfg.data_path, now=started + 900.0)
    disc = st2["machine"]["stages"][0]
    assert disc["pace_source"] == "measured earlier in this workspace" and abs(disc["pace_s"] - 120.0) < 1e-6
    assert st2["machine"]["stages"][1]["pace_source"].startswith("documented")  # never measured -> still the default


def test_walk_away_time_is_discover_plus_the_longest_overlapped_stage(cfg):
    run_id, started = _seed(cfg, with_ts=True)
    st = build_status(cfg.data_path, now=started + 480.0)
    stages = st["machine"]["stages"]
    assert st["machine"]["overlapped"] is True
    expected = stages[0]["eta_s"] + max(m["eta_s"] for m in stages[1:])
    assert abs(st["machine"]["eta_s"] - expected) < 1e-6
    assert st["machine"]["eta_s"] < sum(m["eta_s"] for m in stages)  # the sum would overstate it
    (cfg.workspace / "leadforge.yaml").write_text("enrich:" + chr(10) + "  overlap_stages: false" + chr(10), encoding="utf-8")
    st2 = build_status(cfg.data_path, now=started + 480.0)
    assert st2["machine"]["overlapped"] is False
    assert abs(st2["machine"]["eta_s"] - sum(m["eta_s"] for m in st2["machine"]["stages"])) < 1e-6
