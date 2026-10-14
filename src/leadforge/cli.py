"""LeadForge CLI (U0.4 digest contract + all command surfaces).

Every command ends with exactly one LF_DIGEST line (docs/06). Human output stays terse; detail -> logfile.
`run` is the resumable orchestrator; individual stage commands exist for control + debugging.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from leadforge import __version__

# `db` pulls in leadforge.models (pydantic classes) at import time — measured ~350ms of the ~1.2s cold
# CLI import (docs/06 perf pass, Layer 1 item 5). Most commands never touch the database (doctor,
# intake, plan, discover, watch, dashboard, render-check, version, config); `db` is imported lazily,
# inside each command that actually needs it, rather than once for every invocation.
from leadforge.config import Config, load_config
from leadforge.util import LeadForgeError, emit_digest, setup_logging

app = typer.Typer(add_completion=False, help="LeadForge — internal B2B lead generation engine.", no_args_is_help=True)
_state: dict = {}


def _cfg(ctx: typer.Context) -> Config:
    cfg = ctx.obj["cfg"]
    if cfg is None:  # leadforge.yaml is invalid — fail with a digest, and keep `config set` usable to repair
        emit_digest(False, "config-load", warnings=[ctx.obj.get("cfg_error", "leadforge.yaml invalid")],
                    next_="leadforge config set <key> <value> to repair, or fix/delete leadforge.yaml")
        raise typer.Exit(2)
    return cfg


@app.callback()
def _root(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="override state dir (default ./leadforge_data)"),
    json_only: bool = typer.Option(False, "--json", help="suppress human output; emit only the digest line"),
    verbose: bool = typer.Option(False, "--verbose", help="debug logging to the logfile"),
):
    # A broken leadforge.yaml must not brick the whole CLI: commands get a clean digest via _cfg(),
    # and `config set` / `version` (which never touch cfg) still run so the workspace can be repaired.
    try:
        cfg = load_config(".", data_dir_override=data_dir)
    except Exception as e:  # noqa: BLE001 — any parse/validation error, reported not stacktraced
        ctx.obj = {"cfg": None, "json_only": json_only,
                   "cfg_error": f"leadforge.yaml invalid: {type(e).__name__}: {str(e)[:100]}"}
        return
    setup_logging(cfg.logs_dir, verbose=verbose)
    ctx.obj = {"cfg": cfg, "json_only": json_only}


def _say(ctx: typer.Context, *lines: str) -> None:
    if not ctx.obj.get("json_only"):
        for ln in lines:
            typer.echo(ln)


# ----------------------------------------------------------------------------- doctor
@app.command()
def doctor(ctx: typer.Context, fix: bool = typer.Option(False, "--fix"), strict: bool = typer.Option(False, "--strict"),
           full: bool = typer.Option(False, "--full", help="with --fix: also install the quality extras ([ner] GLiNER + [browser] crawl4ai)")):
    """Verify and (with --fix) install the runtime environment + pinned scraper binary."""
    from leadforge.doctor import run_doctor

    cfg = _cfg(ctx)
    try:
        rep = run_doctor(cfg, fix=fix, strict=strict, full=full)
    except LeadForgeError as e:
        emit_digest(False, "doctor", warnings=[str(e)[:120]], next_="fix the reported item then re-run")
        raise typer.Exit(e.exit_code) from e
    _say(ctx, *rep.lines())
    emit_digest(rep.ok, "doctor", counts=rep.counts(),
                warnings=[f"{r.name}: {r.hint}" for r in rep.results if not r.ok],
                next_=None if rep.ok else "leadforge doctor --fix")
    raise typer.Exit(0 if rep.ok else 3)


# ----------------------------------------------------------------------------- intake
@app.command()
def intake(
    ctx: typer.Context,
    answers: str = typer.Option("answers.yaml", "--answers"),
    out: str = typer.Option("icp.yaml", "--out"),
):
    """Compile + validate answers.yaml into a canonical icp.yaml."""
    from leadforge.intake import compile_icp

    try:
        icp, warns = compile_icp(Path(answers), Path(out))
    except LeadForgeError as e:
        _say(ctx, str(e))
        # the digest must be self-sufficient: carry the actual problems, not just the header line
        detail = [ln.strip(" -") for ln in str(e).splitlines()[1:] if ln.strip(" -")] or [str(e)]
        emit_digest(False, "intake", counts={"errors": len(detail)},
                    warnings=[d[:120] for d in detail[:5]],
                    next_="ask the user for the missing/invalid fields, fix answers.yaml, re-run")
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"icp.yaml written: campaign={icp.campaign}, {len(icp.target.categories)} categories, "
              f"hash={icp.icp_hash()}")
    emit_digest(True, "intake", counts={"errors": 0, "categories": len(icp.target.categories)},
                warnings=warns, artifacts=[str(Path(out).resolve())], next_="leadforge plan --icp " + out + " --json")


# ----------------------------------------------------------------------------- plan
@app.command()
def plan(ctx: typer.Context, icp: str = typer.Option("icp.yaml", "--icp")):
    """Show the discovery plan (tiles/queries/estimate) without scraping."""
    from leadforge.grid import build_plan, plan_counts
    from leadforge.intake import load_icp

    cfg = _cfg(ctx)
    try:
        icp_obj = load_icp(Path(icp))
        queries = build_plan(icp_obj, cfg)
    except LeadForgeError as e:
        emit_digest(False, "plan", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    counts = plan_counts(queries, cfg)
    hours = counts["est_runtime_min"] / 60
    total_hours = counts["est_total_min"] / 60
    _say(ctx, f"queries={counts['queries']} tiles={counts['tiles']} "
              f"est_max_results={counts['est_max_results']} est_runtime~{hours:.1f}h "
              f"est_enrich_sites={counts['est_enrich_sites']} est_enrich~{counts['est_enrich_min'] / 60:.1f}h "
              f"est_total~{total_hours:.1f}h")
    # a tiled plan can be 60x a text plan; say so in hours before it runs, not after
    warns = []
    if counts["est_total_min"] > 240:
        warns.append(f"large plan: ~{counts['queries']} queries, roughly {hours:.1f}h discovery + "
                     f"{counts['est_enrich_min'] / 60:.1f}h enrichment ~= {total_hours:.1f}h total "
                     f"(resumable — every query and every site checkpoints)")
    if counts["est_max_results"] > icp_obj.caps.max_leads * 10:
        warns.append(f"plan is far larger than caps.max_leads ({icp_obj.caps.max_leads}); discovery "
                     f"stops at the cap, so later tiles may never run — raise the cap or narrow the plan")
    if counts["tiles"]:
        warns.append(f"grid tiling ON: {counts['tiles']} map cells x {len(icp_obj.target.categories)} "
                     f"categories (each cell gets its own ~120-result budget)")
    if counts.get("est_runtime_min_max_subdivided"):
        warns.append(f"saturated tiles subdivide automatically (discovery.subdivide_at); worst case "
                     f"~{counts['est_runtime_min_max_subdivided'] / 60:.0f}h if every tile splits "
                     f"{cfg.discovery.max_subdivisions}x")
    emit_digest(True, "plan", counts=counts, warnings=warns, next_="leadforge run --icp " + icp + " --detach --json")


# ----------------------------------------------------------------------------- discover
@app.command()
def discover(
    ctx: typer.Context,
    icp: str = typer.Option("icp.yaml", "--icp"),
    limit: int = typer.Option(0, "--limit", help="cap businesses processed (smoke tests)"),
    provider: str | None = typer.Option(None, "--provider"),
):
    """Scrape listings, normalize, upsert (resumable per query)."""
    from leadforge.intake import load_icp
    from leadforge.pipeline import run_discover

    cfg = _cfg(ctx)
    try:
        icp_obj = load_icp(Path(icp))
        run_id, counts, warns = run_discover(cfg, icp_obj, Path(icp), limit=limit or None, provider=provider)
    except LeadForgeError as e:
        emit_digest(False, "discover", warnings=[str(e)[:120]], next_="leadforge doctor --fix")
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"businesses={counts.get('businesses', 0)} new={counts.get('new', 0)} "
              f"tiles_degraded={counts.get('tiles_degraded', 0)}")
    emit_digest(True, "discover", run=run_id, counts=counts, warnings=warns, next_="leadforge enrich")


# ----------------------------------------------------------------------------- enrich
@app.command()
def enrich(
    ctx: typer.Context,
    icp: str = typer.Option("icp.yaml", "--icp", help="used for caps.max_sites"),
    limit: int = typer.Option(0, "--limit"),
    stage: str = typer.Option("all", "--stage", help="all|site|registry|validate|backfill|recheck"),
):
    """Crawl business sites, extract + validate contacts, build DM candidates."""
    from leadforge import db
    from leadforge.enrich.runner import run_enrich

    cfg = _cfg(ctx)
    if stage not in ("all", "site", "registry", "validate", "backfill", "recheck"):
        # a typo used to be a silent zero-work success — an agent read that as "nothing left"
        emit_digest(False, "enrich", warnings=[f"unknown --stage '{stage}' (all|site|registry|validate|backfill|recheck)"])
        raise typer.Exit(2)
    from leadforge.util import set_progress_file
    set_progress_file(cfg.data_path / "progress.jsonl")  # standalone enrich feeds `leadforge watch` too
    conn = db.connect(cfg.db_path)
    run = db.latest_run(conn)
    if stage == "backfill":
        from leadforge.enrich.metrics import backfill_legacy_observations

        counts = backfill_legacy_observations(conn)
        _say(ctx, f"backfill inserted={counts['inserted']} contacts={counts['contacts']} "
              f"people={counts['people']} evidence={counts['evidence']}")
        emit_digest(True, "enrich", run=run["id"] if run else None, counts=counts,
                    next_="leadforge enrich --stage site")
        return
    if stage == "recheck":
        due = db.observations_due(conn)
        business_ids = {row["business_id"] for row in due}
        counts = {"observations_due": len(due), "businesses_due": len(business_ids)}
        _say(ctx, f"observations_due={counts['observations_due']} businesses_due={counts['businesses_due']}")
        emit_digest(True, "enrich", run=run["id"] if run else None, counts=counts,
                    next_="leadforge enrich --stage site --limit " + str(len(business_ids)))
        return
    # site budget: explicit --limit wins, else the campaign's cap, else a safe default
    max_sites = limit
    if not max_sites:
        try:
            from leadforge.intake import load_icp

            max_sites = load_icp(Path(icp)).caps.max_sites
        except LeadForgeError:
            max_sites = 300
    try:
        counts = run_enrich(conn, cfg, max_sites, stage=stage)
    except LeadForgeError as e:
        emit_digest(False, "enrich", warnings=[str(e)[:120]], next_="leadforge doctor --fix")
        raise typer.Exit(e.exit_code) from e
    warns = [f"{counts['needs_browser']} sites need a browser pass — pip install -e .[browser]"] if counts.get("needs_browser") else []
    _say(ctx, f"sites={counts['sites_crawled']} contacts={counts['contacts']} dm_candidates={counts['dm_candidates']}")
    emit_digest(True, "enrich", run=run["id"] if run else None, counts=counts, warnings=warns,
                next_="leadforge dm export")


# ----------------------------------------------------------------------------- dm
dm_app = typer.Typer(help="Decision-maker labeling loop.")
app.add_typer(dm_app, name="dm")

# v0.3 sub-apps (ADR-011/012): outreach lifecycle + agent drafting. Each lives in its own module so the
# units that build them never edit this file.
from leadforge.draft.cli import draft_app  # noqa: E402
from leadforge.outreach.cli import outreach_app  # noqa: E402

app.add_typer(outreach_app, name="outreach")
app.add_typer(draft_app, name="draft")

benchmark_app = typer.Typer(help="Hand-checked enrichment and outreach benchmarks.")
app.add_typer(benchmark_app, name="benchmark")

byol_app = typer.Typer(help="Import operator-supplied enrichment lists.")
app.add_typer(byol_app, name="byol")


@byol_app.command("import")
def byol_import(
    ctx: typer.Context,
    input_path: str = typer.Option(..., "--in", help="CSV file supplied by the operator."),
    apply_changes: bool = typer.Option(False, "--apply", help="Write only uniquely matched rows; default is preview."),
):
    """Preview or apply conservative BYOL contact/person matching."""
    from leadforge import db
    from leadforge.enrich.byol import import_csv

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        result = import_csv(conn, Path(input_path), dry_run=not apply_changes)
    except OSError as exc:
        emit_digest(False, "byol-import", warnings=[str(exc)[:160]])
        raise typer.Exit(2) from exc
    _say(ctx, f"rows={result['rows']} matched={result['matched']} imported={result['imported']} "
          f"ambiguous={result['ambiguous']} unmatched={result['unmatched']}")
    emit_digest(True, "byol-import", counts=result,
                next_="leadforge byol import --in contacts.csv --apply" if not apply_changes else "leadforge enrich --stage backfill")


@benchmark_app.command("outcomes")
def benchmark_outcomes(
    ctx: typer.Context,
      input_path: str = typer.Option(..., "--in", help="CSV/TSV/XLSX called-outcomes workbook or sheet."),
    apply_changes: bool = typer.Option(False, "--apply", help="Write matched outcomes; default is preview only."),
):
    """Preview or import called-lead outcomes for source/provider attribution."""
    from leadforge import db
    from leadforge.enrich.benchmark import import_outcomes
    from leadforge.enrich.metrics import outreach_attribution

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        result = import_outcomes(conn, Path(input_path), dry_run=not apply_changes)
    except (OSError, ValueError) as exc:
        emit_digest(False, "benchmark-outcomes", warnings=[str(exc)[:160]])
        raise typer.Exit(2) from exc
    _say(ctx, f"rows={result['rows']} matched={result['matched']} imported={result['imported']} "
          f"duplicates={result.get('duplicates', 0)} ambiguous={result['ambiguous']} unmatched={result['unmatched']}")
    counts = {**result, "attribution": outreach_attribution(conn)}
    emit_digest(True, "benchmark-outcomes", counts=counts,
                next_="leadforge benchmark outcomes --in called.tsv --apply" if not apply_changes else "leadforge export")


@benchmark_app.command("dm-fields")
def benchmark_dm_fields(
    ctx: typer.Context,
    input_path: str = typer.Option(..., "--in", help="CSV/TSV/XLSX with operator-confirmed DM fields."),
    apply_changes: bool = typer.Option(False, "--apply", help="Write matched DM observations; default is preview only."),
):
    """Preview or import person-owned DM phone/email fields for calibration."""
    from leadforge import db
    from leadforge.enrich.benchmark import import_dm_fields

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        result = import_dm_fields(conn, Path(input_path), dry_run=not apply_changes,
                                  default_region=cfg.default_region)
    except (OSError, ValueError) as exc:
        emit_digest(False, "benchmark-dm-fields", warnings=[str(exc)[:160]])
        raise typer.Exit(2) from exc
    _say(ctx, f"rows={result['rows']} matched={result['matched']} imported={result['imported']} "
          f"duplicates={result['duplicates']} ambiguous={result['ambiguous']} unmatched={result['unmatched']}")
    emit_digest(True, "benchmark-dm-fields", counts=result,
                next_="leadforge benchmark dm-fields --in called.xlsx --apply" if not apply_changes else "leadforge export")


@dm_app.command("export")
def dm_export(
    ctx: typer.Context,
    icp: str = typer.Option("icp.yaml", "--icp"),
    max_biz: int = typer.Option(60, "--max"),
    tsv: bool = typer.Option(False, "--tsv"),
    out: str | None = typer.Option(None, "--out"),
):
    """Write a batch of DM candidate snippets for the agent to label."""
    from leadforge import db
    from leadforge.enrich.dm import export_batch
    from leadforge.intake import load_icp

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        icp_obj = load_icp(Path(icp))
    except LeadForgeError as e:
        emit_digest(False, "dm export", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    out_path = Path(out) if out else cfg.workspace / ("dm_batch.tsv" if tsv else "dm_batch.ndjson")
    n, remaining = export_batch(conn, icp_obj, out_path, max_biz=max_biz, tsv=tsv)
    _say(ctx, f"batch={n} remaining={remaining} -> {out_path}")
    emit_digest(True, "dm export", counts={"businesses": n, "remaining": remaining},
                artifacts=[str(out_path.resolve())],
                next_="label the batch then: leadforge dm apply --in dm_labels.ndjson")


@dm_app.command("apply")
def dm_apply(ctx: typer.Context, in_: str = typer.Option("dm_labels.ndjson", "--in")):
    """Ingest the agent's DM labels."""
    from leadforge import db
    from leadforge.enrich.dm import apply_labels

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        counts = apply_labels(conn, Path(in_))
    except LeadForgeError as e:
        emit_digest(False, "dm apply", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"applied={counts['applied']} rejected={counts['rejected']} skipped={counts['skipped']}")
    emit_digest(True, "dm apply", counts=counts, next_="leadforge run --resume  (or leadforge score && leadforge export)")


