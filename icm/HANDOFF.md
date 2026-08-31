# LeadForge — Build Handoff (read this first)

You are a fresh coding session (Claude Code / Codex) that has never seen this repo. This file gets you
productive in minutes and lets you **one-shot the remaining build**. Read it fully before touching code.

## What this repo is

An internal, OSS-only B2B lead-generation engine that installs into Claude Code **and** Codex as a
plugin/skill, and does all scraping/enrichment/scoring in a Python CLI so the agent spends almost no tokens.
Full vision: `docs/00-vision.md`. It is **already a working scaffold** — the core pipeline runs end to end
offline (see `tests/test_pipeline_e2e.py`). Your job is to finish the optional units and validate it live.

## Orient yourself (10 minutes, in this order)

0. **`icm/SCOPE.md`** — what this project is, why the work is in bounds, and the hard boundaries. Read it
   first; it removes almost every judgment call you would otherwise have to make.
1. `docs/05-icm-build-plan.md` — the staged plan: every unit, its status (IMPLEMENTED/STUB/TO-BUILD),
   acceptance criteria, and the **one-shot finalize protocol** at the bottom. This is your worklist.
2. `icm/STATE.md` — the live status tracker. Update it as you complete units (it is the single source of
   truth for "what's left").
3. `docs/02-architecture.md` + `docs/03-data-model.md` — how the pieces fit; the canonical schema.
4. `docs/06-token-contract.md` — the non-negotiable agent/CLI interface (the `LF_DIGEST` protocol).
5. `AGENTS.md` — coding rules + invariants you must not break.
6. Then skim `src/leadforge/` guided by the architecture doc. Every module's docstring names its ICM unit.

## Ground truth commands

```bash
pip install -e .[dev]     # pure-python; never needs a compiler
pytest -q                 # MUST stay green — 45 passed, 1 xfailed is the shipped baseline
ruff check src tests      # MUST stay clean
leadforge doctor --fix    # bootstraps the runtime (downloads the pinned gosom binary)
```

If `pytest` is red before you change anything, the environment drifted — run `leadforge doctor --fix` and
re-install; do not start feature work on a red baseline.

## The remaining work (all specced; do in this order)

Each unit below has a **binding spec in a stub file's docstring** and **acceptance criteria in
`docs/05-icm-build-plan.md`**. The stub raises `NotImplementedError` and has an xfail/skipped test waiting.

| Order | Unit | File (spec in docstring) | Gate |
|---|---|---|---|
| 1 | U3.6 Fallback REST provider | `src/leadforge/providers/fallback_rest.py` | G3 |
| 2 | U4.5 Browser escalation (JS sites) | `src/leadforge/enrich/browser.py` | G4 |
| 3 | U4.6 Registry cross-check (opt-in) | `src/leadforge/providers/registry.py` | G4 |
| 4 | U4.7 GLiNER DM upgrade (opt-in) | hook in `src/leadforge/enrich/dm.py` | G4 |
| 5 | U3.7 gosom serve mode (optional) | `src/leadforge/providers/gosom.py` | G3 |
| 6 | U8.1 Finish test suite | `tests/` (see per-file TODO headers) | G8 |
| 7 | U8.3 CI | `.github/workflows/ci.yml` (present; verify + extend) | G8 |
| 8 | **U8.2 Live E2E validation** | run on a real machine (see below) | G8 |
| 9 | U8.4 Guardrails audit + U8.5 tag & push | — | G8 |

**U8.2 is the most important and the only one needing the network + the real gosom binary.** Everything
else is offline-testable.

**Every unit above has a step-by-step spec with code skeletons, exact test names and acceptance checklists in
`icm/stages/`** — `stage-3-discovery.md`, `stage-4-enrichment.md`, `stage-8-hardening.md`. Follow those
literally; they are written so no design judgment is required.

### The live-validation reality (read before U8.2)

The scaffold's gosom field mapping (`GOSOM_FIELD_MAP` in `providers/gosom.py`) is built from the v1.17.4
README, not from a real run's bytes. **Expect field drift.** The finalize protocol handles it:

```bash
leadforge doctor --fix
# tiny real campaign (1 category, small town) so you see real gosom NDJSON quickly:
leadforge run --icp icp.yaml --limit 15 --json
```

Then: open the newest file in `leadforge_data/cache/gosom_*.json`, compare its real keys to
`GOSOM_FIELD_MAP`, fix the map, drop a real sample into `tests/fixtures/gosom_sample.ndjson`, and add a
parse test asserting the mapping. This converts the one place that could be wrong into a covered fact.

## Invariants you must not break (audited at G8)

- The `LF_DIGEST` contract: every command ends with exactly one digest line; no command dumps unbounded
  output; the agent never reads `leadforge_data/*` except a `dm export` batch. (`docs/06`)
- Politeness/compliance is code, not comments: robots.txt honored, one in-flight request per host + delay,
  ICP caps enforced, suppression filtered, **no SMTP RCPT probing, no LinkedIn, no login-gated scraping.**
  (`docs/04` §5, `docs/07`, ADR-009)
- **Scope-of-work red lines** (they keep this project clean and any coding agent comfortable building it):
  use the pinned OSS engines exactly as shipped — never write or extend anti-bot/captcha evasion, fake
  accounts, or fingerprint spoofing; never add outreach/sending features; only public business data. If a
  unit seems to need any of that, stop and flag it instead — it doesn't (see `docs/07-compliance.md`).
- One canonical schema (`models.py`) mirrored by the DDL (`db.py`) and `docs/03`. Change all three together.
- No new runtime dependency outside `pyproject.toml` extras. No native-compile deps on the default path.
- Every consequential choice already has an ADR (`docs/08`); add a new ADR rather than silently diverging.

## Definition of done (per unit)

Acceptance criteria met · tests added/updated and green · ruff clean · `LF_DIGEST` respected · docs updated
if behavior moved · `icm/STATE.md` checkbox ticked. When all gates in `docs/05` pass, do U8.5 (tag v0.1.0,
verify the install matrix in `README.md`, push).

## How you were probably launched

The operator either pasted `icm/PROMPT.txt` or typed the keyword **`BUILD LEADFORGE`** (wired in `CLAUDE.md`)
or ran the `/finalize` command (`.claude/commands/finalize.md`). All three routes point here. Begin at step 1
of the finalize protocol in `docs/05-icm-build-plan.md`.
