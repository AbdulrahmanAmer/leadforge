"""v0.3 discovery coverage (docs/09 unit A): saturation subdivision (A2), resume completing
discovery from any stage (A3), Google Business Profile facts kept from the raw provider payload
(A5), and provider field-map dispatch for rows that carry a phone but no place_id (A6).
"""
from __future__ import annotations

import itertools
import json
from collections import Counter
from pathlib import Path

import yaml

from leadforge import db
from leadforge.grid import Tile
from leadforge.models import ICP, RawListing
from leadforge.normalize import _appointments_from_about, _reply_signatures, _review_credited_names, to_business
from leadforge.providers.base import DiscoveryProvider, register, register_field_map
from leadforge.util import now_iso


def _minimal_icp(**caps) -> ICP:
    body = {"campaign": "cov-test", "offer": {"what": "x"},
            "target": {"categories": ["auto repair shop"],
                       "geography": {"areas": ["Manchester"], "country": "GB"}}}
    if caps:
        body["caps"] = caps
    return ICP.model_validate(body)


# --------------------------------------------------------------------------------------- A2
def test_saturation_subdivision_creates_children_and_respects_max_depth(cfg, monkeypatch):
    """A saturated tile is split into 4 quadrant children one depth deeper, and those children are
    picked up by the SAME run_discover call (in-memory queue growth) and stop subdividing once
    max_subdivisions is reached — never a depth beyond it."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    cfg.discovery.subdivide_at = 12
    cfg.discovery.max_subdivisions = 2
    icp = _minimal_icp(max_leads=100_000)
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    counter = itertools.count()
    fetched_depths: list[int] = []

    @register
    class FakeTiledA2(DiscoveryProvider):
        name = "faketiled_a2"
        supports_tiles = True

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            fetched_depths.append(query.tile.depth)
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": f"Shop {next(counter)}", "place_id": f"PID_{next(counter)}"})
                for _ in range(cfg.discovery.subdivide_at)]

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    root_tile = Tile(bbox=(-2.4, 53.3, -2.0, 53.6), cell_km=5.0, depth=0)
    db.add_queries(conn, run_id, [("shops in Manchester", root_tile.to_json())])

    from leadforge.pipeline import run_discover
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="faketiled_a2")

    conn = db.connect(cfg.db_path)
    rows = conn.execute("SELECT tile_json FROM queries WHERE run_id=?", (run_id,)).fetchall()
    depths = Counter(json.loads(r["tile_json"])["depth"] for r in rows)

    assert depths[0] == 1                    # the original tile
    assert depths[1] == 4                    # 1 saturated parent -> 4 children at depth 1
    assert depths[2] == 16                   # 4 saturated depth-1 parents -> 4 children each
    assert 3 not in depths                   # max_subdivisions=2: never a depth-3 child
    assert sorted(set(fetched_depths)) == [0, 1, 2]  # every inserted child really got fetched


def test_subdivision_children_use_same_query_text_and_parent_stays_marked_done(cfg, monkeypatch):
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    cfg.discovery.subdivide_at = 2
    cfg.discovery.subdivide_min_new = 0  # ADR-016 gate off: this test is about subdivision mechanics
    cfg.discovery.max_subdivisions = 1
    icp = _minimal_icp(max_leads=100_000)
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    @register
    class FakeTiledA2b(DiscoveryProvider):
        name = "faketiled_a2b"
        supports_tiles = True

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            n = 2 if query.tile.depth == 0 else 0  # only the root saturates
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": f"Shop {query.tile.depth}-{i}", "phone": f"161 496 {i:04d}",
                "place_id": f"PID_{query.tile.depth}_{i}"}) for i in range(n)]

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    root_tile = Tile(bbox=(-2.4, 53.3, -2.0, 53.6), cell_km=5.0, depth=0)
    db.add_queries(conn, run_id, [("shops in Manchester", root_tile.to_json())])

    from leadforge.pipeline import run_discover
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="faketiled_a2b")

    conn = db.connect(cfg.db_path)
    rows = conn.execute("SELECT query_text, status, tile_json FROM queries WHERE run_id=?", (run_id,)).fetchall()
    assert len(rows) == 5  # root + 4 children
    assert all(r["query_text"] == "shops in Manchester" for r in rows)  # A2: same text on every child
    assert all(r["status"] == "done" for r in rows)  # nothing left pending after one full pass


def test_subdivision_children_persisted_before_a_crash_are_picked_up_by_a_later_resume(cfg, monkeypatch):
    """A2: 'persisted before the parent is marked finished ... idempotent on resume' means a child
    inserted by one run_discover call but never fetched (the process died right after insertion)
    must still be found and fetched by the NEXT run_discover call for the same run_id — no special
    subdivision-aware resume logic exists or is needed, because it is just another pending query."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    fetched_texts: list[str] = []

    @register
    class FakeCrashResumeA2(DiscoveryProvider):
        name = "fakecrashresume_a2"
        supports_tiles = True

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            fetched_texts.append(f"{query.text}@depth{query.tile.depth}")
            return []

    icp = _minimal_icp()
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    parent = Tile(bbox=(-2.4, 53.3, -2.0, 53.6), cell_km=5.0, depth=0)
    db.add_queries(conn, run_id, [("shops in Manchester", parent.to_json())])
    parent_id = conn.execute("SELECT id FROM queries WHERE run_id=?", (run_id,)).fetchone()["id"]
    db.finish_query(conn, parent_id, "done", 100)  # the parent's own fetch already happened...
    # ...and the crash happened right after its children were persisted (docs/09 A2 ordering) but
    # before this process got to fetch them — simulated directly, the same state a real crash leaves.
    child = Tile(bbox=(-2.4, 53.3, -2.2, 53.45), cell_km=2.5, depth=1)
    db.add_queries(conn, run_id, [("shops in Manchester", child.to_json())])

    from leadforge.pipeline import run_discover
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="fakecrashresume_a2")

    assert fetched_texts == ["shops in Manchester@depth1"]  # only the still-pending child was fetched
    conn = db.connect(cfg.db_path)
    pending = conn.execute("SELECT COUNT(*) c FROM queries WHERE run_id=? AND status='pending'",
                           (run_id,)).fetchone()["c"]
    assert pending == 0


