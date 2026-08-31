# Build State Tracker

Single source of truth for what's done vs left. Tick boxes as you complete units. Baseline shipped by the
scaffolding session on 2026-08-31: **45 passed, 1 xfailed**, ruff clean.

## Stage gates

- [x] **G0 Foundations** — package installs, doctor works, smoke tests green
- [x] **G1 Contracts & data model** — models ↔ DDL ↔ docs/03 aligned; merge/dedupe tested
- [x] **G2 Intake & ICP** — compiler validates + deterministic hash; example ICPs compile
- [~] **G3 Discovery** — gosom adapter + normalizer + planner done & tested; *fallback (U3.6) + live run (U8.2) pending*
- [~] **G4 Enrichment** — crawler/extractors/validators/DM-loop done & tested; *browser (U4.5), registries (U4.6), NER (U4.7) pending*
- [x] **G5 Scoring** — rubric + hooks + explanations; deterministic, tested
- [x] **G6 Export** — XLSX/CSV/report; produced in E2E test (verify visually on a real machine at G6/U8.2)
- [x] **G7 Harness integration** — both plugins + skill + AGENTS/CLAUDE + install.py present (validate live at U8.2)
- [ ] **G8 Ship** — full test suite, CI green, live E2E, guardrails audit, tag v0.1.0

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
- [~] **U3.7** gosom serve mode (optional) — SKIPPED: multi-hour runs not needed yet (stage-3 doc says skip unless needed)
- [x] **U4.5** Browser escalation — implemented + wiring tests; live render UNPROVEN ([browser] extra not installed here)
- [x] **U4.6** Registry cross-check — implemented + fixture tests; live key path UNPROVEN (no keys configured)
- [x] **U4.7** GLiNER DM upgrade — hook + selection rule + tests (skip cleanly without [ner])
- [x] **U4.8** Social/video presence — implemented (youtube via yt-dlp; others honest 'unknown'), linkedin-exclusion tested
- [x] **U8.1** Finish test suite — 89 tests: politeness (real server), suppression e2e, digest contract per command, extractor edges
- [ ] **U8.2** Live E2E validation (needs network + binary) — `icm/stages/stage-8-hardening.md`
- [ ] **U8.3** CI verify/extend — `.github/workflows/ci.yml` — `icm/stages/stage-8-hardening.md`
- [ ] **U8.4** Guardrails audit — `icm/stages/stage-8-hardening.md`
- [ ] **U8.5** Tag v0.1.0 + verify install matrix + push — `icm/stages/stage-8-hardening.md`

## Notes / decisions log (append as you go)
- 2026-08-31 scaffold: gosom FIELD_MAP derived from README, NOT a live run — verify at U8.2 (expect drift).
- 2026-08-31 finalize: this machine's VPN DNS (10.255.255.x) drops MX queries — added `get_resolver()`
  fallback (system → 8.8.8.8/1.1.1.1, probed once per process) in `enrich/validate.py`, used by doctor too.
- 2026-08-31 finalize: CLI now forces UTF-8 stdout/stderr in `main()` (Windows cp1252 pipes broke the digest contract).
- 2026-08-31 scaffold: `discovery.grid_mode` defaults `off`; gosom `-grid-bbox/-grid-cell` value formats
  need live verification before enabling grid tiling by default (part of U8.2).
