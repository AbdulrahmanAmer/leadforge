# POSITION — where LeadForge actually is

Auto-injected at session start / compact / resume. The hook injects the **LAST `## ` block** (max 1400 chars),
so the newest block stays at the BOTTOM and stays short. Older blocks: `.claude/POSITION-HISTORY.md`.
Canonical detail: `icm/STATE.md` (unit status), `CHANGELOG.md` (what shipped).

---

## POSITION — 2026-09-03 ~15:25, RUN PARKED BY OWNER ("stop the run right now and park all this ... we can continue later")

Heartbeat cron c9bd4fc9 DELETED; run process 16940 killed; DB backup leadforge_data/db.sqlite3.parked-backup.
State at park: 15,827 businesses; 1,099 of 10,069 sites crawled/attempted; registry checked ~1.9k; 534 DM-pending.
Run stage set to 'enriched' by hand and campaign draft.auto_max set to 120 (was 1500) so the owner can look at a
sheet now: `run --resume` (log leadforge_data/logs/park_export_stdout.log) -> labeling (runner) -> scoring ->
drafting (<=120) -> export under leadforge_data/exports/run_20260902_22032_7eca/.
TO CONTINUE LATER: in the campaign folder, `python -c "import sqlite3;c=sqlite3.connect('leadforge_data/db.sqlite3');
c.execute(\"update runs set stage='enriching' where id='run_20260902_22032_7eca'\");c.commit()"` then
`leadforge run --icp icp.yaml --resume --json` (enrichment is idempotent: it crawls the ~9k remaining sites, ~20/min
with bounded NER, then re-labels/scores/drafts/exports); restore draft.auto_max 1500 first for full drafting.

## POSITION — 2026-09-25 ~23:00, PARKED BY OWNER mid cleanup batch 2

- Owner: "park your session for the time being ... we will continue another time".
- master = cleanup batch 1 merged (6e06d3f..2f46476): 4 bug fixes + legacy/compat removal, suite 760 passed.
- Batch 2 (PIPELINE-MAP steps 5-13) builder STOPPED mid-work. Partial commits, if any, live on branch
  worktree-agent-ad9c35eb459d7f3ed (worktree under .claude/worktrees/) — review them against the map before merging or reset and rerun.
- Remaining: batch 2 (5-13), batch 3 (14-18), batch 4 (19,20,22). Spec: docs/PIPELINE-MAP.md section 6.
- NOT done at park: two stale  watcher processes from the old Laya training runs (started 02:07/02:22,
  ~1.7k CPU-s each) were left running — the owner declined the kill; ask before touching them.
- Owner decisions still binding: old data = statistics only (no compat); fix at source, no repair layers.
- 60+ commits unpushed to GitHub (owner decides).
