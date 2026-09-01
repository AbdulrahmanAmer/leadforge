"""`leadforge draft` sub-app (v0.3). Registered by leadforge.cli; unit F fills in the commands."""

from __future__ import annotations

import typer

from leadforge.util import emit_digest

draft_app = typer.Typer(help="Agent drafting loop: export packets -> agent writes -> apply (gated) -> render.")

_NOT_BUILT = "NOT IMPLEMENTED — v0.3 unit F (docs/09-v0.3-build-plan.md)"


def _stub(name: str) -> None:
    emit_digest(False, f"draft {name}", warnings=[_NOT_BUILT], next_=None)
    raise typer.Exit(1)


@draft_app.command("export")
def export(ctx: typer.Context) -> None:
    """Write one evidence packet per enrolled target (<= 350 tokens each) for the agent to draft from."""
    _stub("export")


@draft_app.command("apply")
def apply(ctx: typer.Context) -> None:
    """Ingest the agent's drafts; every draft passes the mechanical no-fabrication gate or is rejected."""
    _stub("apply")


@draft_app.command("render")
def render(ctx: typer.Context) -> None:
    """Write reviewable .txt/.eml files for drafted messages."""
    _stub("render")


@draft_app.command("check")
def check(ctx: typer.Context) -> None:
    """Run the gate on a drafts file without storing anything."""
    _stub("check")
