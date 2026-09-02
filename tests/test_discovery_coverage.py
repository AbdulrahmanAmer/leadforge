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
from leadforge.normalize import to_business
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
    assert gbp["status"] == "OPERATIONAL"
    assert gbp["owner_name"] == "Riverside Auto Repair (Owner)"
    assert gbp["reply_signatures"] == ["Sam"]      # every signed reply names the same person once
    # "Ali" is credited in 4 distinct reviews (>= 3); "This" ('This was fixed...') appears 3x too but
    # is a sentence-initial stopword, not a name, and must be filtered out.
    assert gbp["review_names"] == ["Ali"]
    assert gbp["reviews_captured"] == 9
    assert gbp["description"] == "Family-run MOT and repair garage serving the local area since 1998."


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
