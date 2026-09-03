# leadforge CLI reference (agent-facing)

Global: `--json` (digest line only) · `--data-dir PATH` (default `./leadforge_data`) · exit codes: 0 ok (warnings allowed), 1 bug,
3 env, 4 input, 5 provider-fatal.

Digest line (always last): `LF_DIGEST {"ok":bool,"cmd":str,"run":str|null,"counts":{...},"warnings":[...],"artifacts":[...],"next":str|null}`

| Command | Purpose | Key flags | Digest counts to watch |
|---|---|---|---|
| `doctor` | verify/install environment (pip deps, pinned gosom binary, optional extras, network, disk) | `--fix`, `--strict` | `checks`, `fixed`, `failed` |
| `intake` | compile + validate `answers.yaml` → `icp.yaml` | `--answers F`, `--out F` | `errors` (0 required) |
| `plan` | show run plan without scraping | `--icp F` | `tiles`, `queries`, `est_max_results` |
| `discover` | scrape listings → normalize → upsert | `--icp F`, `--limit N`, `--provider NAME` | `businesses`, `new`, `tiles_degraded` |
| `enrich` | crawl sites, extract + validate contacts, build DM candidates | `--limit N`, `--stage site\|validate` | `sites_crawled`, `contacts`, `dm_candidates`, `needs_browser` |
| `dm export` | write NDJSON batch of DM candidates for labeling | `--max N` (default 60), `--tsv`, `--out F` | `businesses`, `remaining`; artifact = batch path |
| `dm apply` | ingest labels | `--in F` | `applied`, `rejected`, `skipped` |
| `score` | rubric scoring + hooks | `--icp F` | `scored`, `tier_a/b/c`, `dq` |
| `export` | write XLSX + CSV + report.json | `--out DIR`, `--format xlsx,csv` | artifacts = file paths |
| `config set\|get KEY [VALUE]` | read/write one `leadforge.yaml` value (e.g. `registry.companies_house_key`) | — | counts.set |
| `doctor --fix --full` | first-time bootstrap: deps, scraper binary, quality extras (NER, browser, yt-dlp) | `--json` | counts.checks/fixed/failed |
| `version` | print version | — | digest only |
| `run` | orchestrated: plan→discover→enrich→(dm_pending)→score→export, resumable | `--icp F`, `--resume`, `--limit N`, `--skip-dm` | `stage`, everything above |
| `status` | current run snapshot | `--run ID` | `stage`, counts |
| `suppress add\|list` | opt-out list (domain/email/place_id) | value | `suppressed` |
| `dashboard` | local read-only status page: machine stages (discover/enrich/registry/validate) with measured pace + ETA, human/agent stages with item counts, every count behind them; `--open` opens the browser | `--port` (8765), `--open` | JSON at `/api/status` |
| `watch` | live bar for this workspace's run; pace and ETA from the feed's own clock (identical in every window) | — | prints a one-line summary on attach |
| `render-check URL` | diagnose one site: robots → plain fetch → browser → contacts | — | `emails`, `blocked`, `rendered` |
| `outreach identity add\|list` | sending identities (from name/email, postal address, privacy URL, opt-out channel) | `--label`, `--from-email`, … | `id`, `live_complete` |
| `outreach mailbox add\|list` | mailboxes that send for an identity; secrets by env var NAME only | `--identity`, `--address`, `--transport file\|smtp`, `--config k=ENV`, `--daily-cap` | `id` |
| `outreach plan` | enrol scored leads as outreach targets (entity type, lawful basis, suppression, chain dedupe) | `--campaign`, `--tier A,B`, `--identity`, `--limit`, `--client` | `enrolled`, `no_sendable_email`, `entity_gate`, `chain_duplicate`, `suppressed` |
| `draft export` | one evidence packet per enrolled target for you to draft from | `--campaign`, `--purpose`, `--max`, `--out`, `--run`/`--tier` (standalone) | `targets`, `grade_a/b/c`, `insufficient_evidence`; artifact = packet path |
| `draft apply` | ingest your drafts through the no-fabrication gate | `--in F`, `--packets F` | `drafted`, `rejected`, `abstained` |
| `draft render` / `draft check` | reviewable files / gate-only dry check | `--campaign`, `--out DIR` / `--in F` | counts |
| `outreach approve` | bind a human approval to each draft's content hash | `--campaign`, `--tier`/`--ids`/`--all-drafted`, `--approver` | `approved` |
| `outreach send` | **dry-run by default** (.eml files to the outbox); `--live` needs `outreach.armed` + `--i-am` | `--campaign`, `--dry-run`/`--live`, `--i-am`, `--mailbox`, `--max` | `would_send` / `sent`, `unknown`, `skipped_*`, `breaker_paused` |
| `outreach sync` | ingest bounces / complaints / unsubscribes / replies → suppression + states | — | `events`, `suppressed`, `replies` |
| `outreach status` | counts by state, caps consumed, breaker | `--campaign` | per-state counts |
| `outreach doctor` | SPF / DKIM / DMARC / MX / identity / warm-up checks, fails closed | `--identity` | per-check ok/FAIL |
| `outreach outcome add` | record a call/email result (feeds the outcome loop) | `--business`, `--channel`, `--result`, `--notes` | `outcomes` |

Notes
- `run` picks up an interrupted run for the same ICP hash automatically with `--resume`.
- `--limit N` caps NEW businesses processed this invocation — use for smoke tests (`--limit 10`); `caps.max_leads` is the
  per-run hard stop across resumes.
- `--json` may be written before or after the subcommand (v0.3); both spellings work.
- v0.3 discovery: `discovery.providers: [gosom, dvsa]` adds the DVSA MOT-station register as its own planned query per area
  (merged into Maps rows by phone); `discovery.grid_mode: auto` tiles areas and splits any tile that returns ≥ `subdivide_at`
  results; `discovery.area_bbox` bypasses the geocoder for a named area; `plan` reports `registry_queries`, `tiled_queries`,
  `est_runtime_min` and a worst-case subdivided estimate.
- v0.3 company mode (GainLev's own pipeline): `target.mode: company` + `sic_codes` in the ICP and `discovery.providers:
  [companies_house]` discover companies from Companies House by SIC × location; see `icp-guide.md`.
- speed unit (ADR-014): `discovery.providers: [maps_list]` uses the native list-first Maps engine (plain Playwright, one
  persistent browser, no place-page visits by default) instead of gosom's per-place subprocess crawl — measured 5-10x
  faster for the same list coverage; a maps_list row has no `place_id` (dedupe keys on CID instead, merged with any
  matching gosom row by phone) and lacks full postal address/hours unless `discovery.maps_list.visit_details: true`.
  `discovery.parallel_queries: N` (any provider) fans discover queries across N provider-chain instances (each its own
  browser/subprocess) instead of one query at a time.
- Optional extras change behavior when installed: `[browser]` auto-covers `needs_browser` sites; configured registry keys add
  officer cross-checks. Digest `warnings` tell you when an extra would have helped.
- Config file `leadforge.yaml` (workspace, optional): provider order, politeness knobs, proxies passthrough, registry keys, validation.staleness_days (drives the sheet's Stale? column). Write values with `leadforge config set <dotted.key> <value>`.
  Don't edit it unless the user asks; defaults are sane.