# ----------------------------------------------------------------------------- score
@app.command()
def score(ctx: typer.Context, icp: str = typer.Option("icp.yaml", "--icp")):
    """Score every business against the ICP rubric."""
    from leadforge import db
    from leadforge.intake import load_icp
    from leadforge.score import score_run

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    run = db.latest_run(conn)
    if not run:
        emit_digest(False, "score", warnings=["no run found; discover first"])
        raise typer.Exit(4)
    try:
        counts = score_run(conn, load_icp(Path(icp)), run["id"], cfg=cfg)
    except LeadForgeError as e:
        emit_digest(False, "score", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"scored={counts['scored']} A={counts['tier_a']} B={counts['tier_b']} C={counts['tier_c']} DQ={counts['dq']}")
    emit_digest(True, "score", run=run["id"], counts=counts, next_="leadforge export")


# ----------------------------------------------------------------------------- export
@app.command()
def export(ctx: typer.Context, icp: str = typer.Option("icp.yaml", "--icp"), out: str | None = typer.Option(None, "--out"),
           format_: str | None = typer.Option(None, "--format", help="comma list, e.g. xlsx,csv (default: config export.formats)")):
    """Write the styled XLSX + CSV + report.json."""
    from leadforge import db
    from leadforge.export import export_run, summarize_for_digest, top_hooks
    from leadforge.intake import load_icp

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    run = db.latest_run(conn)
    if not run:
        emit_digest(False, "export", warnings=["no run found"])
        raise typer.Exit(4)
    out_dir = Path(out) if out else cfg.exports_dir
    formats = [f.strip() for f in format_.split(",") if f.strip()] if format_ else cfg.export.formats
    try:
        artifacts = export_run(conn, load_icp(Path(icp)), run["id"], out_dir, formats,
                               staleness_days=cfg.validation.staleness_days, cfg=cfg)
    except LeadForgeError as e:
        emit_digest(False, "export", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    counts = summarize_for_digest(conn, run["id"])
    db.mark_exported(conn, run["id"], **counts)
    if cfg.export.auto_open:
        from leadforge.util import open_artifact
        xlsx = [a for a in artifacts if a.endswith(".xlsx")]
        if xlsx:
            open_artifact(xlsx[0])
    _say(ctx, f"exported {counts['leads']} leads -> {artifacts[0]}")
    emit_digest(True, "export", run=run["id"], counts=counts, warnings=top_hooks(conn, run["id"]),
                artifacts=artifacts, next_=None)


def _detached_run_cmd(python: str, icp: str, *, limit: int | None, skip_dm: bool,
                      autopilot: bool | None, from_stage: str | None) -> list[str]:
    """The foreground `run --resume` the detached child executes — every run option must be carried
    over (a dropped --from-stage once made the child silently resume the wrong stage)."""
    cmd = [python, "-m", "leadforge", "--json", "run", "--icp", icp, "--resume"]
    if limit:
        cmd += ["--limit", str(limit)]
    if skip_dm:
        cmd.append("--skip-dm")
    if autopilot is not None:
        cmd.append("--autopilot" if autopilot else "--no-autopilot")
    if from_stage is not None:
        cmd += ["--from-stage", from_stage]
    return cmd


def _spawn_detached(cmd: list[str], log_path: Path, cwd: Path) -> int:
    """Start `cmd` as a background process this CLI can walk away from: stdout/stderr to `log_path`,
    detached from this process's console/session so a Ctrl+C or terminal close here does not touch it.
    Returns the child pid. Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (no console window
    inherited, not part of our process group). POSIX: start_new_session=True (its own session, immune
    to our SIGHUP/terminal signals)."""
    import os
    import subprocess

    log_fh = open(log_path, "ab")  # noqa: SIM115 — must outlive this function; the OS owns it after Popen
    kwargs: dict = {}
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=log_fh, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, close_fds=True, **kwargs)
    return proc.pid


