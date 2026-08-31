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

Smoke test any suspicion cheaply: `leadforge run --icp icp.yaml --limit 10 --json`.