def test_subdivision_never_fires_when_the_answering_provider_ignores_tiles(cfg, monkeypatch):
    """A2 review (major): a chain fallback that ignores query.tile (supports_tiles=False) but still
    returns >= subdivide_at whole-area rows must NOT trigger subdivision — splitting a whole-area
    answer into quadrants only gets the same whole-area rows back on each child, recursing pointlessly
    against the politeness budget. Gate on which provider actually answered, not just result size."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    cfg.discovery.subdivide_at = 2
    cfg.discovery.max_subdivisions = 2
    icp = _minimal_icp(max_leads=100_000)
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    @register
    class FakeUntiledSaturated(DiscoveryProvider):
        name = "fakeuntiled_saturated_a2"
        supports_tiles = False  # ignores query.tile entirely, like FallbackRestProvider

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": f"Shop {i}", "place_id": f"PID_{i}"}) for i in range(5)]  # >= subdivide_at

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    root_tile = Tile(bbox=(-2.4, 53.3, -2.0, 53.6), cell_km=5.0, depth=0)
    db.add_queries(conn, run_id, [("shops in Manchester", root_tile.to_json())])

    from leadforge.pipeline import run_discover
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="fakeuntiled_saturated_a2")

    conn = db.connect(cfg.db_path)
    rows = conn.execute("SELECT tile_json FROM queries WHERE run_id=?", (run_id,)).fetchall()
    assert len(rows) == 1  # no children inserted — the answering provider never honoured the tile


def test_crash_between_child_insert_and_parent_finish_does_not_duplicate_children(cfg, monkeypatch):
    """A2 'idempotent on resume': the crash window the persist-before-finish ordering exists for is
    children inserted, parent NOT yet marked done. If the process dies right there and a later
    --resume re-fetches the still-pending parent, the parent saturates again and must NOT get a
    second set of 4 children — db.add_queries has no uniqueness, so pipeline.py itself must dedupe
    against tile_json rows already persisted for this (run_id, query_text)."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    cfg.discovery.subdivide_at = 2
    cfg.discovery.subdivide_min_new = 0  # ADR-016 gate off: this test is about subdivision mechanics
    cfg.discovery.max_subdivisions = 1
    icp = _minimal_icp(max_leads=100_000)
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    @register
    class FakeCrashDupA2(DiscoveryProvider):
        name = "fakecrashdup_a2"
        supports_tiles = True

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            n = 2 if query.tile.depth == 0 else 0  # only the root saturates
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": f"Shop {query.tile.depth}-{i}", "place_id": f"PID_{query.tile.depth}_{i}"})
                for i in range(n)]

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    root_tile = Tile(bbox=(-2.4, 53.3, -2.0, 53.6), cell_km=5.0, depth=0)
    db.add_queries(conn, run_id, [("shops in Manchester", root_tile.to_json())])

    # Simulate the crash: finish_query raises the FIRST time it is called (i.e. right after the
    # children were persisted, before the parent itself is marked finished — the exact ordering
    # docs/09 A2 prescribes so children survive a crash there).
    real_finish = db.finish_query
    state = {"crashed": False}

    def crashing_finish(conn_, qid, status, count, **kw):
        if not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("simulated crash after children persisted, before parent finished")
        return real_finish(conn_, qid, status, count)

    monkeypatch.setattr("leadforge.pipeline.db.finish_query", crashing_finish)

    from leadforge.pipeline import run_discover
    try:
        run_discover(cfg, icp, icp_path, run_id=run_id, provider="fakecrashdup_a2")
    except RuntimeError:
        pass

    conn = db.connect(cfg.db_path)
    after_crash = Counter(json.loads(r["tile_json"])["depth"] for r in
                          conn.execute("SELECT tile_json FROM queries WHERE run_id=?", (run_id,)))
    assert after_crash[1] == 4  # children persisted before the simulated crash

    # --resume: parent is still 'pending' (finish_query never completed for it), so run_discover
    # re-fetches it, it saturates again, and must not insert a second batch of 4 children.
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="fakecrashdup_a2")
    conn = db.connect(cfg.db_path)
    after_resume = Counter(json.loads(r["tile_json"])["depth"] for r in
                           conn.execute("SELECT tile_json FROM queries WHERE run_id=?", (run_id,)))
    assert after_resume[1] == 4, f"resume duplicated children: {dict(after_resume)}"
    assert after_resume[0] == 1  # still just the one root


