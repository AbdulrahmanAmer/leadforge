# LeadForge upgrade plan — layered, measured

Status: plan (2026-09-24). Each layer ships as its own commits with its own gate, bottom-up; a layer
starts only when the one below it is green. Progress is tracked in `.claude/POSITION.md`.

Inputs: three read-only audits on 2026-09-24 (pipeline gaps, parked-run performance, Laya spec), each
spot-checked against the code before being used here. Specs: `docs/specs/skill-v2.md`,
`docs/specs/laya-integration.md`.

## Where we are (measured, parked UK auto-repair run, `D:/GainLev/LeadForge/campaign-uk-autorepair`)

| Stage | Rate | Note |
|---|---|---|
| Maps discovery | 15.7 s/query | pace.json EMA |
| Site crawl + enrich | 17–24 sites/min | 1,099 of 10,069 sites done at park (11%) |
| Companies House registry | 3.2–3.7 s/business | external API; a rate floor, not CPU |
| Labeling / scoring / drafting / export | minutes | drafting capped at 120 for the park export |

Outcome of that run: 15,827 businesses, email on 2.7%, a decision maker on 7.6%, tier A 3.0%. These
quality figures are over **all** businesses, 89% of whose sites were never crawled — so per-crawled-site
quality is not known yet (Layer 4 measures it first).

**About "20x".** At ~20 sites/min the remaining ~9k sites need ~7.5 h. 20x means ~400 sites/min, which
is about politeness limits and network, not only code. We commit to measured before/after numbers per
layer, not a multiplier in advance.

## GPU benchmark (2026-09-24, Quadro M1200 sm_50 4 GB, torch 2.10.0+cu126, one device at a time)

| Model | CPU | GPU | GPU memory |
|---|---|---|---|
| GLiNER `gliner_small-v2.1` (LeadForge NER), 6,000-char page | 821 ms/page | 193 ms/page (4.3x) | 0.7 GB |
| Laya `convaiinnovations/laya`, 5 questions | 2,678 ms/call | 741 ms/call (3.6x) | 2.3 GB |

Both fit on the card together (~3.0 of 4 GB) only inside ONE process (each CUDA process adds its own
context). Laya has no batch API (`predict = system_one`, one input per call). Laya's README claims 33 ms
GPU / 193-464 ms CPU on newer hardware; this card has no fast fp16, so the local numbers are what count.
Scripts: `scripts/bench/` (added with Layer 3).

## Layer 0 — Dev loop and repo hygiene

- One canonical checkout: `E:/LeadForge/leadforge` (editable install re-pointed 2026-09-24). Retire the
  D: clone's worktrees once nothing depends on them; the D: campaign folder stays as data.
- Test speed (done): measured 718 passed in 221 s serial — not ~9 min as first reported; no single test
  dominates (slowest 12 s). `pytest -n 6` (pytest-xdist, now in the dev extra and CI `-n auto`) → 718
  passed in 86 s. A slow/fast marker split would save less than parallelism, so it was not added.
- CLI startup (done, 7ee4f27): every `leadforge` command paid ~1.5 s of imports; dnspython via the outreach
  doctor was the largest single piece → now lazy, warm import 1.45-1.55 s → 1.15-1.26 s. Remaining cost is
  typer/click, httpx, pydantic, yaml — worth another pass in Layer 1 (the skill calls the CLI a lot).
- `addopts = "-q"` + another `-q` hides the pass/fail summary — documented in AGENTS.md.
- Code discovery (done 2026-09-24): code graph (codebase-memory, project `E-LeadForge-leadforge`) for exact
  symbols/callers; `scripts/code_index.py query "<question>" [--kind code]` for plain-English questions
  (eval hit@5 19/22; on 5 unseen questions 3 found the right code first, 2 found the right ADR/doc first
  — use `--kind code` when you want the implementation). Kept fresh by `.githooks/post-commit` (done,
  detached: `code_index.py build` ~4-8 s + graph re-index ~3 s; enable per clone with
  `git config core.hooksPath .githooks`).
- **Gate:** `pytest -n 6` green in ≤ 90 s; `ruff check src tests` clean; post-commit hook refreshes both
  indexes.

## Layer 1 — Run control (the pipeline's state machine)

- `leadforge run --detach` → returns run id + log path immediately; `leadforge status --run <id>` →
  bounded digest (stage, counts, rate, ETA). Required by the skill (runs outlive a 10-min tool call).
- `leadforge run --resume --from-stage <stage>` replaces the raw `sqlite3 UPDATE runs SET stage=…`
  recovery recipe in POSITION.
- One code path marks a run `exported` (today: `pipeline.py:544` and `cli.py:304`).
- **Gate:** tests for detach/status/resume-from-stage; a detached tiny run (mock provider) reaches
  `exported` while `status` reports progress.

## Layer 2 — Agent runner isolation and error visibility — DONE 2026-09-24 (b406252, 6c3809e)

- Result: real calls through `agent_runner.run_agent` return in 10.8 s cold / 5.2 s warm (was 14.5-15.4 s);
  config `agent.isolate: true`, `agent.max_concurrent: 2`, existing `agent.timeout_s` now kills the process
  tree; runner failures are logged and counted (`agent_failures`). Suite 723 passed; isolation test goes red
  when the default is flipped (watched-fail).

