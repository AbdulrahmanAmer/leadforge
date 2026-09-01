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

---

## POSITION — 2026-09-02, assessment session (no code changed; report published)

**Report:** https://claude.ai/code/artifact/12a2c6af-4482-4871-9cd5-88b53145c1dd (the owners' decision memo:
coverage, accuracy, sendable funnel, outreach + drafting design, own-client discovery, phased plan, decisions).

**Corrections to the block above (measured, not inferred):**
- The 709/816-lead campaign is **4 cities, not 5 or 10**. `queries` table: 12 of 30 done, **18 still `pending`**
  (Bristol, Leicester, Nottingham, Southampton, Reading, Coventry = 0 rows). Cause: the 1000 lead cap counted RAW
  listings (1,089 after query 12). Counting was fixed in v0.1.4 but the campaign data was never completed, and the
  run is `exported`, so `--resume` will NOT retry the 18 pending queries (only `degraded` ones, and only before the DM gate).
- **BLOCKED — tiled sweep — geocoder.** `leadforge plan` with `grid_mode=auto` fails: 'Manchester',
  'Manchester, Greater Manchester' and 'Manchester, England' are all rejected as ambiguous (Nominatim returns the same
  city twice; `grid._distinct_places` compares address labels BEFORE coordinates). 'City of Manchester' geocodes to
  the Manchester Ship Canal. Unblock = same-place rule (coords first), addresstype filter (city/town/borough), and a
  per-area bbox override. Also `leadforge plan` has no `--json` flag (SKILL.md says it does).
- Tiled sweep size (computed with make_tiles on Nominatim bboxes): 298 tiles under max_tiles=60/area → 894 queries
  ≈ 52 h at the MEASURED 3.5 min/tiled query (tool's `_EST_MIN_PER_QUERY=8` says 119 h; untiled measured 1.7 min).
  Uncapped 3 km = 959 tiles ≈ 168 h. Grid-test tiles at ~10 km returned 110/100 → still saturated; split any tile ≥100.
- **gosom v1.17.4 is the LATEST release (2026-08-22)** — no upgrade exists. `-fast-mode` = ≤21 results, no grid.
- **Universe denominators:** DVSA "Active MOT test stations" CSV (OGL, quarterly, 23,087 rows, 23,085 with phone):
  2,535 stations across the 10 cities vs 244 MOT-tagged found; in the 4 covered cities 1,045 stations, **317 phone-
  matched to our DB, 717 absent**. Companies House SIC 45200 active: 7,518 across the 10 cities vs 816 found.
  CSV cached at the session scratchpad `dvsa/active-mot-stations.csv`; URL in the report's Method section.
- **Raw gosom cache holds GBP fields we discard** (848 unique places): `about` attributes (Appointments recommended
  629 / required 134), `order_online` links (139), owner review replies (358; 75 signed with a first name), a first
  name credited in ≥3 reviews (76), `status`. `-extra-reviews` is off (8 reviews/place).

**Accuracy defects confirmed by adversarial re-measurement (details + fixes in the report):**
7 junk freemail emails tiered valid/personal (impallari@gmail.com ×3 font credit, test@/sample@ builder script), 3 tier-A
rows where junk outranked info@own-domain; registry match has no name check → 7–10% wrong company; 40/187 profiled
matches DISSOLVED (26 with DM installed, 13 tier A); registry_profile persisted on only 187 (crawl path discards it →
"not matched" on ~280 matched rows); 42% of heuristic people candidates are review authors/garbage; hook
weak_social_presence fires on 630 with 473 zero-evidence (115 'phantom' crawls stamped crawled_at with 0 pages);
booking regex false on Halfords/Hillcliffe(top lead)/Master MOT; industry_match gives 0.1 to 404 rows (Google category
strings) — fixing it alone makes A ≈ 58% → recalibrate thresholds; 3 factors (30 pts) constant; 28 tier-A rows have no
email and no DM. Sendable funnel today: 816 → 473 site → 194 email → 112 own-domain → 98 active-Ltd+email → 45 A+DM+own-domain.

**Scope note:** icm/SCOPE.md #4 (no sending) is still in force. The owner is CONSIDERING reversing it; nothing may be
built past a dry-run/render step until SCOPE.md is amended in writing with an ADR. Transactional ESPs ban cold outreach.

**NEXT MOVE (proposed, awaiting owner decisions listed in the report §8):** Phase 0 = geocoder fix + tile
subdivision + full 10-city tiled sweep; DVSA + Companies House SIC as discovery providers merged by phone; keep the GBP
fields; ship the precision fixes as one release. Then a phone-first outcome loop before any sending layer.
