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

## POSITION — 2026-09-24 ~20:45, OWNER GRANTED FULL CONTROL; BUILDING LAYERS 1-6

- Owner: "all permissions ... full control related to this project" — act without per-step asks; report
  measured outcomes. Never destroy D:/GainLev/LeadForge/campaign-uk-autorepair; never send live outreach.
- Done: L0 (pytest -n 6 718/86 s, hooks, CLI import); D: worktrees removed (all 4 merged, clean);
  GPU bench (GLiNER 821->193 ms/page, Laya 2678->741 ms); claude -p isolation measured (15 s->5.5 s).
- IN FLIGHT: main env torch -> 2.10.0+cu126; then L1 + L2 builders in parallel worktrees.
- ORDER: L1 || L2 -> L3 (GPU model worker) -> L4 (quality, parked-DB copy) -> L5 (Laya shadow) -> L6 skill.
- UNPROVEN: war-room crash cause; GLiNER+Laya in one process on 4 GB.
