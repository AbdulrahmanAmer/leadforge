"""`leadforge outreach` sub-app (v0.3). Registered by leadforge.cli; unit E fills in the commands.

Until unit E lands, every command emits an honest `ok:false` digest naming the unit — never a silent no-op.
"""

from __future__ import annotations

import typer

from leadforge.util import emit_digest

outreach_app = typer.Typer(help="Outreach lifecycle: plan -> draft -> approve -> send (dry-run default) -> sync -> status.")

_NOT_BUILT = "NOT IMPLEMENTED — v0.3 unit E (docs/09-v0.3-build-plan.md)"


def _stub(name: str) -> None:
    emit_digest(False, f"outreach {name}", warnings=[_NOT_BUILT], next_=None)
    raise typer.Exit(1)


@outreach_app.command("plan")
def plan(ctx: typer.Context) -> None:
    """Enrol scored leads as outreach targets (every exclusion counted by reason)."""
    _stub("plan")


@outreach_app.command("approve")
def approve(ctx: typer.Context) -> None:
    """Approve drafted messages; approval is bound to the draft's content hash."""
    _stub("approve")


@outreach_app.command("send")
def send(ctx: typer.Context) -> None:
    """Send approved messages. Dry-run by default; --live needs outreach.armed AND --i-am."""
    _stub("send")


@outreach_app.command("sync")
def sync(ctx: typer.Context) -> None:
    """Ingest bounces / complaints / unsubscribes / replies into suppression and target states."""
    _stub("sync")


@outreach_app.command("status")
def status(ctx: typer.Context) -> None:
    """Counts by state, caps consumed, circuit-breaker status."""
    _stub("status")


@outreach_app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """SPF / DKIM / DMARC / identity checks for a sending identity; fails closed."""
    _stub("doctor")