# ----------------------------------------------------------------------------- run (orchestrator)
@app.command()
def run(
    ctx: typer.Context,
    icp: str = typer.Option("icp.yaml", "--icp"),
    resume: bool = typer.Option(False, "--resume"),
    limit: int = typer.Option(0, "--limit"),
    skip_dm: bool = typer.Option(False, "--skip-dm"),
    autopilot: bool | None = typer.Option(
        None, "--autopilot/--no-autopilot",
        help="continue past labeling into score -> draft -> export without pausing (default: config pipeline.autopilot)"),
    from_stage: str | None = typer.Option(
        None, "--from-stage",
        help="force the resumed run's stage before continuing (replaces manual DB surgery); requires --resume"),
    detach: bool = typer.Option(
        False, "--detach",
        help="start the run in a background process and return immediately with its run id/pid/log"),
):
    """Orchestrated, resumable pipeline: discover -> enrich -> (autopilot: label -> score -> draft, or
    pause for DM) -> export."""
    from leadforge.intake import load_icp
    from leadforge.pipeline import STAGES

    cfg = _cfg(ctx)
    if from_stage is not None:
        if from_stage not in STAGES:
            emit_digest(False, "run", warnings=[f"unknown --from-stage '{from_stage}' ({'|'.join(STAGES)})"])
            raise typer.Exit(2)
        resume = True  # --from-stage only makes sense against an existing run

    try:
        icp_obj = load_icp(Path(icp))
    except LeadForgeError as e:
        emit_digest(False, "run", warnings=[str(e)[:120]], next_="leadforge doctor --fix")
        raise typer.Exit(e.exit_code) from e

    if detach:
        import sys as _sys

        from leadforge import db
        from leadforge.pipeline import ensure_run

        try:
            run_id = ensure_run(cfg, icp_obj, Path(icp))
        except LeadForgeError as e:
            emit_digest(False, "run", warnings=[str(e)[:120]], next_="leadforge doctor --fix")
            raise typer.Exit(e.exit_code) from e
        log_path = cfg.logs_dir / f"{run_id}.detach.log"
        cmd = _detached_run_cmd(_sys.executable, icp, limit=limit, skip_dm=skip_dm,
                                autopilot=autopilot, from_stage=from_stage)
        pid = _spawn_detached(cmd, log_path, Path(cfg.workspace))
        conn = db.connect(cfg.db_path)
        db.set_run_meta(conn, run_id, pid=pid, log_path=str(log_path.resolve()), detached=True)
        _say(ctx, f"detached: run={run_id} pid={pid} log={log_path}")
        emit_digest(True, "run", run=run_id, counts={"pid": pid, "detached": True},
                    artifacts=[str(log_path.resolve())],
                    next_=f"leadforge status --run {run_id} --json")
        raise typer.Exit(0)

    from leadforge.pipeline import run_pipeline

    try:
        result = run_pipeline(cfg, icp_obj, Path(icp), resume=resume, limit=limit or None, skip_dm=skip_dm,
                              autopilot=autopilot, from_stage=from_stage)
    except LeadForgeError as e:
        emit_digest(False, "run", warnings=[str(e)[:120]], next_="leadforge doctor --fix")
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"stage={result['stage']} " + " ".join(f"{k}={v}" for k, v in result["counts"].items()))
    emit_digest(result["ok"], "run", run=result["run"], counts={"stage": result["stage"], **result["counts"]},
                warnings=result["warnings"], artifacts=result.get("artifacts", []), next_=result["next"])


