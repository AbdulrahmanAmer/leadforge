"""`leadforge outreach` sub-app (v0.3 unit E). Registered by leadforge.cli (that registration line is
not this unit's to touch); every command here ends with exactly one LF_DIGEST line (docs/06), same
contract as the root CLI.

Dry-run is the send default everywhere — `--live` always needs both `outreach.armed: true` in
leadforge.yaml AND a matching `--i-am <approver>` per message, and both checks run before anything
else so a bare `outreach send --live` in an unconfigured workspace fails closed with one honest
digest line, never a stack trace.
"""

from __future__ import annotations

from pathlib import Path

import typer

from leadforge import db
from leadforge.config import Config
from leadforge.outreach import approve as approve_mod
from leadforge.outreach import doctor as doctor_mod
from leadforge.outreach import identity as identity_mod
from leadforge.outreach import plan as plan_mod
from leadforge.outreach import send as send_mod
from leadforge.outreach import status as status_mod
from leadforge.outreach import sync as sync_mod
from leadforge.util import LeadForgeError, emit_digest

outreach_app = typer.Typer(help="Outreach lifecycle: plan -> draft -> approve -> send (dry-run default) -> sync -> status.")
identity_app = typer.Typer(help="Sending identities (from name/email, postal address, opt-out).")
mailbox_app = typer.Typer(help="Mailboxes that send on an identity's behalf.")
outcome_app = typer.Typer(help="Post-touch outcome tracking (phone/email results).")
outreach_app.add_typer(identity_app, name="identity")
outreach_app.add_typer(mailbox_app, name="mailbox")
outreach_app.add_typer(outcome_app, name="outcome")


def _cfg(ctx: typer.Context) -> Config:
    cfg = ctx.obj["cfg"] if ctx.obj else None
    if cfg is None:
        emit_digest(False, "config-load", warnings=[(ctx.obj or {}).get("cfg_error", "leadforge.yaml invalid")],
                    next_="leadforge config set <key> <value> to repair, or fix/delete leadforge.yaml")
        raise typer.Exit(2)
    return cfg


def _say(ctx: typer.Context, *lines: str) -> None:
    if not (ctx.obj or {}).get("json_only"):
        for ln in lines:
            typer.echo(ln)


