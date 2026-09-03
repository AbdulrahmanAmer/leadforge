"""Decision-maker loop (U4.4) — the agent-in-the-loop step (docs/04 §3.4, docs/06 §3).

CLI-side glue for `dm export` (NDJSON/TSV batches of candidate snippets) and `dm apply` (labels back).
The agent does the judgment; this module only shrinks + records. Optional GLiNER upgrade hook = U4.7.

v0.4 (ADR-015, autopilot): `auto_label` drives the same loop unattended during `leadforge run` — batches
through the operator's own headless Claude Code (`agent_runner.make_ndjson_runner`, built from
`LABEL_INSTRUCTIONS`) when available, then `heuristic_labels` for anything left over (a business with
exactly one candidate whose title already matches the ICP's priority list). Both are the SAME judgment a
human would apply via `dm export`/`dm apply` — nothing here relaxes rule 3 (better no DM than a wrong DM).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from leadforge import db
from leadforge.config import Config
from leadforge.models import ICP
from leadforge.util import LOG, InputError, now_iso

# condensed rules 1-6 of skills/generate-leads/references/dm-labeling.md, for the headless runner (ADR-015)
LABEL_INSTRUCTIONS = """You are labeling decision-maker candidates for B2B leads. Each input line is one \
business: {"biz","name","category","icp_titles","candidates":[{"i","name","title","snippet","origin"}]}.

Rules:
1. Snippets only — never browse to verify; provenance is already stored for the human.
2. Pick the candidate whose ROLE best matches icp_titles order: actual authority over the offer decision,
   not seniority for its own sake (for a marketing offer, a "Marketing Manager" beats a "CFO").
3. pick:-1 when no candidate plausibly decides (all technicians/staff), names look like testimonials or
   customers, or the "person" is likely a brand/franchise figure. Better no DM than a wrong DM.
4. confidence: 0.9+ exact title match plus a founder-style snippet; 0.6-0.8 plausible authority (e.g. a
   "Manager" at a small shop); 0.5 or below, use pick:-1 instead.
5. title_override: set only when the snippet clearly shows a better title than the extracted one.
6. Ambiguous duo (e.g. two owners): pick the one tied to operations/commercial decisions.

Reply with ONLY one JSON line per input line: {"biz","pick","confidence","title_override"?}. \
Do not use any tools. No prose."""


def batch_lines(conn: sqlite3.Connection, icp: ICP, rows: Iterable[sqlite3.Row]) -> list[str]:
    """The NDJSON lines for one batch of `rows` (businesses from `db.dm_pending`) — shared by `export_batch`
    (human labeling) and `auto_label` (the headless runner)."""
    titles = icp.decision_maker.titles_priority
    lines: list[str] = []
    for b in rows:
        people = [p for p in db.people_for(conn, b["id"]) if p["is_dm"] == 0]
        if not people:
            continue
        cands = [{"i": i, "name": p["name"], "title": p["title"], "snippet": p["snippet"],
                  # v0.3: where the candidate came from (heuristic|registry|gbp) — survives agent labeling
                  "origin": p["origin"] or p["labeled_by"]}
                 for i, p in enumerate(people)]
        lines.append(json.dumps({
            "biz": b["id"], "name": b["name"], "category": b["category"],
            "icp_titles": titles, "candidates": cands,
        }, ensure_ascii=False, separators=(",", ":")))
    return lines


def export_batch(conn: sqlite3.Connection, icp: ICP, out_path: Path, max_biz: int = 60, tsv: bool = False) -> tuple[int, int]:
    """Write one labeling batch. Returns (businesses_in_batch, remaining_after)."""
    rows = db.dm_pending(conn, max_biz)
    if tsv:
        lines: list[str] = []
        for b in rows:
            people = [p for p in db.people_for(conn, b["id"]) if p["is_dm"] == 0]
            # one row per candidate for terse review; label file still keyed by biz
            for i, p in enumerate(people):
                origin = p["origin"] or p["labeled_by"]
                lines.append("\t".join([b["id"], b["name"], str(i), p["title"], p["name"],
                                        p["snippet"][:160], origin]))
    else:
        lines = batch_lines(conn, icp, rows)
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    total_pending = len(db.dm_pending(conn, 10_000))
    return len(rows), max(0, total_pending - len(rows))


def apply_label_records(conn: sqlite3.Connection, records: Iterable[dict]) -> dict:
    """Ingest already-parsed label records (the body of `apply_labels`, shared with `auto_label`'s
    per-batch runner replies). Each record: {"biz","pick"(int; -1 none),"confidence"?,"title_override"?}."""
    applied = rejected = skipped = 0
    for rec in records:
        biz = rec.get("biz")
        pick = rec.get("pick")
        if biz is None or pick is None:
            skipped += 1
            continue
        people = [p for p in db.people_for(conn, biz) if p["is_dm"] == 0]
        if pick == -1:
            for p in people:  # mark all rejected so the business stops appearing in dm_pending
                _set_dm(conn, p["id"], is_dm=-1, confidence=0.0, title=None)
            rejected += 1
            continue
        if not isinstance(pick, int) or isinstance(pick, bool) or pick < 0 or pick >= len(people):
            skipped += 1
            continue
        chosen = people[pick]
        _set_dm(conn, chosen["id"], is_dm=1, confidence=float(rec.get("confidence", 0.7)),
                title=rec.get("title_override"))
        applied += 1
    conn.commit()
    return {"applied": applied, "rejected": rejected, "skipped": skipped}


def apply_labels(conn: sqlite3.Connection, in_path: Path) -> dict:
    """Ingest the agent's labels. Each line: {"biz","pick"(int; -1 none),"confidence"?,"title_override"?}."""
    if not in_path.is_file():
        raise InputError(f"labels file not found: {in_path}")
    records: list[dict] = []
    for raw in in_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError as e:
            rec = _parse_tsv_label(raw)  # documented terse variant (dm-labeling.md); NDJSON canonical
            if rec is None:
                raise InputError(f"bad label line: {raw[:80]} ({e})") from e
        records.append(rec)
    return apply_label_records(conn, records)


