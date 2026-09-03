"""`leadforge draft` sub-app (v0.3 unit F, docs/09 Wave 2 F, ADR-012; v0.4 unit B). Registered by
leadforge.cli.

export -> agent writes subject+observation -> apply (gated, never trusts the model) -> render for
human review; check runs the gate standalone. No LLM API key anywhere in this package: the CLI hands
the agent (already in-harness) a compact evidence packet and reads back its two written slots.

The actual export/apply logic lives in `draft.service` (v0.4): these commands are thin wrappers —
CLI arg handling, file I/O, digest/echo — over the same pure functions `draft.service.auto_draft`
drives headlessly from `pipeline.py`. Behaviour and digests are unchanged from v0.3.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from leadforge import db
from leadforge.config import Config
from leadforge.draft import service
from leadforge.draft.gate import check_draft
from leadforge.draft.skeletons import PURPOSES
from leadforge.util import LeadForgeError, emit_digest

draft_app = typer.Typer(help="Agent drafting loop: export packets -> agent writes -> apply (gated) -> render.")


def _cfg(ctx: typer.Context) -> Config:
    cfg = ctx.obj["cfg"]
    if cfg is None:  # mirrors leadforge.cli._cfg: a broken leadforge.yaml must not brick this sub-app
        emit_digest(False, "config-load", warnings=[ctx.obj.get("cfg_error", "leadforge.yaml invalid")],
                    next_="leadforge config set <key> <value> to repair, or fix/delete leadforge.yaml")
        raise typer.Exit(2)
    return cfg


def _say(ctx: typer.Context, *lines: str) -> None:
    if not ctx.obj.get("json_only"):
        for ln in lines:
            typer.echo(ln)


@draft_app.command("export")
def export(
    ctx: typer.Context,
    campaign: str = typer.Option(..., "--campaign"),
    purpose: str = typer.Option(..., "--purpose"),
    icp: str = typer.Option("icp.yaml", "--icp"),
    run: str | None = typer.Option(None, "--run", help="score businesses without outreach_targets rows yet"),
    tier: str | None = typer.Option(None, "--tier", help="with --run: comma-separated tiers, default A,B"),
    max_: int = typer.Option(40, "--max"),
    out: str | None = typer.Option(None, "--out"),
    redo: bool = typer.Option(False, "--redo", help="also re-export targets already in state 'drafted'"),
) -> None:
    """Write one evidence packet per enrolled target (<= cfg.draft.packet_max_tokens each) for the
    agent to draft a subject + observation from."""
    from leadforge.intake import load_icp

    cfg = _cfg(ctx)
    if purpose not in PURPOSES:
        emit_digest(False, "draft export", warnings=[f"unknown --purpose '{purpose}' (one of {', '.join(PURPOSES)})"])
        raise typer.Exit(2)
    try:
        icp_obj = load_icp(Path(icp))
    except LeadForgeError as e:
        emit_digest(False, "draft export", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e

    conn = db.connect(cfg.db_path)
    if run:
        tiers = {t.strip().upper() for t in (tier or "A,B").split(",") if t.strip()}
        target_ids = service.ensure_targets(conn, run, tiers, campaign)
    else:
        states = ("enrolled", "drafted") if redo else ("enrolled",)
        q = ",".join("?" * len(states))
        rows = conn.execute(
            f"SELECT id FROM outreach_targets WHERE campaign=? AND state IN ({q}) ORDER BY id",
            (campaign, *states),
        ).fetchall()
        target_ids = [r["id"] for r in rows]
    if max_:
        target_ids = target_ids[:max_]

    out_path = Path(out) if out else cfg.workspace / "drafts_packets.ndjson"
    lines, counts = service.build_packets(conn, cfg, icp_obj, target_ids, purpose)

    with open(out_path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False) + "\n")

    tok_vals = [ln["tokens_est"] for ln in lines[1:]]
    mean_tok = round(sum(tok_vals) / len(tok_vals), 1) if tok_vals else 0
    _say(ctx, f"packets={counts['targets']} A={counts['grade_a']} B={counts['grade_b']} "
              f"C={counts['grade_c']} insufficient_evidence={counts['insufficient_evidence']} "
              f"mean_tokens={mean_tok} -> {out_path}")
    emit_digest(True, "draft export",
                counts={**counts, "mean_tokens_est": mean_tok, "max_tokens_est": max(tok_vals, default=0)},
                artifacts=[str(out_path.resolve())],
                next_="agent writes subject+observation per line into drafts.ndjson, then: "
                      "leadforge draft apply --in drafts.ndjson")


@draft_app.command("apply")
def apply(
    ctx: typer.Context,
    in_: str = typer.Option("drafts.ndjson", "--in"),
    campaign: str | None = typer.Option(None, "--campaign", help="unused filter placeholder; drafts.ndjson already names targets"),
    packets: str | None = typer.Option(None, "--packets"),
) -> None:
    """Ingest the agent's drafts; every draft passes the mechanical no-fabrication gate or is rejected."""
    cfg = _cfg(ctx)
    in_path = Path(in_)
    packets_path = Path(packets) if packets else cfg.workspace / "drafts_packets.ndjson"
    if not in_path.is_file():
        emit_digest(False, "draft apply", warnings=[f"{in_path} not found"], next_="leadforge draft export ...")
        raise typer.Exit(2)
    if not packets_path.is_file():
        emit_digest(False, "draft apply", warnings=[f"{packets_path} not found"],
                    next_=f"leadforge draft export --out {packets_path}")
        raise typer.Exit(2)

    packet_by_target = service.load_packets(packets_path)
    conn = db.connect(cfg.db_path)
    drafts = []
    for ln in in_path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        drafts.append(json.loads(ln))
    counts = service.apply_drafts(conn, cfg, packet_by_target, drafts, author="agent", campaign=campaign)
    _say(ctx, f"applied={counts['applied']} rejected={counts['rejected']} "
              f"insufficient_evidence={counts['insufficient_evidence']} skipped={counts['skipped']}")
    emit_digest(True, "draft apply", counts=counts, next_="leadforge draft render --campaign <c> --out drafts/")