# ----------------------------------------------------------------------------- identity
@identity_app.command("add")
def identity_add(
    ctx: typer.Context,
    label: str = typer.Option(..., "--label"),
    from_email: str = typer.Option(..., "--from-email"),
    from_name: str = typer.Option("", "--from-name"),
    reply_to: str = typer.Option("", "--reply-to"),
    postal_address: str = typer.Option("", "--postal-address"),
    privacy_url: str = typer.Option("", "--privacy-url"),
    unsubscribe_mailto: str = typer.Option("", "--unsubscribe-mailto"),
    unsubscribe_url: str = typer.Option("", "--unsubscribe-url"),
    client: str = typer.Option("", "--client"),
    owner_entity: str = typer.Option("gainlev", "--owner-entity"),
):
    """Register a sending identity. Requires --from-email; live-complete needs from_name, postal
    address, privacy URL and at least one unsubscribe channel."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        ident_id = identity_mod.add_identity(
            conn, label=label, from_email=from_email, from_name=from_name, reply_to=reply_to,
            postal_address=postal_address, privacy_url=privacy_url, unsubscribe_mailto=unsubscribe_mailto,
            unsubscribe_url=unsubscribe_url, client_id=client, owner_entity=owner_entity,
        )
    except LeadForgeError as e:
        emit_digest(False, "outreach identity add", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    row = identity_mod.get_identity(conn, label)
    complete = identity_mod.is_identity_complete(row)
    _say(ctx, f"identity '{label}' added (id={ident_id}); live_complete={complete}")
    emit_digest(True, "outreach identity add", counts={"id": ident_id, "live_complete": int(complete)},
                next_=f"leadforge outreach mailbox add --identity {label} --address <you@yourdomain>")


@identity_app.command("list")
def identity_list(ctx: typer.Context):
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    rows = identity_mod.list_identities(conn)
    for r in rows:
        _say(ctx, f"{r['label']}: {r['from_email']} live_complete={identity_mod.is_identity_complete(r)}")
    emit_digest(True, "outreach identity list", counts={"identities": len(rows)})


# ----------------------------------------------------------------------------- mailbox
@mailbox_app.command("add")
def mailbox_add(
    ctx: typer.Context,
    identity: str = typer.Option(..., "--identity"),
    address: str = typer.Option(..., "--address"),
    transport: str = typer.Option("file", "--transport"),
    config: list[str] = typer.Option(  # noqa: B008 — typer's own list-option pattern, not a shared mutable default
        None, "--config", help="key=ENV_VAR_NAME, repeatable — values are env var NAMES, never secrets"
    ),
    daily_cap: int = typer.Option(30, "--daily-cap"),
):
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    cfg_dict: dict[str, str] = {}
    for kv in config or []:
        if "=" not in kv:
            emit_digest(False, "outreach mailbox add", warnings=[f"--config expects key=ENV_VAR_NAME, got '{kv}'"])
            raise typer.Exit(4)
        k, v = kv.split("=", 1)
        cfg_dict[k.strip()] = v.strip()
    try:
        mb_id = identity_mod.add_mailbox(conn, identity_label=identity, address=address, transport=transport,
                                         config=cfg_dict, daily_cap=daily_cap)
    except LeadForgeError as e:
        emit_digest(False, "outreach mailbox add", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"mailbox '{address}' added (id={mb_id}) for identity '{identity}'")
    emit_digest(True, "outreach mailbox add", counts={"id": mb_id})


@mailbox_app.command("list")
def mailbox_list(ctx: typer.Context, identity: str | None = typer.Option(None, "--identity")):
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    rows = identity_mod.list_mailboxes(conn, identity)
    for r in rows:
        _say(ctx, f"{r['address']} transport={r['transport']} status={r['status']} cap={r['daily_cap']}"
                  + (f" paused: {r['paused_reason']}" if r["status"] == "paused" else ""))
    emit_digest(True, "outreach mailbox list", counts={"mailboxes": len(rows)})


# ----------------------------------------------------------------------------- plan
@outreach_app.command("plan")
def plan(
    ctx: typer.Context,
    campaign: str = typer.Option(..., "--campaign"),
    icp: str = typer.Option("icp.yaml", "--icp"),
    run: str | None = typer.Option(None, "--run"),
    tier: str = typer.Option(..., "--tier", help="comma list, e.g. A,B"),
    identity: str = typer.Option(..., "--identity"),
    limit: int | None = typer.Option(None, "--limit"),
    client: str = typer.Option("", "--client"),
):
    """Enrol scored leads of a run as outreach targets (every exclusion counted by reason)."""
    from leadforge.intake import load_icp

    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        icp_obj = load_icp(Path(icp))
    except LeadForgeError as e:
        emit_digest(False, "outreach plan", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    run_row = conn.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone() if run else db.latest_run(conn)
    if run_row is None:
        emit_digest(False, "outreach plan", warnings=["no run found — run `leadforge run` first"],
                    next_="leadforge run --icp " + icp)
        raise typer.Exit(4)
    tiers = [t.strip().upper() for t in tier.split(",") if t.strip()]
    try:
        result = plan_mod.plan_targets(conn, cfg, icp_obj, campaign=campaign, run_id=run_row["id"], tiers=tiers,
                                       identity_label=identity, limit=limit, client_id=client)
    except LeadForgeError as e:
        emit_digest(False, "outreach plan", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    counts = result["counts"]
    _say(ctx, "enrolled=" + str(counts["enrolled"]),
              "excluded: " + ", ".join(f"{k}={counts[k]}" for k in plan_mod.EXCLUSION_REASONS))
    emit_digest(True, "outreach plan", run=run_row["id"], counts=counts,
                next_=f"leadforge draft export --campaign {campaign}")


# ----------------------------------------------------------------------------- approve
@outreach_app.command("approve")
def approve(
    ctx: typer.Context,
    campaign: str = typer.Option(..., "--campaign"),
    approver: str = typer.Option(..., "--approver"),
    tier: str | None = typer.Option(None, "--tier"),
    ids: str | None = typer.Option(None, "--ids", help="comma-separated message ids"),
    all_drafted: bool = typer.Option(False, "--all-drafted"),
):
    """Approve drafted messages; approval is bound to the message's current content hash."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    id_list = None
    if ids:
        try:
            id_list = [int(x.strip()) for x in ids.split(",") if x.strip()]
        except ValueError:
            emit_digest(False, "outreach approve", warnings=[f"--ids must be a comma list of integers, got '{ids}'"])
            raise typer.Exit(4) from None
    try:
        result = approve_mod.approve_messages(conn, campaign=campaign, approver=approver, tier=tier, ids=id_list,
                                              all_drafted=all_drafted)
    except LeadForgeError as e:
        emit_digest(False, "outreach approve", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"approved {result['counts']['approved']} of {result['counts']['candidates']} candidates")
    emit_digest(True, "outreach approve", counts=result["counts"],
                next_=f"leadforge outreach send --dry-run --campaign {campaign}")