# ----------------------------------------------------------------------------- status / suppress
def _status_progress(cfg: Config) -> dict:
    """The current stage's live pace, from the same progress.jsonl feed `leadforge watch` tails and the
    enrich stage writes (util.emit_progress). Bounded: reads at most the tail of the feed, one history
    point per (ts, done) change of the run's current stage. Returns {} when there is no feed yet."""
    from leadforge.util import progress_estimate

    feed = cfg.data_path / "progress.jsonl"
    if not feed.is_file():
        return {}
    try:
        lines = feed.read_text(encoding="utf-8").splitlines()[-2000:]
    except OSError:
        return {}
    stage = None
    hist: list[tuple[float, int, int | None]] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("stage") != stage:
            stage, hist = rec.get("stage"), []
        hist.append((rec.get("ts") or 0.0, rec.get("done", 0), rec.get("total")))
    if not hist:
        return {}
    import time as _time

    est = progress_estimate(hist, _time.time())
    out = {"progress_stage": stage, "progress_done": est["done"], "progress_total": est["total"]}
    if est["eta_s"] is not None:
        out["eta_seconds"] = round(est["eta_s"], 1)
    return out


@app.command()
def status(ctx: typer.Context, run_id: str | None = typer.Option(None, "--run")):
    """Show the current/last run snapshot: stage, counts, whether a --detach'd process is still alive,
    live progress and an ETA when the current stage's pace is known."""
    from leadforge import db
    from leadforge.util import pid_alive

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else db.latest_run(conn)
    if not row:
        emit_digest(True, "status", counts={}, next_="leadforge intake")
        return
    stats = json.loads(row["stats_json"])
    nb = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    counts = {"stage": row["stage"], "businesses": nb, **stats}
    pid = stats.get("pid")
    if pid is not None:
        counts["alive"] = pid_alive(pid)
    counts.update(_status_progress(cfg))
    _say(ctx, f"run={row['id']} stage={row['stage']} businesses={nb}"
              + (f" pid={pid} alive={counts.get('alive')}" if pid is not None else ""))
    emit_digest(True, "status", run=row["id"], counts=counts)