@draft_app.command("render")
def render(
    ctx: typer.Context,
    campaign: str = typer.Option(..., "--campaign"),
    out: str = typer.Option("drafts", "--out"),
) -> None:
    """Write one reviewable .txt file per drafted message (To/Subject preamble + body)."""
    from leadforge.draft.skeletons import render_txt

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    rows = conn.execute(
        "SELECT m.*, t.business_id FROM messages m JOIN outreach_targets t ON t.id=m.target_id "
        "WHERE t.campaign=? AND m.state='drafted' ORDER BY m.id", (campaign,),
    ).fetchall()
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for m in rows:
        b = conn.execute("SELECT * FROM businesses WHERE id=?", (m["business_id"],)).fetchone()
        contact_row = service.best_contact(conn, b, cfg) if b is not None else None
        to = contact_row["value"] if contact_row is not None else ((b["name"] if b is not None else "") or "")
        txt = render_txt(to=to, subject=m["subject"], body=m["body_text"])
        (out_dir / f"{m['target_id']}_{m['id']}.txt").write_text(txt, encoding="utf-8")
        n += 1
    _say(ctx, f"rendered={n} -> {out_dir}")
    emit_digest(True, "draft render", counts={"rendered": n}, artifacts=[str(out_dir.resolve())], next_=None)


@draft_app.command("check")
def check(
    ctx: typer.Context,
    in_: str | None = typer.Option(None, "--in"),
    packets: str | None = typer.Option(None, "--packets"),
) -> None:
    """Run the gate on a drafts file without storing anything."""
    if not in_:
        emit_digest(False, "draft check", warnings=["--in is required (path to a drafts.ndjson file)"],
                    next_="leadforge draft check --in drafts.ndjson")
        raise typer.Exit(2)
    cfg = _cfg(ctx)
    in_path = Path(in_)
    packets_path = Path(packets) if packets else cfg.workspace / "drafts_packets.ndjson"
    if not in_path.is_file():
        emit_digest(False, "draft check", warnings=[f"{in_path} not found"])
        raise typer.Exit(2)
    if not packets_path.is_file():
        emit_digest(False, "draft check", warnings=[f"{packets_path} not found"])
        raise typer.Exit(2)

    packet_by_target = service.load_packets(packets_path)
    counts = {"checked": 0, "ok": 0, "failed": 0}
    for ln in in_path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        draft = json.loads(ln)
        packet = packet_by_target.get(draft.get("target"))
        if packet is None:
            continue
        counts["checked"] += 1
        result = check_draft(packet, draft)
        counts["ok" if result["ok"] else "failed"] += 1
    _say(ctx, f"checked={counts['checked']} ok={counts['ok']} failed={counts['failed']}")
    emit_digest(counts["failed"] == 0, "draft check", counts=counts, next_=None)
