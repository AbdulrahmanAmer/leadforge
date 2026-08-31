"""Decision-maker loop (U4.4) — the agent-in-the-loop step (docs/04 §3.4, docs/06 §3).

CLI-side glue for `dm export` (NDJSON/TSV batches of candidate snippets) and `dm apply` (labels back).
The agent does the judgment; this module only shrinks + records. Optional GLiNER upgrade hook = U4.7.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from leadforge import db
from leadforge.models import ICP
from leadforge.util import InputError, now_iso


def export_batch(conn: sqlite3.Connection, icp: ICP, out_path: Path, max_biz: int = 60, tsv: bool = False) -> tuple[int, int]:
    """Write one labeling batch. Returns (businesses_in_batch, remaining_after)."""
    rows = db.dm_pending(conn, max_biz)
    titles = icp.decision_maker.titles_priority
    lines: list[str] = []
    for b in rows:
        people = [p for p in db.people_for(conn, b["id"]) if p["is_dm"] == 0]
        if not people:
            continue
        cands = [{"i": i, "name": p["name"], "title": p["title"], "snippet": p["snippet"]}
                 for i, p in enumerate(people)]
        if tsv:
            # one row per candidate for terse review; label file still keyed by biz
            for c in cands:
                lines.append("\t".join([b["id"], b["name"], str(c["i"]), c["title"], c["name"], c["snippet"][:160]]))
        else:
            lines.append(json.dumps({
                "biz": b["id"], "name": b["name"], "category": b["category"],
                "icp_titles": titles, "candidates": cands,
            }, ensure_ascii=False, separators=(",", ":")))
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    total_pending = len(db.dm_pending(conn, 10_000))
    return len(rows), max(0, total_pending - len(rows))


def apply_labels(conn: sqlite3.Connection, in_path: Path) -> dict:
    """Ingest the agent's labels. Each line: {"biz","pick"(int; -1 none),"confidence"?,"title_override"?}."""
    if not in_path.is_file():
        raise InputError(f"labels file not found: {in_path}")
    applied = rejected = skipped = 0
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
        if not isinstance(pick, int) or pick < 0 or pick >= len(people):
            skipped += 1
            continue
        chosen = people[pick]
        _set_dm(conn, chosen["id"], is_dm=1, confidence=float(rec.get("confidence", 0.7)),
                title=rec.get("title_override"))
        applied += 1
    conn.commit()
    return {"applied": applied, "rejected": rejected, "skipped": skipped}


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


def _set_dm(conn: sqlite3.Connection, person_id: int, is_dm: int, confidence: float, title: str | None) -> None:
    if title:
        conn.execute(
            "UPDATE people SET is_dm=?, dm_confidence=?, title=?, labeled_by='agent', labeled_at=? WHERE id=?",
            (is_dm, confidence, title, now_iso(), person_id),
        )
    else:
        conn.execute(
            "UPDATE people SET is_dm=?, dm_confidence=?, labeled_by='agent', labeled_at=? WHERE id=?",
            (is_dm, confidence, now_iso(), person_id),
        )