@app.command()
def suppress(ctx: typer.Context, action: str = typer.Argument(..., help="add|list"), value: str = typer.Argument(""),
             kind: str | None = typer.Option(None, "--kind", help="domain|email|place_id (default: guessed from the value)")):
    """Manage the opt-out suppression list (domain/email/place_id)."""
    from leadforge import db

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    if action not in ("add", "list"):
        # 'suppress remove x' used to silently print the list and report ok=true
        emit_digest(False, "suppress", warnings=[f"unknown action '{action}' (add|list)"])
        raise typer.Exit(2)
    if action == "add":
        if not value.strip():
            emit_digest(False, "suppress", warnings=["empty value — nothing to suppress"])
            raise typer.Exit(2)
        if kind is not None and kind not in ("domain", "email", "place_id"):
            emit_digest(False, "suppress", warnings=[f"unknown --kind '{kind}' (domain|email|place_id)"])
            raise typer.Exit(2)
        kind = kind or ("email" if "@" in value else "domain")
        db.suppress(conn, kind, value, reason="cli")
        n = conn.execute("SELECT COUNT(*) c FROM suppression").fetchone()["c"]
        _say(ctx, f"suppressed {kind}: {value}")
        emit_digest(True, "suppress", counts={"suppressed": n})
    else:
        rows = conn.execute("SELECT kind,value FROM suppression ORDER BY added_at DESC").fetchall()
        _say(ctx, *[f"{r['kind']}: {r['value']}" for r in rows] or ["(empty)"])
        emit_digest(True, "suppress", counts={"suppressed": len(rows)})