# --------------------------------------------------------------------------------------- item 1
def test_max_leads_is_a_per_run_cap_across_resumes(cfg, monkeypatch):
    """item 1 (docs/09) BLOCKER: `processed` must be seeded from businesses already credited to
    THIS run (first_run_id=run_id), not 0, on every run_discover call — otherwise a cap-stopped run
    scrapes another max_leads worth of businesses each time it is --resumed."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)

    @register
    class FakeCapResume(DiscoveryProvider):
        name = "fakecapresume"
        supports_tiles = False

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            n = int(query.text[-1])
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": f"Shop {query.text}-{i}", "place_id": f"PID_CAPRESUME_{query.text}_{i}"})
                for i in range(n)]

    icp = _minimal_icp(max_leads=3)
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    db.add_queries(conn, run_id, [("cap query 5", None), ("cap query 3", None)])

    from leadforge.pipeline import run_discover
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="fakecapresume")
    conn = db.connect(cfg.db_path)
    n1 = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    assert n1 == 3  # capped at 3 mid-way through the first (5-listing) query
    pending1 = conn.execute("SELECT COUNT(*) c FROM queries WHERE run_id=? AND status='pending'",
                            (run_id,)).fetchone()["c"]
    assert pending1 == 1  # the second query never got a turn

    # --resume: SAME cap, SAME run_id, one query (worth 3 MORE businesses if fetched) still pending.
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="fakecapresume")
    conn = db.connect(cfg.db_path)
    n2 = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    assert n2 == 3, f"cap was not honoured across --resume: {n2} businesses (expected 3)"


def test_run_discover_records_cap_reached_in_run_stats_when_it_stops_on_the_cap(cfg, monkeypatch):
    """item 1 (docs/09): the run's stats_json records cap_reached=True only when the loop actually
    broke because caps.max_leads was hit — not merely because a --limit smoke-test value was hit,
    and not when discovery simply finished with room to spare."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)

    @register
    class FakeCapFlag(DiscoveryProvider):
        name = "fakecapflag"
        supports_tiles = False

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": f"Shop {i}", "place_id": f"PID_CAPFLAG_{i}"}) for i in range(5)]

    icp = _minimal_icp(max_leads=2)
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    db.add_queries(conn, run_id, [("q1", None)])

    from leadforge.pipeline import run_discover
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="fakecapflag")
    conn = db.connect(cfg.db_path)
    stats = json.loads(conn.execute("SELECT stats_json FROM runs WHERE id=?", (run_id,)).fetchone()["stats_json"])
    assert stats["cap_reached"] is True

    # a second ICP with room to spare (cap raised well past what's credited) must NOT claim cap_reached
    icp2 = _minimal_icp(max_leads=1000)
    icp_path2 = cfg.workspace / "icp2.yaml"
    icp_path2.write_text(yaml.safe_dump(icp2.model_dump(mode="json")), encoding="utf-8")
    conn = db.connect(cfg.db_path)
    run_id2 = db.create_run(conn, str(icp_path2), icp2.icp_hash())
    db.add_queries(conn, run_id2, [("q1", None)])
    run_discover(cfg, icp2, icp_path2, run_id=run_id2, provider="fakecapflag")
    conn = db.connect(cfg.db_path)
    stats2 = json.loads(conn.execute("SELECT stats_json FROM runs WHERE id=?", (run_id2,)).fetchone()["stats_json"])
    assert stats2.get("cap_reached") is not True