# ----------------------------------------------------------------------------- send
@outreach_app.command("send")
def send(
    ctx: typer.Context,
    campaign: str = typer.Option("", "--campaign"),
    dry_run: bool = typer.Option(True, "--dry-run/--live", help="dry-run (default, safe) or --live"),
    i_am: str = typer.Option("", "--i-am"),
    mailbox: str | None = typer.Option(None, "--mailbox"),
    max_n: int | None = typer.Option(None, "--max"),
):
    """Send approved messages. Dry-run by default; --live needs outreach.armed AND --i-am."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    if not dry_run:
        if not cfg.outreach.armed:
            emit_digest(False, "outreach send", warnings=["outreach.armed is false in leadforge.yaml — arm it before --live"],
                        next_="set outreach.armed: true in leadforge.yaml, then re-run with --live")
            raise typer.Exit(3)
        if not i_am.strip():
            emit_digest(False, "outreach send", warnings=["--live requires --i-am NAME"])
            raise typer.Exit(4)
    if not campaign.strip():
        emit_digest(False, "outreach send", warnings=["--campaign is required"])
        raise typer.Exit(4)
    try:
        if dry_run:
            result = send_mod.dry_run(conn, cfg, campaign=campaign, max_n=max_n)
        else:
            result = send_mod.live_send(conn, cfg, campaign=campaign, i_am=i_am, mailbox_addr=mailbox, max_n=max_n)
    except LeadForgeError as e:
        emit_digest(False, "outreach send", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    counts = result["counts"]
    _say(ctx, ("DRY-RUN — nothing sent. " if dry_run else "") +
              ", ".join(f"{k}={v}" for k, v in counts.items()))
    emit_digest(True, "outreach send", counts=counts, artifacts=result.get("artifacts", []),
                next_=None if dry_run else f"leadforge outreach status --campaign {campaign}")


# ----------------------------------------------------------------------------- sync
@outreach_app.command("sync")
def sync(ctx: typer.Context):
    """Ingest bounces / complaints / unsubscribes / replies into suppression and target states."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    counts = sync_mod.sync_inbox(conn, cfg)
    _say(ctx, "synced: " + (", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "nothing new"))
    emit_digest(True, "outreach sync", counts=counts)


# ----------------------------------------------------------------------------- status
@outreach_app.command("status")
def status(ctx: typer.Context, campaign: str | None = typer.Option(None, "--campaign")):
    """Counts by state, caps consumed, circuit-breaker status."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    rep = status_mod.status_report(conn, campaign=campaign)
    _say(ctx, "targets: " + (", ".join(f"{k}={v}" for k, v in rep["target_states"].items()) or "none"),
              "messages: " + (", ".join(f"{k}={v}" for k, v in rep["message_states"].items()) or "none"))
    emit_digest(True, "outreach status", counts={
        "targets": rep["target_states"], "messages": rep["message_states"],
        "unknown_sends": rep["unknown_sends"], "mailboxes": rep["mailboxes"],
    })


# ----------------------------------------------------------------------------- doctor
@outreach_app.command("doctor")
def doctor(ctx: typer.Context, identity: str = typer.Option(..., "--identity")):
    """SPF / DKIM / DMARC / MX / identity-completeness / warm-up checks. Fails closed."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        results = doctor_mod.run_doctor(conn, identity)
    except LeadForgeError as e:
        emit_digest(False, "outreach doctor", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    ok = all(r.ok for r in results)
    for r in results:
        _say(ctx, f"[{'ok' if r.ok else 'FAIL'}] {r.name}" + (f" — {r.hint}" if r.hint else ""))
    warnings = [f"{r.name}: {r.hint}"[:120] for r in results if not r.ok][:5]
    emit_digest(ok, "outreach doctor", counts={"checks": len(results), "failed": sum(1 for r in results if not r.ok)},
                warnings=warnings, next_=None if ok else "fix the FAILed checks and re-run")
    raise typer.Exit(0 if ok else 3)


# ----------------------------------------------------------------------------- outcome
@outcome_app.command("add")
def outcome_add(
    ctx: typer.Context,
    business: str = typer.Option(..., "--business"),
    channel: str = typer.Option(..., "--channel"),
    result: str = typer.Option(..., "--result"),
    notes: str = typer.Option("", "--notes"),
    campaign: str = typer.Option("", "--campaign"),
    by: str = typer.Option("", "--by"),
):
    """Record a phone/email outcome; --result opt_out also writes a suppression row."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    try:
        outcome_id = status_mod.outcome_add(conn, business_id=business, channel=channel, result=result,
                                            notes=notes, campaign=campaign, recorded_by=by)
    except LeadForgeError as e:
        emit_digest(False, "outreach outcome add", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e
    _say(ctx, f"outcome recorded (id={outcome_id})")
    emit_digest(True, "outreach outcome add", counts={"id": outcome_id})
