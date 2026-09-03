"""v0.4 autopilot DM labeling (ADR-015): `LABEL_INSTRUCTIONS`, `batch_lines`, `apply_label_records`,
`heuristic_labels`, `auto_label` in `leadforge.enrich.dm`. The headless runner itself is a plain
callable here — these tests never shell out to a real `claude`, matching how `pipeline.run_pipeline`
injects it (see `tests/test_pipeline_autopilot.py`)."""

from __future__ import annotations

import json

from leadforge import db
from leadforge.enrich import dm
from leadforge.models import Business, Person


def _seed_business(conn, biz_id: str, people: list[tuple[str, str, str]]) -> None:
    """people: list of (name, title, snippet)."""
    db.upsert_business(conn, Business(id=biz_id, name=biz_id, name_norm=biz_id.lower(),
                                      dedupe_key=f"dk:{biz_id}", source="gosom"))
    for name, title, snippet in people:
        db.add_person(conn, Person(business_id=biz_id, name=name, title=title, snippet=snippet))


def test_label_instructions_is_a_nonempty_prompt_with_the_reply_contract():
    assert isinstance(dm.LABEL_INSTRUCTIONS, str) and len(dm.LABEL_INSTRUCTIONS) > 100
    assert "pick" in dm.LABEL_INSTRUCTIONS and "confidence" in dm.LABEL_INSTRUCTIONS
    assert "No prose" in dm.LABEL_INSTRUCTIONS


def test_batch_lines_matches_export_batch_ndjson_shape(cfg, sample_icp):
    conn = db.connect(cfg.db_path)
    _seed_business(conn, "biz_a", [("Jane Smith", "Owner", "Jane Smith founded the shop.")])
    rows = db.dm_pending(conn, 10)
    lines = dm.batch_lines(conn, sample_icp, rows)
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["biz"] == "biz_a"
    assert rec["icp_titles"] == sample_icp.decision_maker.titles_priority
    assert rec["candidates"][0]["name"] == "Jane Smith"
    assert rec["candidates"][0]["title"] == "Owner"
    assert rec["candidates"][0]["origin"] == "heuristic"  # Person.labeled_by default, per db.add_person


def test_export_batch_uses_batch_lines(cfg, sample_icp, tmp_path):
    conn = db.connect(cfg.db_path)
    _seed_business(conn, "biz_a", [("Jane Smith", "Owner", "Jane Smith founded the shop.")])
    out = tmp_path / "batch.ndjson"
    n, remaining = dm.export_batch(conn, sample_icp, out, max_biz=10)
    assert n == 1 and remaining == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["biz"] == "biz_a"
    # export never labels — the business is still pending afterward
    assert len(db.dm_pending(conn, 10)) == 1


def test_apply_label_records_same_semantics_as_apply_labels(cfg):
    conn = db.connect(cfg.db_path)
    _seed_business(conn, "biz_a", [("Jane Smith", "Owner", "..."), ("Bob Jones", "Mechanic", "...")])
    # candidate indexes are defined by the same people_for() enumeration export_batch/batch_lines use —
    # SQLite does not promise insertion order for the ORDER BY tie, so look the index up rather than assume it
    candidates = [p for p in db.people_for(conn, "biz_a") if p["is_dm"] == 0]
    jane_i = next(i for i, p in enumerate(candidates) if p["name"] == "Jane Smith")
    out = dm.apply_label_records(conn, [{"biz": "biz_a", "pick": jane_i, "confidence": 0.9}])
    assert out == {"applied": 1, "rejected": 0, "skipped": 0}
    dm_row = next(p for p in db.people_for(conn, "biz_a") if p["is_dm"] == 1)
    assert dm_row["name"] == "Jane Smith" and dm_row["labeled_by"] == "agent"
    assert abs(dm_row["dm_confidence"] - 0.9) < 1e-9


def test_apply_label_records_reject_and_skip(cfg):
    conn = db.connect(cfg.db_path)
    _seed_business(conn, "biz_b", [("Tech One", "Mechanic", "...")])
    out = dm.apply_label_records(conn, [{"biz": "biz_b", "pick": -1}])
    assert out == {"applied": 0, "rejected": 1, "skipped": 0}
    rej = db.people_for(conn, "biz_b")[0]
    assert rej["is_dm"] == -1

    _seed_business(conn, "biz_c", [("X", "Y", "z")])
    out2 = dm.apply_label_records(conn, [{"biz": "biz_c"}])  # missing pick
    assert out2 == {"applied": 0, "rejected": 0, "skipped": 1}
    out3 = dm.apply_label_records(conn, [{"biz": "biz_c", "pick": 5}])  # out of range
    assert out3 == {"applied": 0, "rejected": 0, "skipped": 1}