# --------------------------------------------------------------------------------------- A3
def test_resume_completes_discovery_from_exported_stage_with_pending_queries(cfg, monkeypatch):
    """docs/09 A3: the live campaign's run sat at stage 'exported' with 18 queries still pending.
    resume=True must re-enter discovery from ANY stage when pending/degraded queries remain, then
    carry the newly-discovered businesses through enrich -> (dm skipped) -> score -> export."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)

    @register
    class FakeResumeA3(DiscoveryProvider):
        name = "fakeresume_a3"
        supports_tiles = False

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            n = int(query.text[-1])
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": f"Garage {query.text}", "phone": f"161 496 000{n}",
                "place_id": f"PID_RESUME_{n}"})]

    cfg.discovery.providers = ["fakeresume_a3"]
    icp = _minimal_icp()
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    db.add_queries(conn, run_id, [("query 1", None), ("query 2", None)])
    db.set_stage(conn, run_id, "exported")  # simulate the live run: exported, but 2 queries pending

    from leadforge.pipeline import run_pipeline
    result = run_pipeline(cfg, icp, icp_path, resume=True, skip_dm=True)

    assert result["stage"] == "exported"
    conn = db.connect(cfg.db_path)
    pending = conn.execute("SELECT COUNT(*) c FROM queries WHERE run_id=? AND status='pending'",
                           (run_id,)).fetchone()["c"]
    assert pending == 0  # both formerly-pending queries finished
    n = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    assert n == 2  # both newly-discovered businesses landed
    scored = conn.execute("SELECT COUNT(*) c FROM scores WHERE run_id=?", (run_id,)).fetchone()["c"]
    assert scored == 2  # ... and were carried through scoring, not just discovery


def test_resume_without_pending_or_degraded_queries_does_not_reenter_discovery(cfg, monkeypatch):
    """The broadened A3 check must not fire when a run genuinely has nothing left pending — only
    'enriching' (a mid-enrich kill) should still re-enter, exactly as before v0.3."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    calls = {"n": 0}

    def _boom(self, icp, icp_path, limit=None, provider=None, run_id=None):
        calls["n"] += 1
        raise AssertionError("run_discover must not be called — no pending/degraded queries exist")

    icp = _minimal_icp()
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    db.add_queries(conn, run_id, [("query 1", None)])
    db.finish_query(conn, conn.execute("SELECT id FROM queries WHERE run_id=?", (run_id,)).fetchone()["id"],
                    "done", 0)
    db.set_stage(conn, run_id, "exported")

    monkeypatch.setattr("leadforge.pipeline.run_discover", _boom)
    from leadforge.pipeline import run_pipeline
    result = run_pipeline(cfg, icp, icp_path, resume=True, skip_dm=True)
    assert calls["n"] == 0
    assert result["stage"] == "exported"


