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

---

## POSITION — 2026-09-02, v0.3 BUILD IN PROGRESS (owner decisions taken; one-shot E2E + push requested)

**Owner decisions (binding, also in docs/09-v0.3-build-plan.md):** sending in scope with guardrails (ADR-011);
phone-first, email second touch; hybrid sender identity per client; NO paid dependencies (pluggable transport,
in-harness drafting, free domain resolution); freemail emailable after plausibility check; director names only
when gated; own-client ICP = anyone needing outreach except call centres (SIC 82200 excluded); full 10-city tiled
sweep in the background after the code ships.

**Wave 0 DONE — commit d7eb4ce** (202 tests, ruff clean): SCOPE #4 amended, ADR-011/012/013, db schema v2,
compliance.py, config sections, `--json` hoisting in cli.main, `outreach`/`draft` sub-app stubs, provider
field-map registry, interface stubs (classify_email_affinity, email_context, rank_email_contacts, name_similarity),
docs/09 build plan, tests/test_compliance.py + tests/test_db_v2.py.

**Wave 1 RUNNING — Workflow run `wf_ecc94138-118`** (5 builders A/B/C1/C2/D in isolated worktrees branched
from d7eb4ce, each reviewed by a fresh-context Fable reviewer, one fix round). Patches land at
`<scratchpad>/wave1/<unit>/<unit>.patch`; each builder commits on its worktree branch. NEXT: merge accepted
branches into master, run the full gate, then launch Wave 2 (E outreach, F drafting, H company mode), then
Wave 3 (docs, version 0.3.0 everywhere, CHANGELOG, STATE, tag, push), then data ops (DVSA load + tiled sweep).

**If resuming after a compaction:** `git worktree list` shows the builder worktrees; `git branch --list` the
unit branches; the workflow journal is at
`~/.claude/projects/D--GainLev-LeadForge-leadforge/bb93e453-b256-4096-9ee1-d9ef417ed812/subagents/workflows/wf_ecc94138-118/journal.jsonl`.

**Wave 1 MERGED into master (0be8074 + 53c61f3 cfg pass-through + 3159d0b package-data): 326 tests, ruff clean.**
Accepted: B (dvsa provider), C2 (runner/registry truth), C1 (extraction truth; test_crawler_politeness.py
exception granted). A and D merged WITH known reviewer findings, now being fixed in the polish round.
**RUNNING — Workflow `wf_0c94adb2-499`** (base 3159d0b): polish A/B/C1/C2/D + Wave 2 builds E (outreach)
and F (drafting), worktrees under `.claude/worktrees/wf_0c94adb2-499-*`, patches at `<scratchpad>/wave2/<unit>/`.
NEXT after it: merge, full gate, pass cfg to score_run call sites (cli.py:269, pipeline.py:258) once D adds the
param, launch H (company mode: providers/companies_house.py, resolve_domain.py, company.py, models.py Target.mode,
intake.py, grid.py dispatch), then Wave 3 docs/SKILL/CHANGELOG/STATE + tag v0.3.0 + push, then data ops.
Precomputed clean city bboxes for the sweep: `<scratchpad>/plan/area_bbox.yaml` (discovery.area_bbox format).

---

## POSITION — 2026-09-03, v0.3.0 RELEASED (all waves merged)