def test_heuristic_labels_only_when_exactly_one_title_matches(cfg, sample_icp):
    conn = db.connect(cfg.db_path)
    # exactly one matching title ("Owner") among candidates -> labeled
    _seed_business(conn, "biz_one", [("Jane Smith", "Owner", "..."), ("Bob Jones", "Mechanic", "...")])
    # two matching titles -> genuinely ambiguous, left alone (never a reject)
    _seed_business(conn, "biz_two", [("A", "Owner", "..."), ("B", "General Manager", "...")])
    # zero matching titles -> left alone
    _seed_business(conn, "biz_zero", [("C", "Mechanic", "...")])

    out = dm.heuristic_labels(conn, sample_icp)
    assert out == {"labeled": 1}
    one = next(p for p in db.people_for(conn, "biz_one") if p["is_dm"] == 1)
    assert one["name"] == "Jane Smith" and one["labeled_by"] == "heuristic_auto"
    assert abs(one["dm_confidence"] - 0.55) < 1e-9
    assert all(p["is_dm"] == 0 for p in db.people_for(conn, "biz_two"))
    assert all(p["is_dm"] == 0 for p in db.people_for(conn, "biz_zero"))


def test_auto_label_with_stub_runner_picks_zero_for_every_line(cfg, sample_icp):
    conn = db.connect(cfg.db_path)
    _seed_business(conn, "biz_a", [("Jane Smith", "Owner", "...")])
    _seed_business(conn, "biz_b", [("Bob Jones", "General Manager", "...")])

    def runner(lines):
        return [{"biz": json.loads(ln)["biz"], "pick": 0, "confidence": 0.8} for ln in lines]

    out = dm.auto_label(conn, sample_icp, cfg, runner=runner)
    assert out["runner"] == "agent"
    assert out["labeled"] == 2
    assert out["unlabeled"] == 0
    assert out["batches"] == 1
    assert db.dm_pending(conn, 100) == []


def test_auto_label_runner_none_falls_back_to_heuristics(cfg, sample_icp):
    conn = db.connect(cfg.db_path)
    _seed_business(conn, "biz_a", [("Jane Smith", "Owner", "...")])  # heuristic will catch this
    _seed_business(conn, "biz_b", [("X", "Mechanic", "...")])        # heuristic can't -> stays pending

    out = dm.auto_label(conn, sample_icp, cfg, runner=None)
    assert out["runner"] == "none"
    assert out["batches"] == 0
    assert out["labeled"] == 1
    assert out["unlabeled"] == 1


def test_auto_label_runner_exception_stops_loop_and_falls_back_to_heuristics(cfg, sample_icp):
    conn = db.connect(cfg.db_path)
    _seed_business(conn, "biz_a", [("Jane Smith", "Owner", "...")])

    def boom(lines):
        raise RuntimeError("agent unavailable")

    out = dm.auto_label(conn, sample_icp, cfg, runner=boom)
    assert out["runner"] == "none"      # the exception fired before any batch was counted as used
    assert out["batches"] == 0
    assert out["labeled"] == 1          # heuristic still ran and caught the "Owner" title
    assert out["unlabeled"] == 0


def test_auto_label_stops_on_no_progress_rather_than_looping_forever(cfg, sample_icp):
    conn = db.connect(cfg.db_path)
    _seed_business(conn, "biz_a", [("X", "Mechanic", "...")])  # neither runner reply nor heuristic can resolve

    calls = {"n": 0}

    def no_op_runner(lines):
        calls["n"] += 1
        return []  # no records applied -> no progress -> the loop must stop after this one batch

    out = dm.auto_label(conn, sample_icp, cfg, runner=no_op_runner)
    assert calls["n"] == 1
    assert out["batches"] == 1
    assert out["labeled"] == 0
    assert out["unlabeled"] == 1


def test_auto_label_respects_max_batches(cfg, sample_icp):
    conn = db.connect(cfg.db_path)
    for i in range(5):
        _seed_business(conn, f"biz_{i}", [(f"Name{i}", "Owner", "...")])

    # batch=1, max_batches=2: only 2 of the 5 businesses go through the runner; heuristics (every
    # candidate title is "Owner", which matches sample_icp) mop up the rest — proving the loop
    # actually stopped at the cap rather than draining dm_pending on its own.
    class FakeAgent:
        batch = 1
        max_batches = 2

    class FakeCfg:
        agent = FakeAgent()

    calls = {"n": 0}

    def runner(lines):
        calls["n"] += 1
        return [{"biz": json.loads(ln)["biz"], "pick": 0, "confidence": 0.8} for ln in lines]

    out = dm.auto_label(conn, sample_icp, FakeCfg(), runner=runner)
    assert calls["n"] == 2
    assert out["batches"] == 2
    assert out["runner"] == "agent"
    assert out["unlabeled"] == 0
    assert out["labeled"] == 5