# ------------------------------------------------------------------------ A3 x item 1 (cap honesty)
def test_a3_stopped_exactly_at_cap_stays_stopped_on_resume(cfg, monkeypatch):
    """item 1 (docs/09): a run that stopped exactly at caps.max_leads must NOT re-enter discovery
    on --resume just because pending/degraded queries remain — the cap is a PER-RUN hard stop, and
    re-entering would silently blow through it (docs/04 §5)."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    calls = {"n": 0}

    def _boom(cfg_, icp_, icp_path_, limit=None, provider=None, run_id=None):
        calls["n"] += 1
        raise AssertionError("run_discover must not be called — this run already hit its cap")

    icp = _minimal_icp(max_leads=2)
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    for i in range(2):  # credited == cap
        biz = to_business(RawListing(provider="gosom", fetched_at=now_iso(), data={
            "title": f"Capped Shop {i}", "place_id": f"PID_STOPPED_{i}"}), run_id, icp, "GB")
        db.upsert_business(conn, biz)
    db.add_queries(conn, run_id, [("still pending", None)])
    db.set_stage(conn, run_id, "exported", cap_reached=True)  # stopped ON the cap, per item 1

    monkeypatch.setattr("leadforge.pipeline.run_discover", _boom)
    from leadforge.pipeline import run_pipeline
    result = run_pipeline(cfg, icp, icp_path, resume=True, skip_dm=True)
    assert calls["n"] == 0
    assert result["stage"] == "exported"


def test_a3_reenters_when_cap_was_raised_past_what_is_credited(cfg, monkeypatch):
    """item 1 (docs/09): the live campaign's exact shape — cap_reached True from a stop under an
    OLDER (lower) cap, but the owners raise caps.max_leads before the tiled sweep, so credited < the
    NEW cap -> discovery must re-enter and pick up the still-pending queries."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)

    @register
    class FakeCapRaised(DiscoveryProvider):
        name = "fakecapraised"
        supports_tiles = False

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": "New Shop", "place_id": "PID_CAPRAISED_NEW"})]

    icp = _minimal_icp(max_leads=1000)  # raised well past the 2 already credited
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")
    cfg.discovery.providers = ["fakecapraised"]

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    for i in range(2):
        biz = to_business(RawListing(provider="gosom", fetched_at=now_iso(), data={
            "title": f"Old Capped Shop {i}", "place_id": f"PID_OLDCAP_{i}"}), run_id, icp, "GB")
        db.upsert_business(conn, biz)
    db.add_queries(conn, run_id, [("still pending", None)])
    db.set_stage(conn, run_id, "exported", cap_reached=True)  # stopped under the OLD, lower cap

    from leadforge.pipeline import run_pipeline
    result = run_pipeline(cfg, icp, icp_path, resume=True, skip_dm=True)
    assert result["stage"] == "exported"
    conn = db.connect(cfg.db_path)
    n = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    assert n == 3  # 2 pre-existing + 1 discovered after the cap was raised


