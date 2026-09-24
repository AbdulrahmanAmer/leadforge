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

## POSITION — 2026-09-24, CLEANUP + AUDIT SESSION (Opus 5.5)

- Done+committed: war-room files deleted (2e9aa03 area); POSITION split; docs/specs/skill-v2.md (skill rewrite = LAST layer); Laya spec -> docs/specs/laya-integration.md marked 0% built; tests/test_autopilot_integration.py (14 passed on E:).
- Env: editable install re-pointed D:/GainLev/LeadForge/leadforge -> E:/LeadForge/leadforge (same commit 4b1b3a2). Parked campaign lives at D:/GainLev/LeadForge/campaign-uk-autorepair (DB + logs; do not modify).
- Measured (parked run): enrich 17-24 sites/min, registry 3.2-3.7 s/item, maps 15.7 s/query. Quality: email on 2.7% of businesses, DM rows 7.6%, tier A 3.0%.
- agent_runner.py:64 spawns full `claude -p` (all MCP servers + hooks per call). Fix: --strict-mcp-config + no hooks; UNPROVEN until measured.
- IN FLIGHT: pipeline-gaps audit; scripts/code_index.py semantic index builder (Sonnet). Graph MCP registered (local scope).
- NEXT: docs/UPGRADE-PLAN.md (layers), verify code_index eval myself, then build layer by layer.
