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

## Layer 0 — Dev loop and repo hygiene

- One canonical checkout: `E:/LeadForge/leadforge` (editable install re-pointed 2026-09-24). Retire the
  D: clone's worktrees once nothing depends on them; the D: campaign folder stays as data.
- Fast test tier: the full suite takes ~9 min because some tests load real torch/GLiNER/crawl4ai
  (`tests/test_browser.py`, and `test_run_no_autopilot_pauses_at_dm_pending` triggers a Hugging Face
  snapshot download). Mark them `@pytest.mark.slow`; default run excludes them; CI runs both.
- `pyproject.toml:70` sets `addopts = "-q"`; passing `-q` again hides the pass/fail summary. Document it
  in AGENTS.md or drop the addopt.
- Code discovery (done 2026-09-24): code graph (codebase-memory, project `E-LeadForge-leadforge`) for exact
  symbols/callers; `scripts/code_index.py query "<question>" [--kind code]` for plain-English questions
  (eval hit@5 19/22; on 5 unseen questions 3 found the right code first, 2 found the right ADR/doc first
  — use `--kind code` when you want the implementation). Remaining L0 work: keep both fresh — a git
  post-commit hook running `code_index.py build` (~4 s when nothing changed) and the graph re-index (~3 s).
- **Gate:** `pytest` (fast tier) < 60 s and green; `ruff check src tests` clean; slow tier green.

## Layer 1 — Run control (the pipeline's state machine)

- `leadforge run --detach` → returns run id + log path immediately; `leadforge status --run <id>` →
  bounded digest (stage, counts, rate, ETA). Required by the skill (runs outlive a 10-min tool call).
- `leadforge run --resume --from-stage <stage>` replaces the raw `sqlite3 UPDATE runs SET stage=…`
  recovery recipe in POSITION.
- One code path marks a run `exported` (today: `pipeline.py:544` and `cli.py:304`).
- **Gate:** tests for detach/status/resume-from-stage; a detached tiny run (mock provider) reaches
  `exported` while `status` reports progress.

## Layer 2 — Agent runner isolation and error visibility

- `agent_runner.py:64` spawns a full `claude -p` per batch — it loads every MCP server, hook and plugin
  the user has (estimated ~15 node processes per call on this machine from the 7 configured Hostinger MCP
  servers, each an npx + node pair — UNPROVEN until Layer 2 counts them). Add `--strict-mcp-config` (verified to exist;
  `--bare` is not usable — it refuses OAuth/subscription auth) and a no-hooks settings override (verify
  the mechanism), a concurrency cap, and a per-call timeout.
- Log every runner failure: `draft/service.py:364,390` swallow exceptions silently.
- **Gate:** measure per-call latency and process count before/after on one real DM batch; tests for the
  timeout and the logged-failure path.

## Layer 3 — Throughput

- Move NER out of the crawl pool. Today GLiNER runs inside `_process_one` in the 12-thread I/O pool
  (`enrich/runner.py:118,305`), so CPU work stalls network workers. Target shape: crawl threads (I/O
  only) → queue → one NER worker process with batched inference → DB writer thread (already single-writer).
- Registry: overlap lookups with crawling within the API's rate limit; cache by company number.
- Discovery: keep the novelty-gated tile subdivision (v0.4.1); measure queries per new business.
- **Gate:** sites/min over a fixed 30-min window of real crawling, before vs after, same campaign and
  machine; CPU per core logged. No regression in people-per-crawled-site.

## Layer 4 — Data quality

- First measure per-crawled-site yield (email, phone, DM, tier) on the crawled 1,099 — then fix the
  largest leak (e.g. contact pages not reached, JS-only sites, NER misses, registry-only directors not
  merged).
- **Gate:** yield per crawled site before/after on a fixed sample; no fabricated contacts (existing
  no-fabrication gate stays green).

## Layer 5 — Laya, shadow mode (optional)

- Run Laya's typed-decision classifier beside the `claude -p` DM labeler on the parked run's 393 labeled
  businesses; compare agreement, latency, cost. Replace nothing until the numbers justify it. Weights come
  from the Hugging Face Hub at runtime — pin a revision and cache locally.
- **Gate:** an agreement report; decision recorded as an ADR.

## Layer 6 — Skill v2 (last)

- Per `docs/specs/skill-v2.md`: split lead-gen and outreach skills, launch-and-monitor, plugin-root
  bootstrap, neutral defaults. **Gate:** that spec's acceptance list.
