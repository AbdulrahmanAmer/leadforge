# Build State Tracker

Single source of truth for what's done vs left. Current baseline (v0.1.4, 2026-08-31): **155 passed, 0 failed locally with all extras**, ruff clean.

## Stage gates

- [x] **G0 Foundations** — package installs, doctor works, smoke tests green
- [x] **G1 Contracts & data model** — models ↔ DDL ↔ docs/03 aligned; merge/dedupe tested
- [x] **G2 Intake & ICP** — compiler validates + deterministic hash; example ICPs compile
- [x] **G3 Discovery** — gosom adapter (+stall watchdog) + normalizer + planner + fallback provider; live-validated
- [x] **G4 Enrichment** — crawler/extractors/validators/DM-loop + browser + registries (CH live-proven) + NER + account-intel profile
- [x] **G5 Scoring** — rubric + hooks + explanations; deterministic, tested
- [x] **G6 Export** — XLSX/CSV/report; produced in E2E test (verify visually on a real machine at G6/U8.2)
- [x] **G7 Harness integration** — both plugins + skill + AGENTS/CLAUDE + install.py present (validate live at U8.2)
- [x] **G8 Ship** — v0.1.1 tagged, CI green (incl. tag run), live E2E done, guardrails audit clean.
      Codex install route PROVEN live 2026-08-31 (codex-cli 0.151.0: marketplace add -> plugin
      leadforge@leadforge 0.1.1 installed -> skill visible -> Codex ran the CLI and returned a valid
      LF_DIGEST). Still operator-only: Excel/LibreOffice visual sheet check.

## Units

### Done (IMPLEMENTED + tested)
- [x] U0.1 skeleton · U0.2 packaging · U0.3 config · U0.4 util/digest/throttle · U0.5 doctor+bootstrap
- [x] U1.1 models · U1.2 db · U1.3 db/model tests
- [x] U2.1 answers schema · U2.2 intake compiler · U2.3 examples
- [x] U3.1 provider ABC · U3.2 grid/plan · U3.3 gosom adapter · U3.4 normalizer · U3.5 discover command
- [x] U4.1 crawler · U4.2 extractors · U4.3 validators · U4.4 DM export/apply loop
- [x] U5.1 rubric · U5.2 hooks · U5.3 scoring tests
- [x] U6.1 XLSX · U6.2 CSV/report · U6.3 cross-run freshness
- [x] U7.1 Claude plugin · U7.2 Codex plugin · U7.3 skill · U7.4 AGENTS/CLAUDE · U7.5 install.py

### Remaining (STUB or TO-BUILD — specs in stub docstrings + docs/05 + icm/stages/)
- [x] **U3.6** Fallback REST provider — `providers/fallback_rest.py` — degraded path verified live; up-path (docker container) UNPROVEN on this machine (no docker)
- [x] **U3.7** gosom serve mode — CLOSED BY DESIGN (multi-hour runs handled by resume + stall watchdog instead)
- [x] **U4.5** Browser escalation — implemented + wiring tests; crawl4ai IS installed on this machine
      (2026-08-31); v0.1.4 added the HTTP-blocked fallback + render rejection + signal parity.
      Live render against a real JS-only site remains UNPROVEN (no live blocked-site run yet).
