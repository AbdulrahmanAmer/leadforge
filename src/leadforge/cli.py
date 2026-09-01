"""LeadForge CLI (U0.4 digest contract + all command surfaces).

Every command ends with exactly one LF_DIGEST line (docs/06). Human output stays terse; detail -> logfile.
`run` is the resumable orchestrator; individual stage commands exist for control + debugging.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from leadforge import __version__, db
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
                warnings=warns, artifacts=[str(Path(out).resolve())], next_="leadforge run --icp " + out)


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
    _say(ctx, f"queries={counts['queries']} tiles={counts['tiles']} "
              f"est_max_results={counts['est_max_results']} est_runtime~{hours:.1f}h")
    # a tiled plan can be 60x a text plan; say so in hours before it runs, not after
    warns = []
    if counts["est_runtime_min"] > 240:
        warns.append(f"large plan: ~{counts['queries']} queries, roughly {hours:.1f}h of scraping "
                     f"(resumable — every query checkpoints)")
    if counts["est_max_results"] > icp_obj.caps.max_leads * 10:
        warns.append(f"plan is far larger than caps.max_leads ({icp_obj.caps.max_leads}); discovery "
                     f"stops at the cap, so later tiles may never run — raise the cap or narrow the plan")
    if counts["tiles"]:
        warns.append(f"grid tiling ON: {counts['tiles']} map cells x {len(icp_obj.target.categories)} "
                     f"categories (each cell gets its own ~120-result budget)")
    emit_digest(True, "plan", counts=counts, warnings=warns, next_="leadforge run --icp " + icp)


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
    stage: str = typer.Option("all", "--stage", help="all|site|registry|validate"),
):
    """Crawl business sites, extract + validate contacts, build DM candidates."""
    from leadforge.enrich.runner import run_enrich

    cfg = _cfg(ctx)
    if stage not in ("all", "site", "registry", "validate"):
        # a typo used to be a silent zero-work success — an agent read that as "nothing left"
        emit_digest(False, "enrich", warnings=[f"unknown --stage '{stage}' (all|site|registry|validate)"])
        raise typer.Exit(2)
    from leadforge.util import set_progress_file
    set_progress_file(cfg.data_path / "progress.jsonl")  # standalone enrich feeds `leadforge watch` too
    conn = db.connect(cfg.db_path)
    run = db.latest_run(conn)
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


@dm_app.command("export")
def dm_export(
    ctx: typer.Context,
    icp: str = typer.Option("icp.yaml", "--icp"),
    max_biz: int = typer.Option(60, "--max"),
    tsv: bool = typer.Option(False, "--tsv"),
    out: str | None = typer.Option(None, "--out"),
):
    """Write a batch of DM candidate snippets for the agent to label."""
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
    from leadforge.intake import load_icp
    from leadforge.score import score_run

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    run = db.latest_run(conn)
    if not run:
        emit_digest(False, "score", warnings=["no run found; discover first"])
        raise typer.Exit(4)
    try:
        counts = score_run(conn, load_icp(Path(icp)), run["id"])
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
                               staleness_days=cfg.validation.staleness_days)
    except LeadForgeError as e:
        emit_digest(False, "export", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    counts = summarize_for_digest(conn, run["id"])
    db.set_stage(conn, run["id"], "exported", **counts)
    if cfg.export.auto_open:
        from leadforge.util import open_artifact
        xlsx = [a for a in artifacts if a.endswith(".xlsx")]
        if xlsx:
            open_artifact(xlsx[0])
    _say(ctx, f"exported {counts['leads']} leads -> {artifacts[0]}")
    emit_digest(True, "export", run=run["id"], counts=counts, warnings=top_hooks(conn, run["id"]),
                artifacts=artifacts, next_=None)


# ----------------------------------------------------------------------------- run (orchestrator)
@app.command()
def run(
    ctx: typer.Context,
    icp: str = typer.Option("icp.yaml", "--icp"),
    resume: bool = typer.Option(False, "--resume"),
    limit: int = typer.Option(0, "--limit"),
    skip_dm: bool = typer.Option(False, "--skip-dm"),
):
    """Orchestrated, resumable pipeline: discover -> enrich -> (pause for DM) -> score -> export."""
    from leadforge.intake import load_icp
    from leadforge.pipeline import run_pipeline

    cfg = _cfg(ctx)
    try:
        icp_obj = load_icp(Path(icp))
        result = run_pipeline(cfg, icp_obj, Path(icp), resume=resume, limit=limit or None, skip_dm=skip_dm)
    except LeadForgeError as e:
        emit_digest(False, "run", warnings=[str(e)[:120]], next_="leadforge doctor --fix")
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"stage={result['stage']} " + " ".join(f"{k}={v}" for k, v in result["counts"].items()))
    emit_digest(result["ok"], "run", run=result["run"], counts={"stage": result["stage"], **result["counts"]},
                warnings=result["warnings"], artifacts=result.get("artifacts", []), next_=result["next"])


# ----------------------------------------------------------------------------- status / suppress
@app.command()
def status(ctx: typer.Context, run_id: str | None = typer.Option(None, "--run")):
    """Show the current/last run snapshot."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else db.latest_run(conn)
    if not row:
        emit_digest(True, "status", counts={}, next_="leadforge intake")
        return
    stats = json.loads(row["stats_json"])
    nb = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    _say(ctx, f"run={row['id']} stage={row['stage']} businesses={nb}")
    emit_digest(True, "status", run=row["id"], counts={"stage": row["stage"], "businesses": nb, **stats})


@app.command()
def suppress(ctx: typer.Context, action: str = typer.Argument(..., help="add|list"), value: str = typer.Argument(""),
             kind: str | None = typer.Option(None, "--kind", help="domain|email|place_id (default: guessed from the value)")):
    """Manage the opt-out suppression list (domain/email/place_id)."""
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

    from leadforge.util import render_progress_line

    cfg = _cfg(ctx)
    import os as _os
    if _os.name == "nt":
        _os.system("")  # enables VT escape processing on legacy conhost (documented side effect)
    feed = cfg.data_path / "progress.jsonl"
    typer.echo(f"watching {feed} — Ctrl+C to close (the run itself is unaffected)", err=True)
    pos = 0
    idle = 0.0
    last = None
    try:
        while True:
            if feed.is_file():
                if feed.stat().st_size < pos:
                    pos = 0  # feed truncated = a new run started in this workspace — start over
                with open(feed, encoding="utf-8") as fh:
                    fh.seek(pos)
                    lines = fh.readlines()
                    pos = fh.tell()
                if lines:
                    idle = 0.0
                    for ln in lines[-40:]:
                        try:
                            last = json.loads(ln)
                            render_progress_line(last["stage"], last["done"], last["total"], last.get("msg", ""))
                        except (ValueError, KeyError):
                            continue
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