# --------------------------------------------------------------------------------------- A5
_GBP_FIXTURE = Path(__file__).parent / "fixtures" / "gosom_gbp_sample.ndjson"


def _gbp_rows():
    return [json.loads(ln) for ln in _GBP_FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_gbp_facts_extracted_from_gosom_raw_payload():
    icp = ICP.model_validate({
        "campaign": "t", "offer": {"what": "x"},
        "target": {"categories": ["auto repair shop"], "geography": {"areas": ["Manchester"], "country": "GB"}},
    })
    rows = _gbp_rows()
    assert len(rows) == 2
    biz = to_business(RawListing(provider="gosom", fetched_at=now_iso(), data=rows[0]), "run_x", icp, "GB")
    assert biz is not None
    gbp = biz.enrich["gbp"]
    assert gbp["appointments"] == "recommended"
    assert gbp["booking_links"] == [
        "https://riverside-auto.example/book", "https://wa.me/447700900123",
    ]
    # A5 review (minor): gosom v1.17.4 emits '' for the overwhelming majority of live places (the
    # 'OPERATIONAL' the pre-fix fixture used was invented, not observed) — the fixture reflects that.
    assert gbp["status"] == ""
    assert gbp["owner_name"] == "Riverside Auto Repair (Owner)"
    assert gbp["reply_signatures"] == ["Sam"]      # every signed reply names the same person once
    # "Ali" is credited in 4 distinct reviews (>= 3); "This" ('This was fixed...') appears 3x too but
    # is a sentence-initial stopword, not a name, and must be filtered out.
    assert gbp["review_names"] == ["Ali"]
    assert gbp["reviews_captured"] == 9
    assert gbp["description"] == "Family-run MOT and repair garage serving the local area since 1998."


def test_reply_signatures_excludes_non_name_signoffs():
    """A5 review (major): 'Thanks Customer', 'Thanks Car Care Team', 'Regards MS' are not owner first
    names — the live-data leak the reviewer measured (Customer x6, Car x5, MS x5). MS is also an
    all-caps acronym, rejected regardless of the stoplist."""
    reviews = [
        {"reply_text_original": "Thanks Customer"},
        {"reply_text_original": "Thanks Car Care Team"},
        {"reply_text_original": "Regards MS"},
        {"reply_text_original": "Thanks so much for the kind words! Regards, Sam"},
    ]
    assert _reply_signatures(reviews) == ["Sam"]


def test_reply_signatures_excludes_the_reviewer_own_name_echoed_back():
    """A5 review (major): 'Thanks <Name>' is the DOMINANT reply form on the live cache and it is the
    owner thanking the REVIEWER by name, not a sign-off — must be rejected when the signed name
    equals the review author's own first token ('Name'/'name' on that same review), even though it
    passes the stoplist, the plausible-name check and the tail-of-reply window."""
    reviews_echo = [
        {"Name": "Sam", "reply_text_original": "Thanks Sam, glad we could help with your MOT!"},
    ]
    assert _reply_signatures(reviews_echo) == []  # rejected: echoes the reviewer's own first name

    reviews_real_signoff = [
        {"Name": "Priya", "reply_text_original": "Thanks so much for the kind words! Regards, Marcus"},
    ]
    assert _reply_signatures(reviews_real_signoff) == ["Marcus"]  # unrelated name -> still accepted

    reviews_full_name_reviewer = [
        {"Name": "Samantha Rivers", "reply_text_original": "Thanks so much! Regards, Sam"},
    ]
    # only the FIRST token of the reviewer's name is compared — "Sam" != "Samantha" -> accepted
    assert _reply_signatures(reviews_full_name_reviewer) == ["Sam"]


def test_review_credited_names_excludes_mot_and_all_caps():
    """A5 review (major): 'MOT' surfaced as a credited review name on live data — it is an all-caps
    acronym (vehicle test), not a person, even when it appears >= 3 times."""
    reviews = [{"Description": "The MOT was quick and easy"} for _ in range(4)]
    assert _review_credited_names(reviews) == []


def test_appointments_from_about_respects_explicit_enabled_false():
    """A5 review (minor): an explicit {'name': 'Appointment required', 'enabled': False} must not be
    reported as 'required' — the enabled flag was previously ignored entirely."""
    about = [{"id": "planning", "name": "Planning",
              "options": [{"name": "Appointment required", "enabled": False}]}]
    assert _appointments_from_about(about) == "none"
    # absent 'enabled' still counts (gosom's own default for a listed option)
    about2 = [{"id": "planning", "name": "Planning", "options": [{"name": "Appointment required"}]}]
    assert _appointments_from_about(about2) == "required"


def test_gbp_facts_default_to_empty_never_none_when_absent():
    icp = ICP.model_validate({
        "campaign": "t", "offer": {"what": "x"},
        "target": {"categories": ["auto repair shop"], "geography": {"areas": ["Manchester"], "country": "GB"}},
    })
    rows = _gbp_rows()
    biz = to_business(RawListing(provider="gosom", fetched_at=now_iso(), data=rows[1]), "run_x", icp, "GB")
    assert biz is not None
    gbp = biz.enrich["gbp"]
    assert gbp == {
        "appointments": "none", "booking_links": [], "status": "", "owner_name": "",
        "reply_signatures": [], "review_names": [], "reviews_captured": 0, "description": "",
    }
    for v in gbp.values():
        assert v is not None


# --------------------------------------------------------------------------------------- A6
def test_normalize_dispatches_on_provider_field_map_without_place_id():
    """A provider with no place_id (e.g. a future registry provider) must still phone-merge: its
    field map is honored via get_field_map(raw.provider), and the dedupe key stays 'na:'-prefixed."""
    register_field_map("faketest_a6", {"phone": ["tel"]})  # only override what differs from gosom
    raw = RawListing(provider="faketest_a6", fetched_at=now_iso(), data={
        "title": "Acme Registry Co", "tel": "01483 970410",
        "address": "1 High St, Guildford, United Kingdom",
        "complete_address": {"street": "1 High St", "city": "Guildford", "country": "GB"},
    })
    biz = to_business(raw, "run_x", None, "GB")
    assert biz is not None
    assert biz.place_id is None
    assert biz.phone_e164 == "+441483970410"          # dispatched via the provider's own field map
    assert biz.dedupe_key.startswith("na:")             # unchanged dedupe convention for place_id-less rows


def test_resume_finds_a_run_stamped_with_the_legacy_hash_and_restamps_it(cfg):
    """Upgrade path for the 2026-09-03 hash change: a run created by an older version (caps in the hash)
    is still the campaign's run, and after one lookup it carries the current hash."""
    from leadforge.pipeline import _latest_run
    icp = _minimal_icp(max_leads=6000)
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, "icp.yaml", icp.icp_hash_legacy())
    assert db.latest_run(conn, icp.icp_hash()) is None
    found = _latest_run(conn, icp)
    assert found is not None and found["id"] == run_id
    assert db.latest_run(conn, icp.icp_hash())["id"] == run_id
    # and the raised-cap ICP still resolves to the same run (the whole point)
    assert _latest_run(conn, _minimal_icp(max_leads=30000))["id"] == run_id


