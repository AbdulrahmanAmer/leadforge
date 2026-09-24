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

## POSITION — 2026-09-24, CLEANUP + AUDIT SESSION (Opus 5.5; no source code changed yet)

- War-room remnants deleted (agent_chat/, run_laya_debate.py, debate_round_1_prompts.json). Likely crash cause: 10 always-on agents each spawning `claude -p` (each loads ~8 MCP servers) + browser reconnect every 3 s. UNPROVEN (not reproduced). Never spawn claude CLI per agent turn from a server.
- Code graph: codebase-memory project `E-LeadForge-leadforge` indexed (2546 nodes); MCP registered at local scope for this repo. Name search works; plain-English search is keyword-only (weak).
- Kept for review (untracked, from opencode): LAYA_INTEGRATION_SPEC.md, tests/test_autopilot_integration.py, build_vector_index.py + .vector_index/. leadforge_data/ kept (owner).
- IN FLIGHT: three read-only audits (Laya spec, pipeline gaps, parked-run perf). Parked run is probably under D:/GainLev/LeadForge/leadforge.
- NEXT: write docs/UPGRADE-PLAN.md from the audits; semantic vector index; profile before any speed claim.