@app.command("config")
def config_cmd(ctx: typer.Context,
               action: str = typer.Argument(..., help="set|get"),
               key: str = typer.Argument(..., help="dotted path, e.g. registry.companies_house_key"),
               value: str = typer.Argument("", help="value (for set)")):
    """Read or write one workspace config value in leadforge.yaml (no manual yaml editing)."""
    import yaml

    path = Path("leadforge.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data = data or {}
    parts = key.split(".")
    if action == "set":
        try:
            parsed = yaml.safe_load(value) if value else value
        except yaml.YAMLError as e:
            emit_digest(False, "config", warnings=[f"value is not parseable YAML: {value[:60]}"], next_=None)
            raise typer.Exit(2) from e
        node = data
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                emit_digest(False, "config", warnings=[f"'{p}' is not a mapping in leadforge.yaml"], next_=None)
                raise typer.Exit(2)
        node[parts[-1]] = parsed
        # validate BEFORE writing: a bad value written first used to brick every command
        # (the root callback loads leadforge.yaml), including the repair `config set` itself
        merged = yaml.safe_dump(data, sort_keys=False)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "leadforge.yaml").write_text(merged, encoding="utf-8")
            try:
                load_config(td)
            except Exception as e:  # noqa: BLE001 — report, don't stacktrace
                emit_digest(False, "config", warnings=[f"invalid value for {key}: {type(e).__name__}"], next_=None)
                raise typer.Exit(2) from e
        path.write_text(merged, encoding="utf-8")
        _say(ctx, f"{key} = {value!r} written to {path}")
        emit_digest(True, "config", counts={"set": 1}, next_=None)
    elif action == "get":
        node = data
        for p in parts:
            node = node.get(p, {}) if isinstance(node, dict) else {}
        _say(ctx, f"{key} = {node!r}")
        emit_digest(True, "config", counts={}, warnings=[], next_=None)
    else:
        emit_digest(False, "config", warnings=[f"unknown action '{action}' (set|get)"], next_=None)
        raise typer.Exit(2)


