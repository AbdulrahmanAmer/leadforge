"""Pipeline orchestrator (U3.5 discover + the resumable `run` state machine) — docs/04 §2.

Stage transitions are persisted to SQLite so `run --resume` continues where an interrupted run stopped.
Every stage is idempotent (upserts, per-query checkpoints). The DM stage is the only one that pauses
for the agent (stage=dm_pending).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from leadforge import db
from leadforge.config import Config
from leadforge.doctor import ensure_ready
from leadforge.grid import build_plan
from leadforge.models import ICP
from leadforge.normalize import to_business
from leadforge.providers.base import get_chain
from leadforge.util import (
    LOG,
    ProviderDegraded,
    ProviderFailed,
    emit_progress,
    open_artifact,
    open_progress_window,
    set_progress_file,
)


# --------------------------------------------------------------------------- discover
def _progress_ui(cfg: Config) -> None:
    """Feed file for `leadforge watch` + auto console window when running headless (agent-driven)."""
    import sys
    set_progress_file(cfg.data_path / "progress.jsonl")
    try:
        headless = not sys.stderr.isatty()
    except Exception:  # noqa: BLE001
        headless = True
    if headless and cfg.progress_window:
        open_progress_window(cfg.workspace)


def run_discover(cfg: Config, icp: ICP, icp_path: Path, limit: int | None = None,
                 provider: str | None = None, run_id: str | None = None) -> tuple[str, dict, list[str]]:
    ensure_ready(cfg)
    _progress_ui(cfg)
    conn = db.connect(cfg.db_path)
    if run_id is None:
        existing = db.latest_run(conn, icp.icp_hash())
        if existing and existing["stage"] in ("planned", "discovering"):
            run_id = existing["id"]
        else:
            run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
            _plan_into_db(conn, cfg, icp, run_id)
    db.set_stage(conn, run_id, "discovering")

    chain = get_chain(cfg, only=provider)
    warns: list[str] = []
    degraded = 0
    new_count = 0
    processed = 0
    pending = db.pending_queries(conn, run_id)
    from leadforge.grid import PlannedQuery

    hard_cap = min(limit, icp.caps.max_leads) if limit else icp.caps.max_leads
    total_q = len(pending)
    for qi, q in enumerate(pending):
        emit_progress("discover", qi, total_q, q["query_text"])
        if processed >= hard_cap:
            emit_progress("discover", total_q, total_q, f"lead cap reached ({processed})")
            break
        pq = PlannedQuery(text=q["query_text"], category="", area="",
                          tile=_tile_from_json(q["tile_json"]))
        listings, status = _fetch_with_chain(chain, pq, limit, warns)
        if status == "degraded":
            degraded += 1
        per_query_new = 0
        for raw in listings:
            # campaign country drives phone/address parsing, not a global default
            biz = to_business(raw, run_id, icp, icp.target.geography.country or cfg.default_region)
            if biz is None:
                continue
            if db.is_suppressed(conn, biz.domain, biz.place_id):
                continue
            _bid, created = db.upsert_business(conn, biz)
            per_query_new += int(created)
            # cap counts UNIQUE leads, not raw listings: cross-category duplicates (a garage that is
            # also an MOT centre) must not consume max_leads (found live: 1000-cap run stopped at 709)
            processed += int(created)
            if processed >= hard_cap:
                break
        new_count += per_query_new
        db.finish_query(conn, q["id"], status, len(listings))
        emit_progress("discover", qi + 1, total_q, f"{processed} unique leads so far")

    total_biz = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    still_pending = len(db.pending_queries(conn, run_id))
    if still_pending == 0 or processed >= hard_cap:
        db.set_stage(conn, run_id, "discovered", businesses=total_biz)
    if degraded:
        warns.append(f"{degraded} queries degraded (captcha/timeout); rerun with --resume later")
    counts = {"businesses": total_biz, "new": new_count, "tiles_degraded": degraded,
              "queries_done": len(pending) - still_pending}
    return run_id, counts, warns[:5]


def _fetch_with_chain(chain, pq, limit, warns) -> tuple[list, str]:
    last_degraded = False
    for provider in chain:
        ok, reason = provider.available()
        if not ok:
            warns.append(f"{provider.name}: {reason}")
            continue
        try:
            return provider.fetch(pq, limit=limit), "done"
        except ProviderDegraded as e:
            LOG.warning("provider %s degraded: %s", provider.name, e)
            last_degraded = True
            continue  # try next provider in the chain
        except ProviderFailed as e:
            warns.append(f"{provider.name} failed: {e}")
            continue
    return [], "degraded" if last_degraded else "failed"


def _plan_into_db(conn: sqlite3.Connection, cfg: Config, icp: ICP, run_id: str) -> None:

    queries = build_plan(icp, cfg)
    db.add_queries(conn, run_id, [(q.text, q.tile.as_json() if q.tile else None) for q in queries])


def _tile_from_json(tile_json: str | None):
    if not tile_json:
        return None
    import json

    from leadforge.grid import Tile

    d = json.loads(tile_json)
    return Tile(bbox=tuple(d["bbox"]), cell_km=d["cell_km"])


# --------------------------------------------------------------------------- run (state machine)
def run_pipeline(cfg: Config, icp: ICP, icp_path: Path, resume: bool = False,
                 limit: int | None = None, skip_dm: bool = False) -> dict:
    ensure_ready(cfg)
    conn = db.connect(cfg.db_path)
    _progress_ui(cfg)
    run = db.latest_run(conn, icp.icp_hash()) if resume else None
    stage = run["stage"] if run else "planned"
    run_id = run["id"] if run else None
    warns: list[str] = []
    artifacts: list[str] = []

    # DISCOVER
    if stage in ("planned", "discovering") or run_id is None:
        run_id, dcounts, dwarn = run_discover(cfg, icp, icp_path, limit=limit, run_id=run_id)
        warns += dwarn
        stage = db.latest_run(conn, icp.icp_hash())["stage"]

    conn = db.connect(cfg.db_path)  # refresh connection view

    # ENRICH
    if stage == "discovered":
        db.set_stage(conn, run_id, "enriching")
        from leadforge.enrich.runner import run_enrich
        ecounts = run_enrich(conn, cfg, limit or icp.caps.max_sites, stage="all")
        if ecounts.get("needs_browser"):
            warns.append(f"{ecounts['needs_browser']} sites need a browser pass (pip install -e .[browser])")
        stage = "enriched"
        db.set_stage(conn, run_id, "enriched", **ecounts)

    # DM gate
    if stage == "enriched":
        pending = len(db.dm_pending(conn, 10_000))
        if pending > 0 and not skip_dm:
            db.set_stage(conn, run_id, "dm_pending", dm_pending=pending)
            return {"ok": True, "run": run_id, "stage": "dm_pending",
                    "counts": {"dm_pending": pending},
                    "warnings": warns, "next": "leadforge dm export --max 60"}
        stage = "scoring"

    if stage == "dm_pending" and not skip_dm:
        # resumed after dm apply — proceed only if nothing is left unlabeled, else keep waiting
        pending = len(db.dm_pending(conn, 10_000))
        if pending > 0:
            return {"ok": True, "run": run_id, "stage": "dm_pending",
                    "counts": {"dm_pending": pending}, "warnings": warns,
                    "next": "leadforge dm export --max 60"}
        stage = "scoring"
    elif stage == "dm_pending" and skip_dm:
        stage = "scoring"

    # SCORE
    if stage in ("scoring", "enriched"):
        from leadforge.score import score_run
        scounts = score_run(conn, icp, run_id)
        db.set_stage(conn, run_id, "scored", **scounts)
        stage = "scored"

    # EXPORT
    if stage == "scored":
        from leadforge.export import export_run, summarize_for_digest, top_hooks
        artifacts = export_run(conn, icp, run_id, cfg.exports_dir, cfg.export.formats,
                               staleness_days=cfg.validation.staleness_days)
        if cfg.export.auto_open:
            xlsx = [a for a in artifacts if a.endswith(".xlsx")]
            if xlsx:
                open_artifact(xlsx[0])
        ecounts = summarize_for_digest(conn, run_id)
        db.set_stage(conn, run_id, "exported", **ecounts)
        warns += top_hooks(conn, run_id)
        return {"ok": True, "run": run_id, "stage": "exported", "counts": ecounts,
                "warnings": warns[:5], "artifacts": artifacts, "next": None}

    return {"ok": True, "run": run_id, "stage": stage, "counts": {}, "warnings": warns[:5], "next": None}
