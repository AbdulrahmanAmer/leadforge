# LeadForge — agent instructions

Internal B2B lead-generation tool. One repo, three consumers: **Claude Code** (plugin), **OpenAI Codex** (plugin/skills), and humans.

## ⚡ Build trigger

**If the user says `BUILD LEADFORGE`, "finish the build", or "continue the ICM": read `icm/HANDOFF.md`, then
`icm/STATE.md`, then `docs/05-icm-build-plan.md`, then the rest of this file — and start working the remaining
units in the order `icm/STATE.md` lists.** Don't explore blindly or re-architect; the design is decided.
The same instructions are pasteable from `icm/PROMPT.txt`.

## What to know first

- The user-facing behavior lives in the skill: `skills/generate-leads/SKILL.md`. If the user wants leads, follow that skill.
- The engine is the `leadforge` Python CLI (`src/leadforge/`). **Never scrape or crawl the web yourself for this task — the CLI does it.**
- **Token contract (`docs/06-token-contract.md`):** every command ends with one `LF_DIGEST {json}` line — read that line, ignore the rest.
  Never `cat` anything under `leadforge_data/` (db, cache, exports are unbounded). The only pipeline file you read is a `dm export` batch.
- Environment problems are the CLI's job: run `leadforge doctor --fix --json` before diagnosing anything by hand.

## Dev commands

```bash
pip install -e .[dev]      # install (never needs a compiler; pure-python wheels)
pytest -q                  # test suite — must stay green
ruff check src tests       # lint
leadforge doctor --fix     # bootstrap runtime deps (downloads pinned scraper binary)
```

## Working on the codebase

- Read `docs/05-icm-build-plan.md` first — it enumerates every unit, its status (IMPLEMENTED/STUB/TO-BUILD), acceptance criteria, and the
  **one-shot finalize protocol**. Do units in the given order; a unit is done only when its acceptance criteria pass.
- Architecture, data model, pipeline behavior, and decisions: `docs/02`–`04`, `docs/08`. Do not contradict an ADR without recording a new
  one in `docs/08-decisions.md`.
- Canonical schema lives in `src/leadforge/models.py`; the SQLite DDL in `db.py` must mirror it and `docs/03-data-model.md`.
- Politeness/compliance invariants (`docs/04` §5, `docs/07`) are load-bearing: robots.txt respect, per-host single-flight + delay,
  caps, suppression filtering, no SMTP probing, no login-gated scraping, no LinkedIn. Never weaken them to "make a test pass".
- Scope red lines: use the pinned OSS engines as shipped — never write or extend anti-bot/captcha evasion, fake accounts, or
  fingerprint spoofing; never add outreach/sending features; public business data only. A unit that seems to need any of that is
  mis-read — stop and flag it (`docs/07-compliance.md` has the reasoning).
- Style: Python 3.11+, pathlib-only paths, `shell=False` subprocess with explicit encoding + timeout, pydantic v2 at boundaries,
  no new runtime deps outside `pyproject.toml` extras.

## Harness install matrix (for telling users)

| Harness | Commands |
|---|---|
| Claude Code | `/plugin marketplace add AbdulrahmanAmer/leadforge` → `/plugin install leadforge@leadforge` |
| Codex | `codex plugin marketplace add AbdulrahmanAmer/leadforge` → install "LeadForge" from `/plugins` |
| Any Agent-Skills harness | `npx skills add AbdulrahmanAmer/leadforge` (or `python install.py`) |
| Plain CLI | `pip install -e . && leadforge doctor --fix` |