- [x] **U4.6** Registry cross-check — implemented + fixture tests; Companies House LIVE-PROVEN (18 real directors, Guildford campaign); OpenCorporates fixture-tested
- [x] **U4.7** GLiNER DM upgrade — hook + selection rule + tests (skip cleanly without [ner])
- [x] **U4.8** Social/video presence — implemented (youtube via yt-dlp; others honest 'unknown'), linkedin-exclusion tested
- [x] **U8.1** Finish test suite — politeness (real server), suppression e2e, digest contract per command, extractor edges
- [x] **U8.2** Live E2E validation — real UK campaign (accounting firms, Guildford): 15/15 businesses with
      phone-or-website, 0 dup place_id, DM loop round-tripped (5 applied, 5 rejected), XLSX/CSV exported;
      real fixture committed. NO field drift in GOSOM_FIELD_MAP. Excel/LibreOffice VISUAL check UNPROVEN
      (operator: open `D:\GainLev\LeadForge\campaign-uk-test\leadforge_data\exports\run_20260831_03433_8ad5\`)
- [x] **U8.3** CI verified locally (matrix/lint/test/manifests all present); green GH Actions run UNPROVEN until first push
- [x] **U8.4** Guardrails audit — all 10 items pass (max_leads hard stop + export suppression filter were fixed to pass)
- [x] **U8.5** Pushed to https://github.com/AbdulrahmanAmer/leadforge (public), tagged v0.1.0, CI run
      33354980045 green on all 5 jobs (win+ubuntu × py3.11/3.12 + manifests). Partner install from the
      GitHub link UNPROVEN — that's the final G8 acceptance and needs the partner's machine.

## Notes / decisions log (append as you go)
- 2026-08-31 U8.2: gosom full-depth run on 1 query took >30m (visits every place page) — added depth=1 cap
  for --limit ≤20 smoke runs and partial-NDJSON salvage on timeout (both tested).
- 2026-08-31 U8.2 known quality item: email regex can extract junk like `str@egy.in` from page text that
  passes syntax+MX; consider a domain-vs-business-site affinity check later. Also gosom sometimes lists a
  different site than the business name suggests (listing data, not our bug).
- 2026-08-31 scaffold: gosom FIELD_MAP derived from README, NOT a live run — verify at U8.2 (expect drift).
- 2026-08-31 finalize: this machine's VPN DNS (10.255.255.x) drops MX queries — added `get_resolver()`
  fallback (system → 8.8.8.8/1.1.1.1, probed once per process) in `enrich/validate.py`, used by doctor too.
- 2026-08-31 finalize: CLI now forces UTF-8 stdout/stderr in `main()` (Windows cp1252 pipes broke the digest contract).
- 2026-08-31 scaffold: `discovery.grid_mode` defaults `off`; gosom `-grid-bbox/-grid-cell` value formats
  need live verification before enabling grid tiling by default (part of U8.2).

## v0.1.1 (2026-08-31)
- gosom stall watchdog (v1.17.4 hangs after writing; salvage + captcha check preserved; `discovery.stall_s`)
- Registry DM auto-pick (ADR-010); OpenCorporates fixture tests
- WE SCORE account_fit profile: tech/departments/headcount/trigger detection (`enrich/profile.py`),
  0-100 rubric + A-D grades + contactability + data confidence + statuses (`score.py`), account columns in export
- Hooks column fixed (intake seeds default soft qualifiers); staleness flag implemented; version digest;
  export --format flag; social honest defaults (+[social] yt-dlp extra); CI matrix 3.11-3.14 + tag trigger
  + version-consistency job; doctor extras probe fixed (child interpreter)
- Deferred to v0.2: serve mode, grid tiling verification, real FB/IG probes, fallback_rest docker proof,
  claude plugin validate in CI, TSV dm format reconciliation (NDJSON canonical)

## v0.1.2 / v0.1.3 (2026-08-31 — shipped as commits, never tagged; see CHANGELOG retro entries)
- Call-ready sheets (registry covers site-less businesses, CH profile columns, Call Readiness),
  max_leads counts unique leads, LF_PROGRESS heartbeat + progress bar v2, `leadforge watch`,
  zero-blank-cell rule, sheet auto-open, headed browser debug mode.
- LESSON: all five version strings stayed at 0.1.1 through both — the CI versions job only checked
  they agreed with each other. v0.1.4 anchors the job to the pushed tag.

## v0.1.4 (2026-08-31, finish-for-good session — full detail in CHANGELOG [0.1.4])
- 34-agent adversarial audit (5 dimensions, every serious finding independently verified REAL);
  all confirmed findings fixed. Suite grew 121 -> 155 tests, ruff clean throughout.
- Headlines: browser fallback for HTTP-blocked sites (block-shaped statuses only, robots red line
  pinned by crawl()-level tests); honest coverage stats + natural DM names; geography actually
  scored + out_of_area implemented; word-bounded chain/competitor matching; resume un-wedged
  (stage 'enriching'), config set un-bricked, LF_DIGEST contract on all failure paths; Excel
  formula-injection + IllegalCharacterError fixes; -grid-bbox lat-first (was wrong-continent);
  TSV dm labels; rubric packaged into the wheel; doctor/social extras truth; version 0.1.4
  everywhere + tag-anchored CI + `claude plugin validate` job.
- INCIDENT (recorded per Rule 3): an audit agent injected a watched-fail robots-bypass mutation
  into the SHARED working tree; commit ff07dc6 captured it mid-experiment; corrected in 8489570
  before any push. Lesson: mutation experiments only in isolated worktrees.
- Deferred list after this session: grid tiling LIVE run (flag contract now verified offline),
  real FB/IG probes (ToS — deliberately excluded), fallback_rest docker proof (no docker here).
  Serve mode closed by design. plugin-validate-in-CI and TSV reconciliation SHIPPED.
- Live-run ops note (2026-08-31): the "leaked gosom" after the 709-lead autorepair run was NOT a
  watchdog failure — an idle no-args gosom web-UI instance had been double-clicked in Explorer
  (parent explorer.exe, 0.6s CPU). Watchdog behaved correctly all run.