- `agent_runner.py:64` spawns a full `claude -p` per batch — it loads every MCP server, hook and plugin
  the user has. MEASURED 2026-09-24 (3 runs each, trivial prompt, agent_runner's exact flags):
  default → 7-15 extra node/claude processes, 14.5-15.4 s per call; with
  `--strict-mcp-config --settings '{"disableAllHooks":true}'` → 1-3 processes, 5.4-6.1 s, same answer.
  (`--bare` is not usable — it refuses OAuth/subscription auth.) Add those two flags, a concurrency cap,
  and a per-call timeout. Probe: scripts/bench/claude_proc_probe.ps1.
- Log every runner failure: `draft/service.py:364,390` swallow exceptions silently.
- **Gate:** measure per-call latency and process count before/after on one real DM batch; tests for the
  timeout and the logged-failure path.

## Layer 3 — Throughput — NER worker DONE 2026-09-24 (ef6073c..7952026)

- MEASURED, same 200 sites from identical copies of the parked DB, `enrich --limit 200`, this laptop:
  crawl 16.4 → 22.9 sites/min (1.4x; 12.1 → 8.7 min). Yield unchanged: people rows 154 = 154, businesses
  with people 61 → 62, with email 16 → 17. Worker proven on cuda (196 ms/page, on nvidia-smi's list,
  `ner_fallback: 0`); startup ~30 s once per enrichment. Probe: scripts/bench/worker_device_probe.py.
- Why only 1.4x: crawling is network- and politeness-bound; NER was the CPU part that got removed.
  Remaining levers below (registry overlap, more crawl workers now that CPU is free).
- Side finding: `--limit` caps the crawl only — registry lookups ran over the whole 13,528 backlog.

- Move NER out of the crawl pool. Today GLiNER runs inside `_process_one` in the 12-thread I/O pool
  (`enrich/runner.py:118,305`), so CPU work stalls network workers. Target shape: crawl threads (I/O
  only) → queue → ONE model worker process (GPU when present, CPU otherwise) → DB writer thread
  (already single-writer). On GPU, 193 ms/page ≈ 300 pages/min of NER capacity, so NER stops being a
  bottleneck and the CPU is left to crawling. The same process later hosts Laya (Layer 5) — one CUDA
  context, one model memory budget.
- Prerequisite: main environment on the CUDA build of torch (`torch==2.10.0+cu126`; it also runs on CPU).
  `leadforge doctor` reports the device; config `enrich.ner_device: auto|cpu|cuda`. Machines without a
  GPU keep today's bounded CPU path.
- Registry: overlap lookups with crawling within the API's rate limit; cache by company number.
- Discovery: keep the novelty-gated tile subdivision (v0.4.1); measure queries per new business.
- **Gate:** sites/min over a fixed 30-min window of real crawling, before vs after, same campaign and
  machine; CPU per core logged. No regression in people-per-crawled-site.

## Layer 4 — Data quality

- Baseline MEASURED 2026-09-24 (copy of the parked campaign DB, run_20260902_22032_7eca):

  | Group | n | email | decision maker | tier A |
  |---|---|---|---|---|
  | crawled | 1,099 | 39.9% | 64.2% | 23% (255) |
  | website, not crawled | 8,970 | 0% | 0.4% | 0% |
  | no website | 5,758 | 0% | 8.7% | 3.7% (212) |

  The 2.7%-email headline was a coverage problem: 89% of sites were never crawled. **Finishing the crawl
  (Layer 3) is the biggest quality lever.** Leaks inside crawled sites: 252/1,099 (23%) returned zero
  pages, 122 flagged needs_browser, 137 errors, median 2 pages per site; DM rows come mostly from the
  registry (1,865) and heuristics (830), GLiNER/agent labels 244.
- Zero-page causes (252): 115 finished (`crawled_at` set) with zero pages and NO recorded reason — a
  silent failure, first fix is to record why; 83 home pages returned HTTP 403 (bot blocking) and were not
  sent to the browser fallback — route 403 to it; 36 robots-disallowed (correct, keep); 9 × 404, 2 × 410,
  5 timeouts, 1 connect error, 1 non-HTML. 2 "websites" were link aggregators (linktr.ee, w3w.co) —
  follow them to the real site. Then: contact-page discovery depth (median 2 pages per site).
- **Gate:** yield per crawled site before/after on a fixed sample; no fabricated contacts (existing
  no-fabrication gate stays green).

## Layer 5 — Laya, shadow mode (optional) — MEASURED NO-GO for DM labeling (ADR-017)

- 150 businesses vs Claude's picks: 0.7% agreement with a "none" option (answers none 94%), 50.0% without —
  below the 58.0% first-candidate rule. Fast (235 ms GPU) but not accurate zero-shot. Revisit only after
  fine-tuning on LeadForge labels. Nothing was integrated.

- Run Laya's typed-decision classifier beside the `claude -p` DM labeler on the parked run's 393 labeled
  businesses; compare agreement, latency, cost. Replace nothing until the numbers justify it. Weights come
  from the Hugging Face Hub at runtime — pin a revision and cache locally.
- Measured budget: 0.74 s per decision on GPU → one or two decisions per business is affordable (393
  businesses ≈ 5 min); per-link decisions during crawling are not (a site has dozens of links). At
  2.7 s on CPU, Laya is GPU-only: without a GPU the pipeline skips it and keeps today's labeler.
- Runs inside the Layer 3 model worker, not its own process (GPU memory: 2.3 + 0.7 of 4 GB).
- **Gate:** an agreement report; decision recorded as an ADR.

## Layer 6 — Skill v2 (last)

- Per `docs/specs/skill-v2.md`: split lead-gen and outreach skills, launch-and-monitor, plugin-root
  bootstrap, neutral defaults, and GPU detection (doctor says whether NER/Laya will use the GPU; never
  required). **Gate:** that spec's acceptance list.
