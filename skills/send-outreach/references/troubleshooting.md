# Troubleshooting (agent playbook)

Match the digest warning/error → do the fix → retry ONCE → else report to user with the digest line.

| Symptom | Fix |
|---|---|
| `leadforge: command not found` | `pip install -e <repo-root>`; if pip missing → `python -m pip install -e <repo-root>`; then retry. Windows PowerShell may need a new shell for PATH refresh — use `python -m leadforge ...` as the no-PATH fallback. |
| doctor: `binary download failed` | Network/proxy issue or GitHub rate limit. Retry once. Still failing → ask user to download the release asset named in the message into `leadforge_data/bin/` manually (URL is in the digest), then `leadforge doctor`. |
| doctor: `python < 3.11` | Tell the user; do not attempt OS-level python installs without their say-so. |
| discover: `tiles_degraded` high / captcha cooldown warnings | Normal under load. Re-run `leadforge run --resume` later; suggest lowering scope (fewer categories/areas) or adding proxies in `leadforge.yaml` (`discovery.proxies`) if the user has them. Never hammer retries. |
| discover: 0 businesses | Check category phrasing (Maps-style singular, e.g. "auto repair shop" not "car mechanics near me"); check area spelling; run `leadforge plan` and sanity-check tiles>0. |
| enrich: `needs_browser` large | Offer: `pip install -e .[browser]` then `leadforge enrich --stage site --json` to re-cover those sites. Optional — run completes without it. |
| enrich: many `tier=unknown` emails | DNS flaky. `leadforge enrich --stage validate --json` re-runs validation only. |
| dm export: `businesses:0` but user expected DMs | Sites likely list no people (common for tiny SMBs). The sheet still ships with role emails/phones; tell the user DM coverage % from the digest. |
| export: file locked (Windows) | The XLSX is open in Excel. Ask user to close it, re-run `leadforge export`. |
| `ok:false` with traceback pointer | Read nothing else; report the digest + log path to the user; suggest `leadforge doctor --strict`. Do NOT paste the log. |
| Codex marketplace add fails on `.agents/plugins/marketplace.json` | Use `codex plugin marketplace add AbdulrahmanAmer/leadforge` (repo-level), or the bridge: `npx skills add AbdulrahmanAmer/leadforge` / `python install.py`. |
| First `leadforge` call via the plugin launcher (`bin/leadforge`) takes minutes | Expected once per plugin version: it creates a venv and `pip install`s the package. The pinned gosom scraper binary itself is ~61 MB and downloads on first `doctor --fix`/`run` — slow on a cold connection, one-time (cached under the venv's data dir). Tell the user this is a one-off; do not retry mid-download. |
| `doctor` reports no GPU / NER falls back to CPU | Not an error — `enrich.ner_device: auto` picks CPU when no CUDA device is present; NER (person/title extraction) still runs, just slower (measured: ~4x slower per page on this project's benchmark machine). Never required for a run to complete. |
| torch/NER install is large or slow | Only pulled by `doctor --fix --full` (or the `[ner]` extra) — hundreds of MB. Always state the size and get consent before running `--full`; plain `doctor --fix` never installs it. |
| `needs_browser` sites, or HTTP 403 on a site's home page | Crawl4ai/Playwright (`[browser]` extra) render JS-heavy or bot-walled sites; 403s are routed to the browser fallback automatically once installed. Offer: `pip install -e .[browser]` (or `doctor --fix --full`), then `leadforge enrich --stage site --json` to re-cover those sites. Optional — a run completes without it, just with fewer contacts from those sites. Never attempt to bypass a genuine WAF/anti-bot wall yourself (scope red line) — some 403s are real and stay unresolved. |
| link-aggregator sites (linktr.ee, w3w.co, beacons.ai, linkin.bio, lnk.bio) as a business's "website" | Handled automatically — the crawler follows the first non-social outbound link, or records `error="aggregator"` if none is found. Not user-actionable. |
| `sqlite3.OperationalError: database is locked` | Another `leadforge` process (often a `--detach`'d run) has the workspace's DB open. Check `leadforge status --json` for `alive:true` before starting a second command against the same workspace; wait for it or use a different workspace directory. Never delete the `.db` file to "fix" this. |
| Resuming a run found by `status` in a new session | Never restart from `intake`/`run` fresh. `leadforge status --json` (no `--run`) shows the latest run for this workspace; if `stage` isn't `exported` and (for a detached run) `alive` is false, resume it: `leadforge run --icp icp.yaml --resume --json`, or force a stage first with `--resume --from-stage <stage>` if the digest names one. Every query and every crawled site checkpoints, so nothing already done is repeated. |
| `outreach doctor` reports SPF/DKIM/DMARC/MX failing | These are DNS records on the *sending domain*, not something LeadForge can fix — tell the user which record failed and point them to their DNS provider (SPF: a `v=spf1` TXT record including their SMTP relay; DKIM: a selector TXT record matching `--config dkim_selector`; DMARC: a `_dmarc` TXT record; MX: the domain must resolve mail exchangers). Do not enrol/send until all checks are `ok`. |
| `outreach doctor` warm-up check failing on a brand-new mailbox | Expected — a fresh mailbox has no sending history. Tell the user warm-up is a ramp (start low-volume, `outreach send --max N` small), not something to skip by force. |

Smoke test any suspicion cheaply: `leadforge run --icp icp.yaml --limit 10 --json`.
