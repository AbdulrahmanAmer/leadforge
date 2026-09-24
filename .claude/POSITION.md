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

## POSITION — 2026-09-24, AUDITS DONE; PLAN WRITTEN; NEXT = LAYER 0

- Plan: docs/UPGRADE-PLAN.md (L0 dev loop -> L6 skill v2; skill spec docs/specs/skill-v2.md = LAST).
- Done: war-room removed; Laya spec kept as 0%-built proposal; autopilot test ruff-clean (14 passed);
  semantic index scripts/code_index.py (b05a5f1, hit@5 19/22); graph MCP registered; install -> E:.
- Parked campaign data: D:/GainLev/LeadForge/campaign-uk-autorepair (read-only).
- Gotcha: pytest addopts "-q" + extra -q hides the pass count; run without -q.
- NEXT (L0): slow-test marker + fast tier < 60 s; post-commit index refresh; CI both tiers.
- UNPROVEN: war-room crash cause; ~15 node procs per `claude -p` (count in L2).