def heuristic_labels(conn: sqlite3.Connection, icp: ICP, limit: int = 10_000) -> dict:
    """Deterministic fallback (ADR-015): for every still-pending business with EXACTLY ONE unlabeled
    candidate whose title contains one of the ICP's priority titles (case-insensitive), mark them the DM
    at a modest confidence. Everything else — zero matches, or two-plus (genuinely ambiguous) — is left
    alone; this never writes a reject (-1), only ever a pick, so it can't wrongly close out a business a
    human or the agent runner still needs to look at."""
    titles_priority = [t.casefold() for t in icp.decision_maker.titles_priority if t]
    labeled = 0
    if titles_priority:
        for b in db.dm_pending(conn, limit):
            people = [p for p in db.people_for(conn, b["id"]) if p["is_dm"] == 0]
            title_matches = [p for p in people
                             if p["title"] and any(t in p["title"].casefold() for t in titles_priority)]
            if len(title_matches) == 1:
                _set_dm(conn, title_matches[0]["id"], is_dm=1, confidence=0.55, title=None,
                       labeled_by="heuristic_auto")
                labeled += 1
    conn.commit()
    return {"labeled": labeled}


def auto_label(conn: sqlite3.Connection, icp: ICP, cfg: Config, *,
               runner: Callable[[list[str]], list[dict]] | None) -> dict:
    """The autopilot labeling loop (ADR-015): batch through `runner` (the operator's own headless Claude
    Code) while it makes progress, up to `cfg.agent.max_batches`; whatever is left is handed to
    `heuristic_labels`. A runner exception stops the loop (logged, never raised) rather than failing the
    run — the leftover count is exported as `dm_unlabeled` and the digest tells the operator to
    `dm export` them."""
    batch_size = getattr(getattr(cfg, "agent", None), "batch", 40)
    max_batches = getattr(getattr(cfg, "agent", None), "max_batches", 50)
    labeled = rejected = 0
    batches = 0
    used_runner = False
    if runner is not None:
        while batches < max_batches:
            rows = db.dm_pending(conn, batch_size)
            if not rows:
                break
            lines = batch_lines(conn, icp, rows)
            if not lines:
                break
            before = len(db.dm_pending(conn, 10_000))
            try:
                records = runner(lines)
            except Exception as e:  # noqa: BLE001 — a broken/unavailable runner must not crash the run
                LOG.warning("auto_label: runner failed on batch %d, falling back to heuristics: %s", batches, e)
                break
            batches += 1
            used_runner = True
            result = apply_label_records(conn, records)
            labeled += result["applied"]
            rejected += result["rejected"]
            after = len(db.dm_pending(conn, 10_000))
            if after >= before:
                break  # no progress this batch — stop rather than loop forever on unusable replies
    hcounts = heuristic_labels(conn, icp)
    unlabeled = len(db.dm_pending(conn, 10_000))
    return {"labeled": labeled + hcounts["labeled"], "rejected": rejected, "unlabeled": unlabeled,
            "batches": batches, "runner": "agent" if used_runner else "none"}


def _parse_tsv_label(raw: str) -> dict | None:
    """'biz<TAB>pick[<TAB>confidence[<TAB>title_override]]' -> the same dict a JSON line yields."""
    parts = raw.split("\t")
    if len(parts) < 2:
        return None
    try:
        rec: dict = {"biz": parts[0].strip(), "pick": int(parts[1])}
    except ValueError:
        return None
    if len(parts) >= 3 and parts[2].strip():
        try:
            rec["confidence"] = float(parts[2])
        except ValueError:
            return None
    if len(parts) >= 4 and parts[3].strip():
        rec["title_override"] = parts[3].strip()
    return rec


def _set_dm(conn: sqlite3.Connection, person_id: int, is_dm: int, confidence: float, title: str | None,
           labeled_by: str = "agent") -> None:
    if title:
        conn.execute(
            "UPDATE people SET is_dm=?, dm_confidence=?, title=?, labeled_by=?, labeled_at=? WHERE id=?",
            (is_dm, confidence, title, labeled_by, now_iso(), person_id),
        )
    else:
        conn.execute(
            "UPDATE people SET is_dm=?, dm_confidence=?, labeled_by=?, labeled_at=? WHERE id=?",
            (is_dm, confidence, labeled_by, now_iso(), person_id),
        )
