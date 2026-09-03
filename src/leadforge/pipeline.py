"""Pipeline orchestrator (U3.5 discover + the resumable `run` state machine) — docs/04 §2.

Stage transitions are persisted to SQLite so `run --resume` continues where an interrupted run stopped.
Every stage is idempotent (upserts, per-query checkpoints). The DM stage is the only one that pauses
for the agent (stage=dm_pending).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from leadforge import db
from leadforge.config import Config
from leadforge.doctor import ensure_ready
from leadforge.grid import ADDITIVE_PROVIDERS, PlannedQuery, build_plan, quarter_tile
from leadforge.models import ICP
from leadforge.normalize import to_business
from leadforge.providers import base as providers_base
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
        open_progress_window(cfg.workspace, data_dir=cfg.data_path)


def _latest_run(conn, icp: ICP):
    """Latest run of this campaign. Falls back to the pre-2026-09-03 hash (caps included) and re-stamps the
    row so a campaign started by an older version keeps resuming after an upgrade."""
    run = db.latest_run(conn, icp.icp_hash())
    if run is None:
        legacy = db.latest_run(conn, icp.icp_hash_legacy())
        if legacy is not None:
            conn.execute("UPDATE runs SET icp_hash=? WHERE id=?", (icp.icp_hash(), legacy["id"]))
            conn.commit()
            run = db.latest_run(conn, icp.icp_hash())
    return run


def run_discover(cfg: Config, icp: ICP, icp_path: Path, limit: int | None = None,
                 provider: str | None = None, run_id: str | None = None) -> tuple[str, dict, list[str]]:
    ensure_ready(cfg)
    _progress_ui(cfg)
    conn = db.connect(cfg.db_path)
    if run_id is None:
        existing = _latest_run(conn, icp)
        if existing and existing["stage"] in ("planned", "discovering"):
            run_id = existing["id"]
        else:
            run_id = db.create_run(conn, str(icp_path), icp.icp_hash())
            _plan_into_db(conn, cfg, icp, run_id)
    db.set_stage(conn, run_id, "discovering")
    # a degraded query (captcha/timeout) is retryable, and the digest tells the agent
    # "rerun with --resume later" — so a resumed discover must actually re-attempt them
    conn.execute("UPDATE queries SET status='pending' WHERE run_id=? AND status='degraded'", (run_id,))
    conn.commit()

    # ADR-013: register providers (dvsa, ...) never act as a FALLBACK for a Maps query — a degraded tile
    # must stay degraded (and be retried) rather than be "answered" with town-wide register rows. They run
    # through their own planned queries (PlannedQuery.provider), routed below.
    chain = _build_chain(cfg, provider)
    warns: list[str] = []
    # item 1 (docs/09): caps.max_leads is a PER-RUN hard stop across --resume calls, not a per-call
    # one — seed `processed` from businesses already credited to THIS run, or a cap-stopped run could
    # scrape another max_leads worth of businesses on every subsequent --resume.
    processed = conn.execute(
        "SELECT COUNT(*) c FROM businesses WHERE first_run_id=?", (run_id,)
    ).fetchone()["c"]

    # A2: the queue grows in place when a saturated tile is subdivided, so children inserted by an
    # earlier iteration are picked up by this same loop (and, via the DB, by a later --resume too).
    queue = list(db.pending_queries(conn, run_id))
    known_ids = {q["id"] for q in queue}

    # --limit means 'at most N NEW businesses this invocation' (smoke tests), never 'stop because the
    # run already credited N' — otherwise `discover --limit 5` on a resumed run fetched nothing
    hard_cap = min(processed + limit, icp.caps.max_leads) if limit else icp.caps.max_leads

    # v0.3 speed unit: known-place skip hook. Providers that declare a `known_cids` attribute
    # (maps_list) get every CID already in the DB, seeded once here and grown as new businesses are
    # credited (_apply_query_result) — the provider marks repeat cards `data["known"]=True` instead
    # of re-visiting their (expensive) detail page. Providers without the attribute are unaffected.
    known_cids = {r["cid"] for r in conn.execute("SELECT cid FROM businesses WHERE cid IS NOT NULL")}

    ctx = {
        "processed": processed, "degraded": 0, "new_count": 0, "cap_reached": False,
        "known_cids": known_cids, "queue": queue, "known_ids": known_ids, "hard_cap": hard_cap,
    }
    try:
        if cfg.discovery.parallel_queries > 1:
            _run_discover_parallel(conn, cfg, icp, run_id, chain, provider, ctx, limit, warns)
        else:
            _run_discover_serial(conn, cfg, icp, run_id, chain, ctx, limit, warns)
    finally:
        _close_chain(chain)

    processed, degraded, new_count, cap_reached, queue = (
        ctx["processed"], ctx["degraded"], ctx["new_count"], ctx["cap_reached"], ctx["queue"],
    )

    total_biz = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    still_pending = len(db.pending_queries(conn, run_id))
    if still_pending == 0 or processed >= hard_cap:
        db.set_stage(conn, run_id, "discovered", businesses=total_biz, cap_reached=cap_reached)
    if degraded:
        warns.append(f"{degraded} queries degraded (captcha/timeout); "
                     "run --resume retries them until the DM gate")
    counts = {"businesses": total_biz, "new": new_count, "tiles_degraded": degraded,
              "queries_done": len(queue) - still_pending}
    return run_id, counts, warns[:5]


def _build_chain(cfg: Config, provider: str | None):
    return ([p for p in get_chain(cfg, only=provider) if provider or p.name not in ADDITIVE_PROVIDERS]
            or get_chain(cfg, only=provider))


def _close_chain(chain) -> None:
    for p in chain:
        closer = getattr(p, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — teardown must never break a finished discover call
                LOG.debug("provider %s close() failed", getattr(p, "name", "?"))


def _set_known_cids(chain, known_cids: set[str]) -> None:
    for p in chain:
        if hasattr(p, "known_cids"):
            p.known_cids = known_cids


def _apply_query_result(conn, cfg: Config, icp: ICP, run_id: str, q, pq, listings, status,
                        tiled_honoured, ctx: dict) -> None:
    """Everything that happens once ONE query's fetch has returned, regardless of whether that fetch
    ran serially or in a worker thread: normalize + upsert every listing, run A2 saturation
    subdivision, and finish_query. Always called on the main thread (DB writes are never done from a
    worker) — shared by `_run_discover_serial` and `_run_discover_parallel` so the two loop shapes
    can never drift on what "processing a query result" means."""
    if status == "degraded":
        ctx["degraded"] += 1
    per_query_new = 0
    for raw in listings:
        # campaign country drives phone/address parsing, not a global default
        biz = to_business(raw, run_id, icp, icp.target.geography.country or cfg.default_region)
        if biz is None:
            continue
        cls_ = providers_base.PROVIDERS.get(raw.provider)
        enrich_fn = getattr(cls_, "enrich_for", None) or (
            getattr(sys.modules.get(cls_.__module__), "enrich_for", None) if cls_ else None)  # module-level fallback
        if enrich_fn:
            biz.enrich.update(enrich_fn(raw.data))
        if db.is_suppressed(conn, biz.domain, biz.place_id):
            continue
        _bid, created = db.upsert_business(conn, biz)
        per_query_new += int(created)
        # cap counts UNIQUE leads, not raw listings: cross-category duplicates (a garage that is
        # also an MOT centre) must not consume max_leads (found live: 1000-cap run stopped at 709)
        ctx["processed"] += int(created)
        if biz.cid:
            ctx["known_cids"].add(biz.cid)  # speed unit: grow the known-place set as we go
        if ctx["processed"] >= ctx["hard_cap"]:
            if ctx["processed"] >= icp.caps.max_leads:
                ctx["cap_reached"] = True
            break
    ctx["new_count"] += per_query_new

    # A2 saturation subdivision: a tiled query that came back saturated is split into 4 quadrant
    # queries at the next depth, persisted BEFORE the parent is marked finished (so a crash right
    # after this point still has the children on --resume), capped at max_subdivisions deep.
    # Dedupe against already-persisted rows for this (run_id, query_text): a crash between the
    # children insert and finish_query(parent) leaves the parent 'pending', so a later --resume
    # re-fetches it, saturates again, and would otherwise insert a second set of 4 children —
    # quarter_tile(pq.tile) is deterministic (same parent bbox -> same 4 quadrant dicts), so the
    # json.dumps of a child's to_json() matches the tile_json string already on disk byte-for-byte.
    if status == "done" and tiled_honoured and len(listings) >= cfg.discovery.subdivide_at \
            and pq.tile.depth < cfg.discovery.max_subdivisions:
        children = quarter_tile(pq.tile)
        existing_tiles = {
            row["tile_json"] for row in conn.execute(
                "SELECT tile_json FROM queries WHERE run_id=? AND query_text=?",
                (run_id, q["query_text"]),
            )
        }
        new_children = [c for c in children if json.dumps(c.to_json()) not in existing_tiles]
        if new_children:
            db.add_queries(conn, run_id, [(q["query_text"], c.to_json()) for c in new_children])
        for nr in db.pending_queries(conn, run_id):
            if nr["id"] not in ctx["known_ids"]:
                ctx["known_ids"].add(nr["id"])
                ctx["queue"].append(nr)

    db.finish_query(conn, q["id"], status, len(listings))


def _run_discover_serial(conn, cfg: Config, icp: ICP, run_id: str, chain, ctx: dict, limit, warns) -> None:
    """parallel_queries == 1 (the default): unchanged, strictly sequential discover loop."""
    queue = ctx["queue"]
    qi = 0
    while qi < len(queue):
        q = queue[qi]
        total_q = len(queue)
        emit_progress("discover", qi, total_q, q["query_text"])
        if ctx["processed"] >= ctx["hard_cap"]:
            if ctx["processed"] >= icp.caps.max_leads:
                ctx["cap_reached"] = True
            emit_progress("discover", total_q, total_q, f"lead cap reached ({ctx['processed']})")
            break
        only = _provider_from_json(q["tile_json"])
        pq = PlannedQuery(text=q["query_text"], category="", area="",
                          tile=_tile_from_json(q["tile_json"]), provider=only)
        # a register query goes to its own provider only; everything else walks the fallback chain
        query_chain = get_chain(cfg, only=only) if only else chain
        _set_known_cids(query_chain, ctx["known_cids"])
        listings, status, tiled_honoured = _fetch_with_chain(query_chain, pq, limit, warns)
        _apply_query_result(conn, cfg, icp, run_id, q, pq, listings, status, tiled_honoured, ctx)
        emit_progress("discover", qi + 1, len(queue), f"{ctx['processed']} unique leads so far")
        qi += 1


def _run_discover_parallel(conn, cfg: Config, icp: ICP, run_id: str, chain, provider: str | None,
                           ctx: dict, limit, warns) -> None:
    """parallel_queries > 1: fan discover queries across N provider-chain instances (each its own
    browser/subprocess pool — item 6, docs/09 speed unit). Every normal (non-registry) query BORROWS
    one of the N persistent chains for the duration of its fetch and returns it when done — a chain
    (and therefore a maps_list provider's single browser/page) is never used by two fetches at once.
    Registry queries (PlannedQuery.provider set) always build their OWN fresh instance, exactly like
    the serial path, and don't consume a borrowed chain slot; overall concurrency is still capped at
    `n_workers` in-flight fetches. ALL DB writes happen on this (the main) thread, only ever after a
    future completes — workers only ever call the network-facing `_fetch_with_chain`."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    n_workers = max(1, cfg.discovery.parallel_queries)
    chains = [chain] + [_build_chain(cfg, provider) for _ in range(n_workers - 1)]
    free_chains = list(chains)
    queue = ctx["queue"]
    done_count = 0
    qi_submit = 0

    try:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            inflight: dict = {}

            def submit_next() -> None:
                nonlocal qi_submit
                while (qi_submit < len(queue) and len(inflight) < n_workers
                       and ctx["processed"] < ctx["hard_cap"]):
                    q = queue[qi_submit]
                    only = _provider_from_json(q["tile_json"])
                    if not only and not free_chains:
                        break  # every borrowable chain is busy; wait for one to come back
                    pq = PlannedQuery(text=q["query_text"], category="", area="",
                                      tile=_tile_from_json(q["tile_json"]), provider=only)
                    if only:
                        worker_chain, borrowed = get_chain(cfg, only=only), None
                    else:
                        worker_chain = free_chains.pop()
                        borrowed = worker_chain
                    _set_known_cids(worker_chain, ctx["known_cids"])
                    fut = ex.submit(_fetch_with_chain, worker_chain, pq, limit, warns)
                    inflight[fut] = (q, pq, borrowed)
                    qi_submit += 1
                    emit_progress("discover", done_count, len(queue), q["query_text"])

            submit_next()
            while inflight:
                fut = next(as_completed(list(inflight.keys())))
                q, pq, borrowed = inflight.pop(fut)
                if borrowed is not None:
                    free_chains.append(borrowed)
                listings, status, tiled_honoured = fut.result()
                _apply_query_result(conn, cfg, icp, run_id, q, pq, listings, status, tiled_honoured, ctx)
                done_count += 1
                emit_progress("discover", done_count, len(queue), f"{ctx['processed']} unique leads so far")
                submit_next()

        if qi_submit < len(queue) and ctx["processed"] >= ctx["hard_cap"]:
            emit_progress("discover", len(queue), len(queue), f"lead cap reached ({ctx['processed']})")
    finally:
        for extra in chains[1:]:  # chains[0] is `chain` itself — the caller closes it
            _close_chain(extra)


def _fetch_with_chain(chain, pq, limit, warns) -> tuple[list, str, bool]:
    """Returns (listings, status, tiled_honoured). tiled_honoured is True only when the provider that
    actually answered declares supports_tiles=True — a chain fallback that ignores query.tile (e.g.
    FallbackRestProvider) must never be treated as having constrained the search to that tile, or A2
    saturation subdivision would split a whole-area answer into quadrants that get the exact same
    whole-area rows back, recursing pointlessly against the politeness budget (docs/09 A2 review)."""
    last_degraded = False
    for provider in chain:
        ok, reason = provider.available()
        if not ok:
            warns.append(f"{provider.name}: {reason}")
            continue
        tiled_honoured = pq.tile is not None and getattr(provider, "supports_tiles", False)
        if pq.tile is not None and not tiled_honoured:
            # say it out loud: this provider searches the query TEXT only, so a tiled plan silently
            # loses its per-cell geographic constraint (and with it the point of tiling)
            msg = f"{provider.name} ignores grid tiles — geo constraint dropped for tiled queries"
            if msg not in warns:
                warns.append(msg)
        try:
            return provider.fetch(pq, limit=limit), "done", tiled_honoured
        except ProviderDegraded as e:
            LOG.warning("provider %s degraded: %s", provider.name, e)
            last_degraded = True
            continue  # try next provider in the chain
        except ProviderFailed as e:
            warns.append(f"{provider.name} failed: {e}")
            continue
    return [], "degraded" if last_degraded else "failed", False


def _plan_into_db(conn: sqlite3.Connection, cfg: Config, icp: ICP, run_id: str) -> None:

    queries = build_plan(icp, cfg)
    # a register query carries {"provider": name} in tile_json (no bbox) so run_discover routes it to
    # that provider alone; _tile_from_json returns None for it
    db.add_queries(conn, run_id, [
        (q.text, q.tile.to_json() if q.tile else ({"provider": q.provider} if q.provider else None))
        for q in queries])


def _tile_from_json(tile_json: str | None):
    if not tile_json:
        return None
    from leadforge.grid import Tile

    d = json.loads(tile_json)
    if not isinstance(d, dict) or "bbox" not in d:
        return None  # a provider marker, not a tile
    return Tile.from_json(d)


def _provider_from_json(tile_json: str | None) -> str | None:
    if not tile_json:
        return None
    d = json.loads(tile_json)
    return d.get("provider") if isinstance(d, dict) else None


# --------------------------------------------------------------------------- run (state machine)
def run_pipeline(cfg: Config, icp: ICP, icp_path: Path, resume: bool = False,
                 limit: int | None = None, skip_dm: bool = False) -> dict:
    ensure_ready(cfg)
    conn = db.connect(cfg.db_path)
    _progress_ui(cfg)
    run = _latest_run(conn, icp) if resume else None
    stage = run["stage"] if run else "planned"
    run_id = run["id"] if run else None
    warns: list[str] = []
    artifacts: list[str] = []

    # A3 (docs/09): a run can reach ANY later stage (even 'exported') while discovery queries are
    # still 'pending' or 'degraded' — a saturation-subdivision child inserted after the parent's
    # stage flipped to 'discovered', or a captcha/timeout tile never retried before the DM gate. On
    # resume, unfinished discovery always wins over wherever the run happened to stop: re-entering
    # discovery moves the stage back through discovered -> enrich -> ... so late arrivals still get
    # enriched, scored and exported like any other business (the live run: 'exported' with 18 pending).
    if run_id is not None and conn.execute(
            "SELECT 1 FROM queries WHERE run_id=? AND status IN ('pending','degraded') LIMIT 1",
            (run_id,)).fetchone():
        # item 1 (docs/09): a run that stopped exactly at caps.max_leads must STAY stopped across
        # --resume (pending/degraded queries notwithstanding) — unless the cap has since been raised
        # past what this run has already credited, in which case there is room to keep discovering.
        stats = json.loads(run["stats_json"]) if run else {}
        credited = conn.execute(
            "SELECT COUNT(*) c FROM businesses WHERE first_run_id=?", (run_id,)).fetchone()["c"]
        if not (stats.get("cap_reached") and credited >= icp.caps.max_leads):
            stage = "discovering"

    # DISCOVER
    if stage in ("planned", "discovering") or run_id is None:
        run_id, dcounts, dwarn = run_discover(cfg, icp, icp_path, limit=limit, run_id=run_id)
        warns += dwarn
        stage = _latest_run(conn, icp)["stage"]
        if getattr(icp.target, "mode", "local_business") == "company":
            from leadforge.enrich.resolve_domain import run_resolve
            run_resolve(conn, cfg, limit or icp.caps.max_sites)

    conn = db.connect(cfg.db_path)  # refresh connection view

    # ENRICH ('enriching' = a previous run died mid-enrich; the stage is idempotent —
    # businesses_for_enrich skips already-crawled rows — so resume just re-enters it)
    if stage in ("discovered", "enriching"):
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
        scounts = score_run(conn, icp, run_id, cfg=cfg)
        db.set_stage(conn, run_id, "scored", **scounts)
        stage = "scored"

    # EXPORT
    if stage == "scored":
        from leadforge.export import export_run, summarize_for_digest, top_hooks
        artifacts = export_run(conn, icp, run_id, cfg.exports_dir, cfg.export.formats,
                               staleness_days=cfg.validation.staleness_days, cfg=cfg)
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
