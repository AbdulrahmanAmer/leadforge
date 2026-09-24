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

## POSITION — 2026-09-25 ~00:30, LAYERS 0-3 + 4a DONE; L5 NO-GO (ADR-017); NEXT = L4b depth, L6 skill

- L1: run --detach / status --run / --from-stage (E2E proven on a DB copy; fix 2a4b2ca). CLI import ~0.45 s.
- L2: isolated claude -p 15 s -> 5.2 s warm. L3: GPU NER worker, crawl 16.4 -> 22.9 sites/min, same yield.
- Suite: 751 passed, 1 skipped, ~68 s (`pytest -n 6`); LEADFORGE_NO_NER_MODEL=1 keeps real GLiNER out of tests.
- Bench workspaces (scratchpad): bench-before / bench-after / e2e — copies of the parked DB; D: campaign untouched.
- L4 targets (docs/UPGRADE-PLAN.md): 115 silent zero-page sites, 83 HTTP 403 not sent to browser, link aggregators.
- Worktrees: agent worktrees now start from local master (verify `git log -1` in the brief).
- L5: Laya zero-shot DM labeling 0.7% / 50.0% agreement vs 58.0% first-candidate rule -> not integrated.
- L4a merged: aggregator sites followed/flagged, silent empty crawl impossible (error=empty_crawl). Sample of 102
  problem sites: yield flat (77->78 zero-page, emails 3->3) — most are real bot-walls/robots; no evasion added.
  Suite 759 passed, 1 skipped, ~64 s.
- NEXT after L4a: contact-page depth (L4b), then L6 skill v2 per docs/specs/skill-v2.md.
- UNPROVEN: war-room crash cause.
