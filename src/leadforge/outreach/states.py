"""Outreach lifecycle state machines (v0.3 unit E, docs/09 Wave 2 E).

Two independent state sets — a `target` (the lead's position in the outreach sequence) and a
`message` (one drafted email's own lifecycle). Kept separate on purpose: a target can carry several
messages over its life (step 1, a follow-up step 2, ...), and a message's approval/send status must
never be conflated with where the lead itself has gotten to.

`transition()` is the ONLY way any other module in this package should change a `state` column —
it enforces the allowed-move table below and stamps `updated_at`, so an illegal jump (e.g. straight
from `enrolled` to `sent`, skipping drafting/approval/queueing entirely) raises instead of silently
corrupting the record.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from leadforge.util import now_iso

Kind = Literal["target", "message"]

TARGET_STATES = {
    "enrolled", "drafted", "approved", "queued", "sent", "unknown", "bounced", "replied",
    "opted_out", "no_response", "follow_up", "done",
}

MESSAGE_STATES = {"drafted", "rejected", "approved", "queued", "sent", "unknown", "failed"}

# from-state -> allowed to-states. A state absent from a value set (including its own) may not be
# re-entered via transition() except by being the current state already (see the no-op guard below).
TARGET_TRANSITIONS: dict[str, set[str]] = {
    "enrolled": {"drafted", "opted_out", "done"},
    "drafted": {"approved", "opted_out", "done"},
    "approved": {"queued", "drafted", "opted_out", "done"},   # drafted = approval invalidated (edit)
    "queued": {"sent", "unknown", "opted_out"},
    "sent": {"bounced", "replied", "opted_out", "no_response", "unknown"},
    "unknown": {"sent", "bounced", "opted_out", "done"},       # manual resolution after investigation
    "bounced": {"opted_out", "done"},
    "replied": {"follow_up", "done", "opted_out"},
    "no_response": {"follow_up", "done", "opted_out"},
    "follow_up": {"drafted", "done", "opted_out"},
    "opted_out": set(),   # terminal — suppression wins, nothing moves a lead off it
    "done": set(),        # terminal
}

MESSAGE_TRANSITIONS: dict[str, set[str]] = {
    "drafted": {"approved", "rejected"},
    "rejected": set(),                       # terminal for this attempt; a fresh draft is a new row
    "approved": {"queued", "drafted"},        # drafted = content changed after approval (hash mismatch)
    "queued": {"sent", "unknown", "failed"},
    "sent": set(),      # terminal — successful, at-most-once
    "unknown": set(),   # terminal — a send crashed after dispatch; NEVER auto-requeued (docs/09 §E)
    "failed": set(),    # terminal
}

_TABLES: dict[Kind, tuple[str, dict[str, set[str]], set[str]]] = {
    "target": ("outreach_targets", TARGET_TRANSITIONS, TARGET_STATES),
    "message": ("messages", MESSAGE_TRANSITIONS, MESSAGE_STATES),
}


class IllegalTransition(Exception):
    """Raised by transition() on an unknown kind/state or a move the table above does not allow."""


def transition(conn: sqlite3.Connection, kind: Kind, row_id: int, new_state: str) -> None:
    """Move `outreach_targets` or `messages` row `row_id` to `new_state`, or raise IllegalTransition.

    A no-op (new_state == current state) is allowed and still stamps `updated_at` — callers that
    re-assert the current state after a partial retry should not need a special case.
    """
    if kind not in _TABLES:
        raise IllegalTransition(f"unknown transition kind '{kind}' (expected 'target' or 'message')")
    table, table_transitions, states = _TABLES[kind]
    if new_state not in states:
        raise IllegalTransition(f"unknown {kind} state '{new_state}' (expected one of {sorted(states)})")
    row = conn.execute(f"SELECT state FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise IllegalTransition(f"{kind} id={row_id} does not exist")
    current = row["state"]
    if new_state != current and new_state not in table_transitions.get(current, set()):
        raise IllegalTransition(f"illegal {kind} transition: '{current}' -> '{new_state}' (id={row_id})")
    conn.execute(f"UPDATE {table} SET state=?, updated_at=? WHERE id=?", (new_state, now_iso(), row_id))
    conn.commit()
