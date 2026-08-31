# Changelog

## [0.1.0] — 2026-08-31 (finalized)

Finalize session (same day) — all remaining ICM units landed:

- **U3.6** Fallback REST discovery provider (conor-is-my-name engine adapter; degrades, never crashes).
- **U4.5** Browser escalation for JS-shell sites via crawl4ai (`[browser]` extra); robots + throttle honored,
  escalates only when the static pass found nothing.
- **U4.6** Public registry cross-check: Companies House + OpenCorporates (key-gated, locality-matched,
  600/5-min throttle, 429 → disable-for-run).
- **U4.7** GLiNER zero-shot DM-candidate extraction (`[ner]` extra), heuristic fallback unchanged.
- **U4.8** Social/video presence signals via Agent-Reach (opt-in; LinkedIn excluded, logged-out only,
  metadata only).
- **U8.1** Test suite completion: crawler-politeness (real local server), suppression e2e, per-command
  `LF_DIGEST` contract, extractor edge cases — 89 tests.
- **U8.4** Guardrails audit: `caps.max_leads` now a hard stop; export now filters suppressed
  domains/place_ids; audit greps clean.
- Fixes: UTF-8 stdout/stderr forced in the CLI (Windows cp1252 pipes broke the digest contract);
  DNS resolver fallback (system → 8.8.8.8/1.1.1.1) for networks that drop MX queries; person-name
  extractor word-stoplist kills "And The Team"-style false positives.

## [0.1.0-scaffold] — 2026-08-31

Initial scaffold, produced from the staged ICM build plan (`docs/05-icm-build-plan.md`).

### Docs
- `docs/00`–`08`: vision, research digest (every source verified 2026-08-31), architecture, data model,
  pipeline behavior, ICM build plan, token contract, compliance posture, and ADRs.
- `docs/diagrams/`: seven rendered PNGs (how-it-works, architecture, sequence, run states, ICM stages, data
  model, token economics) with their mermaid sources, embedded in the README and the docs they belong to.

### Packaging (dual harness)
- Claude Code plugin + self-marketplace (`.claude-plugin/`), Codex plugin + local marketplace
  (`.codex-plugin/`, `.agents/plugins/`), one portable `generate-leads` Agent Skill (spec-only frontmatter),
  `AGENTS.md` / `CLAUDE.md`, and an `install.py` user-scope bridge.

### Working core
- Config, canonical pydantic models, SQLite layer with merge-upsert + dedupe, doctor/bootstrap (auto-downloads
  the pinned gosom v1.17.4 binary per OS), intake/ICP compiler, geocoding + query planner, gosom discovery
  adapter, normalizer, polite static crawler, contact/people extractors, validators, DM export/apply loop,
  scoring engine with per-factor explanations and need-hooks, styled XLSX/CSV/report export, and a Typer CLI
  on the `LF_DIGEST` contract.
- 64 tests + 2 xfail placeholders; ruff clean; full offline end-to-end pipeline test.

### Location quality
- `target.geography.country` is **required** (ISO2, validated) and areas must be specific: geocoding is
  country-constrained, refuses ambiguous matches instead of guessing, warns on federal-country cities with no
  state, and country-qualifies every query. The campaign country also drives phone/address parsing.

### Build handoff
- `icm/`: `SCOPE.md` (what's in and out of bounds), `HANDOFF.md` (orientation), `STATE.md` (live worklist),
  `PROMPT.txt` (pasteable), and prescriptive per-stage specs in `icm/stages/`.
- Trigger a finishing session with `BUILD LEADFORGE`, `/finalize`, or `icm/PROMPT.txt`.
- CI (`.github/workflows/ci.yml`): Ubuntu + Windows × py3.11/3.12, lint, tests, manifest validation, and a
  skill-frontmatter portability guard.

### Stubs with binding specs (remaining ICM units)
- U3.6 fallback REST provider · U4.5 browser escalation · U4.6 registry cross-check · U4.7 GLiNER upgrade ·
  U4.8 social/video presence via Agent-Reach (config + signals + hooks + guardrail tests already wired).
