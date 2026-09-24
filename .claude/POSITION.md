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

## POSITION — 2026-09-24 ~20:00, LAYER 0 DONE; GPU BENCHMARK PENDING; NEXT = LAYER 1

- L0 gate met: `pytest -n 6` 718 passed 86 s (serial 221 s); ruff clean; .githooks/post-commit refreshes
  code_index + graph (enable: git config core.hooksPath .githooks). CLI import 1.5 s -> 1.2 s (7ee4f27).
- L0 open (owner decision): retire D:/GainLev/LeadForge/leadforge worktrees (campaign data stays).
- GPU: Quadro M1200 (sm_50, 4 GB). Main env torch 2.10.0+cpu (untouched). Test venv E:/LeadForge/.venv-gpu
  installing torch 2.10.0+cu126. Laya needs no code change (fp16 <sm80, sdpa, OOM->CPU fallback).
  Bench script: scratchpad laya_bench.py (cpu|cuda). Owner wants CPU vs GPU numbers BEFORE Laya work;
  pause heavy CPU work while benchmarking.
- NEXT: L1 run control (--detach, status --run, resume --from-stage, one exported path, CLI import pass).
- UNPROVEN: war-room crash cause; ~15 node procs per `claude -p` (L2 counts it).
