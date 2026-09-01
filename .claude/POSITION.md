# POSITION — where LeadForge actually is

Auto-injected at session start / compact / resume. The hook injects the **TAIL** of this file, so the
newest and most important block must stay at the BOTTOM. Canonical detail: `icm/STATE.md` (unit
status), `CHANGELOG.md` (what shipped).

---

## Background (2026-08-31, two releases shipped this day)

- **v0.1.4** — a 34-agent adversarial audit of the whole tool; every confirmed finding fixed (honest
  coverage stats, natural DM name order, geography actually scored, Excel formula-injection,
  resume/config-set/digest-contract criticals, `-grid-bbox` was lng-first = wrong continent).
- **v0.2.0** — grid tiling live-proven, browser fallback live-proven, opt-in inferred emails.

### Machine quirks that will bite otherwise
- VPN DNS (10.255.255.x) drops MX queries → `validate.get_resolver()` falls back to 8.8.8.8/1.1.1.1.
- Windows cp1252 pipes broke the LF_DIGEST contract → `cli.main()` forces UTF-8 on stdout/stderr.
- A no-args `google_maps_scraper.exe` double-clicked in Explorer starts an idle web-UI — looks like a
  leaked process but is not ours; the stall watchdog is fine.
- crawl4ai renders fine here (example.com in 0.54s); many UK garage sites 403 **both** httpx and
  headless Chromium — that is the site's WAF, correctly reported as "render returned nothing".
- Files are CRLF; a postwrite hook warns. Harmless, git normalizes on commit.

### Segment fact (why inferred emails do nothing for trades)
UK garages publish `info@` or nothing: **112 same-domain emails across 709 businesses, ZERO with a
`.` or `_` local part.** Professional services (Guildford accountancy campaign) DO carry
`first.last@` (`duncan.sweetland@`, `samantha.webber@`). So `validation.infer_emails` (opt-in,
anchored-only, never SMTP) correctly produces nothing on trade campaigns — by design, not by bug.

### UNPROVEN / deliberately excluded (do NOT re-litigate without a reason)
- `fallback_rest` docker up-path — no docker on this machine.
- Excel/LibreOffice visual check — operator-only, cannot be automated here.
- Facebook/Instagram presence probes — excluded on ToS grounds (`icm/SCOPE.md` #3).
- **No SMTP / RCPT probing, ever** (`icm/SCOPE.md` #5). An earlier plan draft proposed it; that was
  wrong and was withdrawn. Inference uses public evidence + MX only.

---

## CURRENT POSITION — 2026-08-31, end of session

**v0.2.0 SHIPPED. Nothing in flight, nothing blocked.** Working tree clean, all pushed to
https://github.com/AbdulrahmanAmer/leadforge, tagged `v0.2.0`, both CI runs (master + tag) **success**.
Gate: **192 tests pass · ruff clean · `claude plugin validate .` passes**.

**THE measurement that drives scaling:** Google Maps caps results at **~106–120 per search,
SERVER-SIDE**. `discovery.depth` is already maxed — scrolling/deepening does nothing. **Grid tiling is
the only fix and it is now proven live:** 4 tiled queries (`auto repair shop`, Birmingham) found
**107 businesses the entire 709-lead 3-category × 5-city campaign had missed** (Birmingham 221 → 310,
+40%), 0 degraded, **0 duplicate place_ids**. Tiling stays **opt-in**
(`leadforge config set discovery.grid_mode auto`) — it adds a Nominatim geocode per area and
multiplies query count 10–60×.

**Live campaign — `D:\GainLev\LeadForge\campaign-uk-autorepair\`:** 816 businesses (was 709), 274
Tier A, 419 named DMs, 194 published emails. Latest sheet:
`leadforge_data/exports_v2/run_20260831_13432_c75e/uk-autorepair-it-solutions.xlsx`. Pre-tiling DB
backup: `leadforge_data/db.sqlite3.pre-grid-backup`. Grid-test ICP left at `icp.gridtest.yaml`.
Second workspace: `D:\GainLev\LeadForge\campaign-uk-test\` (Guildford accountants, 15 leads).

**NEXT MOVE (agreed, not started):** a **full tiled sweep** — all 3 categories × 5 cities with
`grid_mode: auto`. On today's evidence that is several hundred more leads. Walk-away run: every query
checkpoints, `--resume` continues after any interruption. **Run `leadforge plan` FIRST** and read the
estimated hours — a tiled 3×5 plan is large (the plan digest now reports cells / queries / hours).