@app.command()
def watch(ctx: typer.Context):
    """Live progress bar for the run happening in this workspace (tail of the progress feed)."""
    import time as _time

    from leadforge.util import _PROG, progress_summary, render_progress_line

    cfg = _cfg(ctx)
    import os as _os
    if _os.name == "nt":
        _os.system("")  # enables VT escape processing on legacy conhost (documented side effect)
    # no side effects here: cfg.data_path would mkdir leadforge_data/ in whatever folder this was run from
    data_dir = Path(cfg.workspace) / cfg.data_dir
    feed = data_dir / "progress.jsonl"
    if not (data_dir / "db.sqlite3").is_file() and not feed.is_file():
        # a human in the wrong folder used to see one line and then silence for 15 minutes
        hint = (f"no LeadForge workspace in {Path(cfg.workspace).resolve()} — run `leadforge watch` inside the "
                f"campaign folder, or pass --data-dir <path-to-leadforge_data>")
        typer.echo(hint, err=True)
        emit_digest(False, "watch", warnings=[hint[:120]], next_="cd <campaign workspace> && leadforge watch")
        raise typer.Exit(4)
    typer.echo(f"watching {feed.resolve()} — Ctrl+C to close (the run itself is unaffected)", err=True)
    if not feed.is_file():
        typer.echo("no run in progress yet in this workspace — waiting for `leadforge run` to start one", err=True)
    pos = 0
    idle = 0.0
    last = None
    summarized = False
    try:
        while True:
            if feed.is_file():
                if feed.stat().st_size < pos:
                    pos = 0  # feed truncated = a new run started in this workspace — start over
                    summarized = False
                with open(feed, encoding="utf-8") as fh:
                    fh.seek(pos)
                    lines = fh.readlines()
                    pos = fh.tell()
                if lines:
                    idle = 0.0
                    # replay the WHOLE backlog (not a tail) so the pace/ETA come from the feed's own clock.
                    for ln in lines:
                        try:
                            last = json.loads(ln)
                            render_progress_line(last["stage"], last["done"], last["total"], last.get("msg", ""),
                                                 ts=last["ts"])
                        except (ValueError, KeyError):
                            continue
                    if not summarized and last is not None:
                        import time as _t
                        typer.echo("\n" + progress_summary(last["stage"], list(_PROG["hist"]), _t.time()), err=True)
                        summarized = True
                else:
                    idle += 0.5
            else:
                idle += 0.5  # no feed at all still counts toward the 15m timeout
            _time.sleep(0.5)
            if idle > 900:  # 15 min of silence — run is over or long gone
                typer.echo("\nno progress for 15m — closing. (leadforge status --json for state)", err=True)
                break
    except KeyboardInterrupt:
        pass
    emit_digest(True, "watch", counts={}, warnings=[], next_=None)


@app.command()
def dashboard(ctx: typer.Context, port: int = typer.Option(8765, "--port"),
              open_: bool = typer.Option(False, "--open", help="open the page in the default browser")):
    """Local read-only status page for this workspace: machine stages with measured pace + ETA, the
    human/agent stages with their item counts, and every count behind them (http://127.0.0.1:<port>/)."""
    from leadforge.dashboard import serve

    cfg = _cfg(ctx)
    data_dir = Path(cfg.workspace) / cfg.data_dir
    if not (data_dir / "db.sqlite3").is_file():
        hint = (f"no LeadForge workspace in {Path(cfg.workspace).resolve()} — run `leadforge dashboard` inside the "
                f"campaign folder, or pass --data-dir <path-to-leadforge_data>")
        emit_digest(False, "dashboard", warnings=[hint[:120]], next_="cd <campaign workspace> && leadforge dashboard")
        raise typer.Exit(4)
    typer.echo(f"dashboard: http://127.0.0.1:{port}/  (read-only; Ctrl+C to stop — the run is unaffected)", err=True)
    serve(data_dir, port=port, open_browser=open_)
    emit_digest(True, "dashboard", counts={}, warnings=[], next_=None)


@app.command("prune-tiles")
def prune_tiles(ctx: typer.Context,
                min_new: int = typer.Option(3, "--min-new", help="skip pending children whose parent tile added fewer NEW businesses than this"),
                all_children: bool = typer.Option(False, "--all-children", help="skip EVERY pending child tile (parents recorded without new_count)"),
                dry_run: bool = typer.Option(False, "--dry-run", help="count only, change nothing")):
    """Drop queued subdivision children that cannot pay for themselves: a saturated tile whose results
    were all already known spawns children that come back saturated-and-known too (measured live
    2026-09-03: ~0.6 new businesses per 15 s child). Marks them 'skipped'; `run --resume` then treats
    discovery as complete. Safe to re-run; never touches done, degraded or depth-0 tiles."""
    from leadforge import db

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    run = db.latest_run(conn)
    if not run:
        emit_digest(False, "prune-tiles", warnings=["no run found"])
        raise typer.Exit(4)
    counts = db.prune_child_tiles(conn, run["id"], min_new, all_children=all_children, dry_run=dry_run)
    _say(ctx, f"{'would skip' if dry_run else 'skipped'} {counts['skipped']} of {counts['children']} pending child tiles "
              f"(kept: {counts['kept_parent_yielded']} whose parent yielded, {counts['kept_parent_unknown']} with an unknown parent)")
    emit_digest(True, "prune-tiles", run=run["id"], counts={**counts, "dry_run": dry_run},
                next_=None if dry_run or not counts["skipped"] else "leadforge run --resume")


