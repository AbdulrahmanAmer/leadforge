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

## POSITION — 2026-09-25 ~20:50, PHASE 2: MAP THE PIPELINE, THEN THIN IT

- OWNER DECISIONS (binding): (1) old DBs/workspaces are NOT supported by the finished code — old campaign data
  is for statistics only, read directly; compat shims, legacy fallbacks and old-schema migrations are removal
  candidates (incl. the v5 name migration 78908f5). (2) Map the whole tool end to end first, then remove stages
  and steps that exist only to repair another step's output; fix at the source instead.
- D: archived: campaign moved to E:/LeadForge/campaigns/uk-autorepair (128 files, SHA-256 verified);
  everything else in E:/LeadForge/_archive/gainlev-leadforge-2026-09-25.tar.gz (2135/2135 files) +
  LeadForge-older-snapshot.zip. D:/GainLev/LeadForge deleted.
- MAP DONE: docs/PIPELINE-MAP.md (ab0e81e) — 11 stages -> 7, 22 one-commit cleanup steps (~610 src lines).
  Confirmed bugs: MX timeout cached by lru_cache (validation retry can't succeed); bounce breaker reads
  classification but sync writes kind (never trips); inferred emails inserted without affinity.
- EXECUTION in 4 sequential batches (shared files): B1 = steps 1,2,9,21,3,4 + getattr shims + dead names
  (MERGED 6e06d3f: src -342/+140; suite 760 passed x6; breaker watched-fail ok);
  B2 = 5-13 (enrich/label, IN FLIGHT, opus builder); B3 = 14-18 (stage machine, score persists); B4 = 19,20,22.
  Orchestrator verifies (suite + watched-fail) and merges each batch before the next.
- Upgrade 0.5.0 done (see CHANGELOG); 55+ commits unpushed. Suite 765 passed, 1 skipped.
