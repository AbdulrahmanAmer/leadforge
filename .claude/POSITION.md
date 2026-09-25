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

## POSITION — 2026-09-25 ~01:20, UPGRADE COMPLETE (0.5.0, local); NOT PUSHED

- All layers done or decided: L0 dev loop, L1 run control, L2 isolated agent (15 s -> 5.2 s/call), L3 GPU NER +
  24 workers (crawl 16.4 -> 26.3 sites/min, same yield), L4a/4b quality fixes (small measured gains; coverage is
  the lever), L5 Laya = measured no-go (ADR-017), L6 skill v2 + bin/leadforge + release 0.5.0.
- Acceptance: real Bath GB campaign via skill flow exported 20 leads in 4.4 min (plan said 5).
- Suite 765 passed, 1 skipped (~70-95 s, `pytest -n 6`); ruff clean; `claude plugin validate .` passes.
- master is 53 commits ahead of origin (incl. this one) — NOT pushed (owner decides; push publishes to GitHub).
- Open: resume the parked UK campaign (E:/LeadForge/campaigns/uk-autorepair) on 0.5.0 when the owner
  wants (`leadforge run --icp icp.yaml --resume --from-stage enriching --detach --json`, ~9k sites ≈ 6 h at 26/min).
- Laya DM fine-tune DONE (E:/LeadForge/laya-dm, head-only, 460 train ex): held-out 133, shuffled order —
  finetuned 43.6%, random 37.6%, first-cand 29.3%, zero-shot 21.8%. NOT integrated (disagrees with Claude 56%).
- KEY FINDING: Claude's pick is always in the best title tier (98/98). Title rule + tie-break "web-found before
  registry-only" = 72.9% test / 75.0% train, zero cost. Path to 80%: rule does ranking; Laya (or rule) only for
  top-tier ties + "none"; fix same-person name duplicates (35/658 examples); more labels; measure Claude's own
  consistency as the ceiling. trained `temperature` was not saved (confidence uncalibrated).
- Open follow-ups: lcmautomotive-style post-redirect link miss; Laya fine-tune idea (ADR-017); war-room crash
  cause UNPROVEN.
