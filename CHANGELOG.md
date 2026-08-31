# Changelog

## [0.2.0] — 2026-08-31

The coverage release: break the per-query result ceiling, rescue bot-walled sites, and propose
contacts where none are published — each one measured on live data, none of them relaxing a red line.
192 tests, ruff clean.

### Added
- **Grid tiling, live-proven.** Google Maps serves ~100–120 results per search *server-side* — the
  scraper already scrolls to the end, so deepening does nothing. Tiling splits one town into map
  cells, each with its own budget. Measured: 4 tiled queries on `auto repair shop` in Birmingham
  found **107 businesses that the entire 709-lead, 3-category, 5-city campaign had missed**
  (Birmingham 221 → 310, +40%), with **0 duplicate place_ids** after the cross-tile merge.
  Still opt-in (`discovery.grid_mode: auto`) because tiling adds a geocode per area and multiplies
  query count; `leadforge plan` now reports map cells, queries and estimated hours before you spend
  them.
- **`leadforge render-check <url>`** — diagnose the bot-wall fallback on one site (robots → plain
  fetch → rendered fetch → contacts) in a single digest. It proved the fallback live:
  `shamsautos.com` 403s the plain client, renders in a real browser, and yields an email the static
  path lost. Two robots-disallowed sites were refused without a single page fetch.
- **Inferred emails** (opt-in `validation.infer_emails`, default off): when a business has a named
  decision maker, an MX-confirmed domain, and a *real personal email already found on that domain*,
  the address that domain's own convention implies is proposed — in its own `Email (Inferred)`
  column, labeled `likely, N% — pattern X from <anchor>`, excluded from published-email coverage,
  and scored below anything observed. **No anchor, no guess.** Measured honestly: this fires on
  0 of 709 UK garages (trades publish `info@` or nothing) and does have anchors in professional-
  services campaigns. Red line intact — no SMTP, no RCPT probing, ever (SCOPE #5 clarified, not
  relaxed).

### Fixed
- **Category/area starvation under the lead cap**: `build_plan` emitted category-major, so a run
  that hit `caps.max_leads` could finish having never searched the last category. Queries now
  rotate tile-major — every category, in every area, before any tile advances.
- `geocode` retries transient network errors (3 attempts, backing off) instead of killing a
  multi-hour tiled plan on its first area; a bbox-only ICP with grid off now names the switch that
  would make it usable instead of raising a generic "no queries".
- A tiled query falling through to a provider that ignores tiles now warns that the geographic
  constraint was dropped, instead of silently scraping ungridded.
- `plan_counts` reports **distinct map cells** (it counted tiled queries before) plus tiled-query
  count and an estimated runtime.

## [0.1.4] — 2026-08-31

Finish-for-good release: a 34-agent adversarial audit over the whole tool, every confirmed
finding fixed. 155 tests, ruff clean.

### Added
- **Browser fallback for HTTP-blocked sites**: a block-shaped refusal to the plain HTTP client
  (401/403/405/406/429/503 — not 404s, dead DNS, or PDFs) is retried once with a real rendered
  browser, like a person opening the site. Robots-disallowed sites never escalate; an unreadable
  (5xx) robots.txt counts as complete disallow per RFC 9309; failed/4xx renders are rejected so
  parked-domain phones can't enter the sheet; at most 2 concurrent Chromium instances; rendered
  pages produce the same content signals + WE SCORE profile as static crawls. Sites flagged
  `needs_browser` re-enter the enrich queue automatically once the `[browser]` extra is installed.
- `suppress --kind domain|email|place_id` (place_id was documented but unreachable);
  `enrich --stage registry` documented + validated; validate-stage progress heartbeat
  (700-email DNS tails no longer trip watch's silence timeout).
- CI: `claude plugin validate` job; the version-consistency job anchors against the pushed release
  tag (exactly how 0.1.2/0.1.3 went out self-reporting 0.1.1); the non-editable install step
  asserts the packaged scoring rubric ships in the wheel.

### Fixed
- **Coverage stats counted placeholders as data**: the live 709-lead run reported `with_dm=709` /
  `with_email=709` while 330 DM cells and 541 email cells held zero-blank-cell placeholder text.
  Summary sheet + report.json now count only real values; the sheet keeps every cell resolved.
- **Registry DM names were reversed** ("Murphy, Sean Vincent"): natural order at ingestion AND at
  export (re-exports of existing DBs heal); corporate commas and two-comma names pass through.
- **Geography was effectively unscored**: `geography_match` gave full credit to any structured city
  without ever reading `icp.target.geography`; it now matches the campaign areas (soft 0.3 for
  same-country spillover towns), and the advertised-but-dead `out_of_area` penalty (-20) fires on
  country-level mismatch. Dead `competitor`/`existing_client` soft-penalty config removed (their
  real implementation is the `icp.qualify.hard` DQ path, now documented in the rubric).
- **Substring name matching DQ'd innocents** ('group' in "Grouper", 'inc' in "Vincent", competitor
  'ace' in "Palace Garage") — all four match sites are now word-bounded.
- **`run --resume` after a kill mid-enrich no-opped forever** (stage 'enriching' matched no dispatch
  block); **`config set` with a bad value bricked every command** (wrote before validating — now
  validates first, and a hand-broken leadforge.yaml degrades to a clean digest with `config set`
  still usable to repair); **degraded discovery queries are actually retried** by `--resume` (until
  the DM gate) as the digest always claimed; score/export/dm export/enrich emit a digest on every
  failure path, with a catch-all in main() so the LF_DIGEST contract holds even on crashes.
- **Excel safety**: scraped strings starting with '='/'@' neutralized (formula injection, XLSX +
  CSV); control chars stripped (openpyxl IllegalCharacterError killed the export at the end of a
  run); tier D counted in Summary/report (account_fit rows vanished from stats); placeholder
  Website/Maps text no longer styled as clickable links; `Stale?` distinguishes fresh /
  "never verified" / "unknown (bad timestamp)"; a raw unparsed Maps phone string is
  "UNVERIFIED PHONE - confirm number", not call-ready; both writers share one blank-cell rule;
  About-sheet tier legend matches the profile that graded the workbook.
- **Grid mode would have scraped the wrong continent**: `-grid-bbox` was emitted lng-first;
  gosom v1.17.4 wants lat-first (verified against the real binary). Flag contract now pinned by
  test; grid_mode stays off by default until a live tiled run.
- `dm apply` accepts the documented TSV label variant (agents following dm-labeling.md got
  "bad label line"); scoring rubric packaged into the wheel (`pip install .` shipped a score
  command that crashed on a repo-relative path); doctor reports the [social] extra truthfully and
  installs extras correctly under non-editable installs; install.py refreshes stale skill copies;
  version identity drift healed (all five strings + tag = 0.1.4).
- Headless `run` popped two progress windows; the auto window ignored `--data-dir`; `watch` went
  blank/false-timed-out after feed truncation and ETA lied when attached mid-run — all fixed.
- Dev-process note, recorded plainly: an audit agent's watched-fail mutation (a robots-bypass) was
  captured by an intermediate local commit and removed before any push; the robots-never-escalates
  invariant is now pinned by crawl()-level tests against a real local server.

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
