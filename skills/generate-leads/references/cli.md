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
| `run` | orchestrated: plan→discover→enrich→(dm_pending)→score→export, resumable | `--icp F`, `--resume`, `--limit N`, `--skip-dm` | `stage`, everything above |
| `status` | current run snapshot | `--run ID` | `stage`, counts |
| `suppress add\|list` | opt-out list (domain/email/place_id) | value | `suppressed` |

Notes
- `run` picks up an interrupted run for the same ICP hash automatically with `--resume`.
- `--limit N` caps businesses processed this invocation — use for smoke tests (`--limit 10`).
- Optional extras change behavior when installed: `[browser]` auto-covers `needs_browser` sites; `[registry]` + configured keys adds
  officer cross-checks. Digest `warnings` tell you when an extra would have helped.
- Config file `leadforge.yaml` (workspace, optional): provider order, politeness knobs, proxies passthrough, registry keys, staleness_days.
  Don't edit it unless the user asks; defaults are sane.
