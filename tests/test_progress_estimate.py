"""v0.3.1: the progress feed carries its own clock and the ETA is computed from it — two watchers
attached at different times used to disagree by hours (2h46m vs 11h45m on the same run, 2026-09-03)."""

from __future__ import annotations

import json

from leadforge import util
from leadforge.util import progress_estimate, progress_summary


def _hist(n: int, step_s: float = 120.0, total: int = 100, start: float = 1_000.0):
    return [(start + i * step_s, i, total) for i in range(n)]


def test_rate_and_eta_come_from_feed_timestamps_not_uptime():
    hist = _hist(5)                       # 4 items in 480 s -> 120 s per item
    est = progress_estimate(hist, now=hist[-1][0])
    assert est["done"] == 4 and est["total"] == 100
    assert abs(est["per_item_s"] - 120.0) < 1e-6
    assert abs(est["eta_s"] - 96 * 120.0) < 1e-6
    assert est["elapsed_s"] == 480.0


def test_two_watchers_replaying_the_same_feed_agree():
    hist = _hist(9)
    a = progress_estimate(hist, now=hist[-1][0])
    b = progress_estimate(list(hist), now=hist[-1][0] + 3.0)  # attached 3 s later: same feed, same pace
    assert a["eta_s"] == b["eta_s"] and a["per_item_s"] == b["per_item_s"]


def test_growth_of_total_is_reported_and_raises_the_eta():
    flat = _hist(5, total=100)
    grown = [(t, d, 100 + 40 * (i > 2)) for i, (t, d, _) in enumerate(flat)]  # tiles split at item 3
    est = progress_estimate(grown, now=grown[-1][0])
    assert est["growth"] == 40 and est["total"] == 140
    assert est["eta_s"] > progress_estimate(flat, now=flat[-1][0])["eta_s"]
    assert "+40" in progress_summary("discover", grown, grown[-1][0])


def test_moving_window_reacts_to_a_stall():
    fast = _hist(13, step_s=60.0)                          # 60 s per item for 12 items
    stalled = fast + [(fast[-1][0] + 600.0, 13, 100)]      # then one 10-minute item
    est = progress_estimate(stalled, now=stalled[-1][0])
    assert est["per_item_s"] > 60.0                       # the window includes the stall
    assert est["per_item_s"] < 600.0                      # but is not dominated by it


def test_no_estimate_before_two_completions():
    est = progress_estimate(_hist(1), now=1_000.0)
    assert est["eta_s"] is None and est["per_item_s"] is None
    assert progress_estimate([], now=0.0)["eta_s"] is None


def test_feed_lines_carry_a_timestamp(tmp_path, monkeypatch, capsys):
    feed = tmp_path / "progress.jsonl"
    util.set_progress_file(feed)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)
    util.emit_progress("discover", 3, 10, "tile 3")
    line = json.loads(feed.read_text(encoding="utf-8").splitlines()[-1])
    assert line["done"] == 3 and isinstance(line["ts"], float) and line["ts"] > 1_700_000_000
    util.set_progress_file(None) if False else None  # keep other tests independent (file is per tmp_path)