**Code:** master = v0.3.0 (Waves 0/1/polish/2 + register queries + docs). Gate `python scripts/v03_gate.py
--live-db <db>` = 19/19 PASS, full suite 518 passed, ruff clean, `claude plugin validate` OK. One flaky failure
observed once in a full run (did not reproduce in 4 subsequent runs; drafting builder saw the same once) —
UNPROVEN which test; watch CI.
**Lessons recorded:** the Workflow harness branches worktrees from ORIGIN/master (last pushed commit), not local
HEAD — push before launching worktree agents, and tell agents to `git merge-base --is-ancestor <base> HEAD ||
git reset --hard <base>`. An agent running `pip install -e .` inside a worktree re-points the editable install
of the MAIN tree at that worktree — re-run `pip install -e .[dev]` in the main tree after any wave. pytest here
needs `-o addopts=""` to print the summary line. Reviewer+fixer loops on Fable were ~half the token spend; the
deterministic gate script + orchestrator-applied small fixes replaced them for Wave 2 at ~1/3 the cost.
**Live campaign workspace prepared:** `campaign-uk-autorepair/leadforge.yaml` has grid_mode auto, providers
[gosom, dvsa], subdivide_at 100, area_bbox for all 10 cities (precomputed, settlement-level); `icp.yaml` caps
raised to 6000 leads / 6000 sites (backups: `*.pre-v0.3`). `leadforge plan` = 295 cells, 885 tiled queries
+ 10 register queries, ~59 h estimated (worst case with subdivision far higher). DVSA CSV cached in
`leadforge_data/cache/dvsa/`.
**NEXT:** the sweep runs detached (see below); when `leadforge status` shows `dm_pending`, label DMs
(`dm export`/`dm apply`), then `run --resume` to score + export; then `outreach plan` / `draft export` for the
eligible cohort, and a calling sprint with `outreach outcome add`. GainLev's own ICP: `config/icp.company.example.yaml`
(Stockport pilot) — needs `discovery.providers: [companies_house]` + the CH key in a fresh workspace.

---

## POSITION — 2026-09-03, SWEEP PAUSED; SPEED PROGRAM (owner: "not limited to gosom, build our own")

**Sweep paused by the owner** at 26/947 queries (16 Maps + 10 register; 0 degraded); resume later with
`run --resume` in the campaign folder if wanted. Audit of its output: 2,576 new businesses (1,862 DVSA register in
minutes; 714 Maps rows: phone 94%, rating 98%, postcode 98%, website 66%). Two real bugs found + fixed + pushed
(1913e87): phone merge only worked register->Maps (197 twins; `leadforge dedupe` repaired them, backup
`db.sqlite3.pre-dedupe-backup`), and DVSA facts never persisted (enrich_for was module-level; 2,162 rows backfilled).

**Measured (scratchpad/speed):** gosom c=2 28/min; c=8 46-61/min; c=16 63/min; 2 parallel c=8 = 71/min aggregate;
0 block signs in 12 min; gosom source: the result-list JSON already carries phone/website/rating/address/coords
(EntryFromJSON) but normal mode queues a PlaceJob for EVERY result. Native list-first Playwright probe: 120 cards in
25-28 s per search (phone 99%, website 70-76%, CID+lat/lng 100%) = 250-290 places/min per browser, no place visits.
Enrichment: 20-site samples 4.8-6.6 sites/min; tail-dominated (403/Cloudflare, dead hosts x 15 s timeouts, browser
Semaphore(2)); workers=16 alone did not help.

**IN FLIGHT (two Sonnet builders, separate worktrees):** `speed` branch — providers/maps_list.py (list-first,
persistent browser, known-CID skip, details only for new places, discovery.parallel_queries); `speed-enrich`
branch — fail-fast tail, browser gate 4, workers 12, overlapped registry/validate stages, DNS pool, bench_enrich.py.
NEXT: merge both, `scripts/bench_speed.py` end-to-end on a copy (target: 1,000 enriched+scored rows < 60 min),
then register-density adaptive tiling; gosom demoted to fallback. Research results: tasks/wnxprvm3w.output.

**Measured paces 2026-09-03 (scripts/bench_speed.py baseline, Nottingham, dvsa+maps_list, 2 browsers):**
discover 352 businesses in 46 s (0.13 s/item); enrich 40 sites in 200 s at 4 workers/6 pages (5.0 s/site ->
83 min per 1,000; the enrich builder on branch `speed-enrich` targets <= 2 s/site); registry 2.1 s/business ->
35 min per 1,000, bounded by Companies House 600 req/5 min (~2.5 calls/business: search+profile+officers; a
later cut = advanced-search by name+postcode returns status directly, saving the profile call); score+export
seconds. Fixed today on master: two-way phone merge + `dedupe` repair (1913e87), DVSA facts persisted, registry
jurisdiction gate now maps 'United Kingdom' -> GB (37c53b0; DVSA/maps_list rows were silently skipped before),
maps_list merged (ed61148, ADR-014). Live 2-browser proof: 4 queries -> 281 unique businesses in 141 s, 0 blocks.

