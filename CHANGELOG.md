# Changelog

## [0.1.4] — 2026-08-31

### Added
- **Browser fallback for HTTP-blocked sites**: a site that 403s/refuses the plain HTTP client is
  retried with a real rendered browser (same as a person opening it) before enrichment gives up;
  robots-disallowed sites never escalate. New signals: `rendered`, `http_blocked`.
- CI: `claude plugin validate` job (closes the v0.1.1 deferred item); the version-consistency job
  now anchors against the pushed release tag, so five stale-but-agreeing strings can't ship again
  (exactly how 0.1.2/0.1.3 went out self-reporting 0.1.1).

### Fixed
- **Coverage stats counted placeholders as data**: the live 709-lead run reported `with_dm=709` /
  `with_email=709` while 330 DM cells and 541 email cells held zero-blank-cell placeholder text.
  Summary sheet + report.json now count only real values; the sheet itself keeps every cell resolved.
- **Registry DM names were reversed**: Companies House/OpenCorporates list officers as
  "SURNAME, Given Names" and sheets showed "Murphy, Sean Vincent". `natural_name()` flips
  person-shaped comma names at ingestion (new runs) and at export (re-exports of existing DBs heal
  too); corporate commas ("Acme Widgets, Inc") and two-comma names pass through untouched.
- Version identity drift: pyproject/__init__/both plugin manifests/skill frontmatter all said 0.1.1
  while v0.1.3 code shipped; all five now read 0.1.4.

## [0.1.3] — 2026-08-31 (retro entry — commits 4c87029/fa4db04/7b0b2d2 were never tagged)
- Progress heartbeat: `LF_PROGRESS` JSON lines for agents + live single-line stderr bar (%, ETA,
  stage history) across discover/enrich/registry; machine lines only when piped.
- `leadforge watch` + auto progress window for headless runs; sheet auto-open after export;
  headed-browser debug mode (no stealth flags).
- Zero-blank-cell export rule: every cell resolved with the reason it would have been blank.

## [0.1.2] — 2026-08-31 (retro entry — commits cdbb1d8/051ad0a were never tagged)
- Call-ready sheets: registry stage covers site-less businesses; Companies House profile columns
  (number / incorporation / status / SIC codes), opening hours, Call Readiness column.
- fix(discover): `caps.max_leads` counts unique leads, not raw listings — cross-category duplicates
  no longer consume the cap (the live 1000-cap run had stopped at 709).

## [0.1.1] — 2026-08-31

### Added
- **WE SCORE prospecting profile** (`scoring: {profile: account_fit}`): account-intel enrichment
  (`enrich/profile.py` — tech stack via MX records + site fingerprints, departments, headcount estimate,
  industry buying triggers with freshness bands; tri-state facts, UNKNOWN never counts as NO), a fixed
  0-100 account-fit rubric with A-D grades, separate Contactability and Data Confidence scores, and
  NEW/READY_FOR_OUTREACH/MANUAL_REVIEW/DISQUALIFIED statuses in the export. Example:
  `config/icp.wescore.example.yaml`.
- **Registry DM auto-pick** (ADR-010): exactly one active individual director from Companies House is
  marked as the decision maker automatically; ambiguous cases still go to the agent.
- `leadforge config set|get`, `export --format`, `doctor --fix --full` bootstraps all quality extras
  (incl. new `[social]` yt-dlp extra); `version` now emits a digest; export gains a `Stale?` column
  driven by `validation.staleness_days`.
- Intake seeds default soft qualifiers so the "Likely Need (Hook)" column is populated by default.

### Fixed
- gosom v1.17.4 hangs after writing results: a stall watchdog (`discovery.stall_s`) terminates and
  salvages the written listings; captcha classification preserved on the kill path.
- doctor `--fix --full` falsely reported fresh extras as failed (stale import caches — now probed in a
  child interpreter); crawl4ai-setup timeout no longer crashes doctor.
- Junk-email extraction (word-splitting obfuscation regex, markup-truncation artifacts, cross-domain
  testimonial emails), newline-crossing person names, GLiNER title pairing, Excel phone mangling.
- Hardcoded copyright year; suppressed domains leaking into exports; `caps.max_leads` not enforced.

### CI
- Matrix extended to Python 3.11-3.14; release tags trigger CI; version-consistency job; non-editable
  install check; digest asserted in CLI smoke.

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
- full offline end-to-end pipeline test; ruff clean.

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