@app.command()
def dedupe(ctx: typer.Context, dry_run: bool = typer.Option(False, "--dry-run", help="count only, change nothing")):
    """Fold register rows (DVSA, Companies House) into the Maps row that carries the same phone and looks
    like the same business. Chains sharing a switchboard are never merged. Safe to re-run."""
    from leadforge import db

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    counts = db.merge_phone_duplicates(conn, dry_run=dry_run)
    _say(ctx, f"{counts['groups']} shared-phone groups; {'would merge' if dry_run else 'merged'} {counts['merged']} "
              f"register rows into their Maps twin; {counts['skipped_unrelated']} left alone (different business)")
    emit_digest(True, "dedupe", counts=counts, warnings=[], next_=None if dry_run else "leadforge export --icp icp.yaml")


@app.command("render-check")
def render_check(ctx: typer.Context, url: str = typer.Argument(..., help="one site URL to diagnose"),
                 force: bool = typer.Option(False, "--force", help="render even if the plain client succeeded")):
    """Diagnose the browser fallback on ONE url: robots -> plain fetch -> rendered fetch -> contacts.

    Makes the bot-wall escalation observable without running a whole campaign. Honors robots exactly
    like the crawler: a disallowed URL is never fetched or rendered.
    """
    from leadforge.enrich import browser
    from leadforge.enrich.crawler import SiteCrawler
    from leadforge.enrich.extract import extract_emails, extract_people, extract_phones
    from leadforge.util import HostThrottle

    cfg = _cfg(ctx)
    throttle = HostThrottle(cfg.politeness.delay_s)
    crawler = SiteCrawler(cfg, throttle)
    counts: dict = {"static_ok": False, "needs_browser": False, "rendered": False,
                    "emails": 0, "phones": 0, "people": 0}
    warns: list[str] = []
    try:
        if not crawler._allowed(url):
            emit_digest(False, "render-check", counts=counts,
                        warnings=[f"robots.txt disallows {url} — not fetched, not rendered"],
                        next_="nothing to do: the site opted out")
            raise typer.Exit(0)
        res = crawler.crawl(url)
        counts["static_ok"] = res.ok
        counts["needs_browser"] = res.needs_browser
        if res.error:
            warns.append(f"static: {res.error}")
        html = ""
        if res.ok and not force:
            html = res.pages[0].html if res.pages else ""
        elif not browser.is_available():
            warns.append("browser extra not installed — pip install -e .[browser] && crawl4ai-setup")
        elif res.needs_browser or force:
            html = browser.fetch_rendered(url, cfg, throttle)
            counts["rendered"] = bool(html)
            if not html:
                warns.append("render returned nothing (blocked, failed, or 4xx+)")
        if html:
            text = SiteCrawler.extract_text(html)
            region = cfg.default_region
            counts["emails"] = len(extract_emails(html, text))
            counts["phones"] = len(extract_phones(html, text, region))
            counts["people"] = len(extract_people(text, url))
            counts["bytes"] = len(html)
    finally:
        crawler.close()
    _say(ctx, f"static_ok={counts['static_ok']} needs_browser={counts['needs_browser']} "
              f"rendered={counts['rendered']} emails={counts['emails']} phones={counts['phones']}")
    emit_digest(True, "render-check", counts=counts, warnings=warns, next_=None)


@app.command()
def version():
    """Print version."""
    typer.echo(f"leadforge {__version__}")
    emit_digest(True, "version", counts={}, warnings=[], next_=None)


_ROOT_FLAGS = ("--json", "--verbose")


def _hoist_root_flags(argv: list[str]) -> list[str]:
    """`--json` / `--verbose` are root options, but every skill example (and every agent) writes them
    after the subcommand — `leadforge plan --icp x --json` — which Typer rejects. Hoist them to the
    front so both spellings work. Values after `--` are left alone."""
    if "--" in argv:
        head, tail = argv[: argv.index("--")], argv[argv.index("--"):]
    else:
        head, tail = argv, []
    hoisted = [a for a in head if a in _ROOT_FLAGS]
    rest = [a for a in head if a not in _ROOT_FLAGS]
    return hoisted + rest + tail


def main() -> None:
    # Digest lines must be UTF-8 regardless of platform locale (Windows pipes default to cp1252).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.argv[1:] = _hoist_root_flags(sys.argv[1:])
    try:
        app()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:  # noqa: BLE001 — the digest contract holds even on unexpected crashes
        emit_digest(False, "error", warnings=[f"{type(e).__name__}: {str(e)[:120]}"],
                    next_="see the logfile; leadforge doctor if environment-related")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