**AFTER benchmark 2026-09-03 (master fdfd97a+, scripts/bench_speed.py --overlap, Nottingham, dvsa+maps_list, 2 browsers,
workers 12 / pages 4, real Companies House key):** discover 356 businesses in 48 s; enrich 60 sites + registry 356
lookups (210 matched active, 87 auto DMs) + gbp + validate OVERLAPPED in 588 s (registry-bound at ~1.65 s/business);
score+export < 1 s; TOTAL 636 s for a scored 356-row sheet. Projection for 1,000 businesses (~600 with websites):
discover 2-3 min, overlapped enrich/registry ~35 min (registry 28-35 min at the CH rate limit; crawl 23-35 min at
17-26 sites/min), score/export seconds -> ~40 min. The hour target holds on measured paces; the binding constraint is
Companies House's 600 req/5 min, not our code. Live sweep still PAUSED; to restart on the new engine set
`discovery.providers: [dvsa, maps_list, gosom]`, `discovery.parallel_queries: 2` in campaign leadforge.yaml and
`leadforge run --icp icp.yaml --resume` (885 tiled queries at ~25 s each on 2 browsers ~3 h + subdivision growth).
Dashboards: 8765 (campaign, paused), 8766 (benchmark). NEXT candidates: streaming pipeline (enrich as discovery
finds), register-density tiling, fewer CH calls per business (advanced-search by name+postcode returns status).

## POSITION — 2026-09-03 ~00:15, SWEEP RESUMED AFTER LEAD-CAP STOP (pid 16480)

The resumed sweep (pid 18760, new engine dvsa+maps_list+gosom, 2 browsers) hit `caps.max_leads: 6000` after 195
of 1,543 tiles (6,000 businesses credited to run_20260902_22032_7eca; 6,816 in the DB) and moved to enrichment with
1,348 tiles pending — the cap was a v0.2-era number. Fix shipped (tests 64 green, ruff clean):
- `ICP.icp_hash()` now EXCLUDES `caps` and `notes` (operational limits are not campaign identity);
  `ICP.icp_hash_legacy()` keeps the old value and `pipeline._latest_run()` falls back to it and re-stamps the row,
  so campaigns started by older versions keep resuming. `db.create_run` is collision-proof within one second.
- Live migration done by hand with the run STOPPED: `runs.icp_hash` 45eaca579771 -> 8108f1495954 for
  run_20260902_22032_7eca (scratchpad/sweep/restamp.py), THEN icp.yaml caps raised to max_leads 30000 /
  max_sites 30000 (backup `icp.yaml.pre-capraise`). `run --resume` re-entered discovery: 1,352 pending tiles,
  "6021 unique leads so far" at restart.
- Relaunch recipe (PowerShell): Start-Process C:\Python314\python.exe -ArgumentList -m,leadforge,run,--icp,icp.yaml,
  --resume,--json -WorkingDirectory <campaign> -RedirectStandardOutput leadforge_data\logs\sweep3_stdout.log
  -RedirectStandardError leadforge_data\logs\sweep3_stderr.log -WindowStyle Hidden -PassThru. Current pid 16480.
- Heartbeat: session cron every 5 min (2-59/5) running scratchpad/sweep/heartbeat.py (pid from sweep.pid next to it,
  read-only DB, dashboard 8765 ETA); it prints STOP_HEARTBEAT when finished/died and the tick deletes the job.
  Dashboards: 8765 campaign, 8766 bench-after2.
- After discovery the run continues into overlapped enrich/registry/gbp/validate, then pauses at dm_pending
  (agent DM labeling), then `run --resume` for score+export. Expected sheet 9,000-12,000 rows; registry stage
  (~1.65 s/business at the CH rate limit) is the long pole.
UNPROVEN: that the cap is not hit again (30,000 vs a 9-12k projection — should hold). NOT DONE: commit/push of the
hash change is pending the v0.3 gate (push only on gate exit 0).
- Dashboard fix (same commit): measured paces persist in `leadforge_data/pace.json`; DEFAULT_PACE_S enrich 2.5 s,
  registry 1.65 s. Campaign dashboard 8765 restarted on the new code (no --open). 8766 untouched.
