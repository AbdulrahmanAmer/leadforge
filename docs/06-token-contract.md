# Token Contract — the agent/CLI interface

**Purpose:** make LeadForge cheap for ANY agent harness (Claude Code, Codex, others). The model's context is the scarcest resource in the
system; this contract bounds what may ever enter it.

## 1. The digest protocol

Every `leadforge` command:

1. May print a short human block (≤ 25 lines, plain text, no tables of data rows).
2. **Always** ends with exactly one machine line:

```
LF_DIGEST {"ok":true,"cmd":"discover","run":"run_20260831_1201_k3","counts":{"tiles":24,"tiles_done":24,"tiles_degraded":1,"businesses":183,"new":161},"warnings":["1 tile degraded (captcha cooldown)"],"artifacts":[],"next":"leadforge enrich"}
```

- Single line, compact JSON, stable keys: `ok, cmd, run, counts, warnings, artifacts, next`.
- `warnings` ≤ 5 strings, each ≤ 120 chars. `next` = the exact suggested next command or `null`.
- With `--json`, the human block is suppressed entirely — digest line only.

**Agent rule #1:** read the `LF_DIGEST` line; ignore everything above it. **Agent rule #2:** never `cat` files in `leadforge_data/`
(db, cache, exports) — they are unbounded. The only file the agent ever reads is a `dm export` batch, which is size-capped by design.

## 2. Budget table (agent-visible tokens per campaign, target)

| Phase | Agent I/O | Budget |
|---|---|---|
| Intake interview | 7 questions + user answers + write `answers.yaml` | ~1.5k |
| doctor + intake + plan digests | 3 digest lines | ~0.3k |
| discover + enrich digests (incl. resumes) | ~4–8 digest lines | ~0.5k |
| DM labeling | 1 batch ≤ 60 businesses × ≤ ~70 tokens/line, read + labels out | ~6–9k |
| score + export digests + final summary to user | digests + ~15-line summary | ~1k |
| **Total** | | **< 15k** |

Contrast: one raw Google Maps results page or one uncompressed business website ≈ 30–150k tokens. The pipeline processes hundreds of
pages; **none of them** cross the boundary.

## 3. DM snippet economics

- Snippets are pre-shrunk by the CLI (≤ 300 chars, title-adjacent context only) — the *local-extract → agent-decides* pattern: cheap
  deterministic code shrinks pages by ~99%, the model spends tokens only on the judgment call it is uniquely good at.
- Batches capped (`--max`, default 60). For bigger runs the agent loops batches — each batch is one read + one write.
- `dm export --tsv` offers an even terser variant (no JSON keys) when the agent prefers.

## 4. Command-surface guarantees

| Command | Max human lines | Notes |
|---|---|---|
| `doctor` | 20 | one line per check: `[ok]/[fixed]/[FAIL] name — hint` |
| `intake` | 15 | field errors only |
| `plan` | 15 | tiles/queries/estimates |
| `discover` / `enrich` / `score` / `export` | 10 | progress goes to logfile, not stdout |
| `run` | 25 | stage transitions only |
| `status` | 15 | counts snapshot |
| `dm export` | 5 + file path | data goes to the NDJSON file, not stdout |

Progress bars, per-item logging, tracebacks → `leadforge_data/logs/leadforge.log` (rotating). stdout is for humans-in-a-hurry and one
digest line for machines.

## 5. Skill-side rules (mirrored in `skills/generate-leads/SKILL.md`)

1. Ask intake questions conversationally; write `answers.yaml` yourself; never invent ICP fields the user didn't confirm.
2. Prefer `leadforge run` over stage-by-stage calls; use `--json`.
3. On `ok:false` → read the digest `warnings`/`next`, consult `references/troubleshooting.md`, retry **once** with the suggested fix;
   then report to the user. Never brute-force loops.
4. Label DM batches in a single pass from snippets; do not browse candidate websites to "double-check" (the pipeline already stored
   evidence URLs for the human).
5. Final answer to the user: counts, tier split, top hooks, sheet path. Never paste sheet contents beyond ≤ 5 example rows if asked.


## LF_PROGRESS heartbeat (v0.1.3)

Long stages (discover / enrich / registry) additionally emit bounded progress lines to stdout:

    LF_PROGRESS {"stage": "discover", "done": 12, "total": 30, "msg": "car garage in Leeds"}

Rules: one line per unit of work (query / site / lookup), never unbounded; agents MAY ignore them
entirely (the digest contract is unchanged — still exactly one LF_DIGEST at the end) or relay them
as status. Humans at an interactive terminal also get an in-place progress bar on stderr; that bar
is suppressed automatically when stderr is not a TTY (pipes, CI, agent harnesses).