def test_saturated_tile_with_nothing_new_does_not_subdivide(cfg, monkeypatch):
    """ADR-016 (live 2026-09-03): a saturated tile whose listings were all already known spawns no
    children; the root (all new) still does. The provider returns the SAME 12 listings every fetch."""
    monkeypatch.setattr("leadforge.pipeline.ensure_ready", lambda c: None)
    cfg.discovery.subdivide_at = 12
    cfg.discovery.max_subdivisions = 2
    cfg.discovery.subdivide_min_new = 3
    icp = _minimal_icp(max_leads=100_000)
    icp_path = cfg.workspace / "icp.yaml"
    icp_path.write_text(yaml.safe_dump(icp.model_dump(mode="json")), encoding="utf-8")

    @register
    class FakeTiledSame(DiscoveryProvider):
        name = "faketiled_same"
        supports_tiles = True

        def available(self):
            return True, "fake"

        def fetch(self, query, limit=None):
            return [RawListing(provider=self.name, fetched_at=now_iso(), data={
                "title": f"Shop {i}", "place_id": f"PID_SAME_{i}"}) for i in range(12)]

    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
    root_tile = Tile(bbox=(-2.4, 53.3, -2.0, 53.6), cell_km=5.0, depth=0)
    db.add_queries(conn, run_id, [("shops in Manchester", root_tile.to_json())])

    from leadforge.pipeline import run_discover
    run_discover(cfg, icp, icp_path, run_id=run_id, provider="faketiled_same")
    conn = db.connect(cfg.db_path)
    rows = conn.execute("SELECT tile_json, new_count, status FROM queries WHERE run_id=?", (run_id,)).fetchall()
    depths = Counter(json.loads(r["tile_json"])["depth"] for r in rows)
    assert depths[0] == 1 and depths[1] == 4     # root: 12 new -> 4 children
    assert 2 not in depths                        # children: 0 new -> no grandchildren
    by_depth = {json.loads(r["tile_json"])["depth"]: r["new_count"] for r in rows}
    assert by_depth[0] == 12 and by_depth[1] == 0  # new_count recorded per query
    assert all(r["status"] == "done" for r in rows)


def test_prune_child_tiles_skips_children_of_parents_that_found_nothing_new(cfg):
    conn = db.connect(cfg.db_path)
    run_id = db.create_run(conn, "icp.yaml", "h")
    parent_dry = Tile(bbox=(0.0, 0.0, 1.0, 1.0), cell_km=5.0, depth=0)
    parent_rich = Tile(bbox=(2.0, 2.0, 3.0, 3.0), cell_km=5.0, depth=0)
    parent_old = Tile(bbox=(4.0, 4.0, 5.0, 5.0), cell_km=5.0, depth=0)
    db.add_queries(conn, run_id, [("shops", parent_dry.to_json()), ("shops", parent_rich.to_json()),
                                  ("shops", parent_old.to_json())])
    ids = [r["id"] for r in conn.execute("SELECT id FROM queries WHERE run_id=? ORDER BY id", (run_id,))]
    db.finish_query(conn, ids[0], "done", 120, new_count=0)
    db.finish_query(conn, ids[1], "done", 120, new_count=40)
    db.finish_query(conn, ids[2], "done", 120, new_count=None)   # recorded by an older version
    from leadforge.grid import quarter_tile
    children = [("shops", c.to_json()) for p in (parent_dry, parent_rich, parent_old) for c in quarter_tile(p)]
    db.add_queries(conn, run_id, children)
    db.add_queries(conn, run_id, [("shops in town", None)])      # an untiled pending query is never touched

    dry = db.prune_child_tiles(conn, run_id, 3, dry_run=True)
    assert dry == {"pending": 13, "children": 12, "skipped": 4, "kept_parent_yielded": 4, "kept_parent_unknown": 4}
    assert conn.execute("SELECT COUNT(*) FROM queries WHERE status='skipped'").fetchone()[0] == 0
    real = db.prune_child_tiles(conn, run_id, 3)
    assert real["skipped"] == 4
    assert conn.execute("SELECT COUNT(*) FROM queries WHERE status='pending'").fetchone()[0] == 9
    blunt = db.prune_child_tiles(conn, run_id, 3, all_children=True)
    assert blunt["skipped"] == 8
    assert conn.execute("SELECT COUNT(*) FROM queries WHERE status='pending'").fetchone()[0] == 1
