# Stage 8 — Hardening & Release (PRESCRIPTIVE) — the ship gate

**Read `icm/SCOPE.md` first.** This stage runs the tool against real public listings and hardens it. Nothing
here requires evasion, credentials, or sending anything.

Prereqs: U3.6 done; U4.5/4.6/4.7 done or explicitly deferred (deferring is fine — tick them "skipped" in
`icm/STATE.md` with a one-line reason).

---

## U8.1 — Finish the test suite

Write these files. Each bullet is one test function; names are prescribed so nothing is ambiguous.

**`tests/test_crawler_politeness.py`** — proves the invariants that make the compliance posture real:
- `test_per_host_delay_is_honored`: spin up `http.server.ThreadingHTTPServer` on `127.0.0.1:0` serving a
  trivial page; set `cfg.politeness.delay_s = 1.0`; fetch the same host twice via `SiteCrawler._get`; assert
  elapsed ≥ 0.7s (jitter is ±30%).
- `test_robots_disallow_is_respected`: serve `/robots.txt` = `User-agent: *\nDisallow: /private`; assert
  `SiteCrawler._get(".../private")` returns `None` and that the server log recorded **no** request for
  `/private`.
- `test_pages_per_site_cap`: serve a home page linking to 20 "about/team/contact" pages; assert at most
  `cfg.crawl.pages_per_site` pages were fetched.

**`tests/test_suppression_e2e.py`** (also satisfies U8.4):
- `test_suppressed_domain_never_crawled_or_exported`: seed a business, `db.suppress(conn,"domain",…)`, run
  enrich + score + export, assert the domain appears in **no** exported row and `businesses_for_enrich`
  never returned it.

**`tests/test_cli_contract.py`** — extend using the offline mock pattern from `tests/test_pipeline_e2e.py`:
- one test per command (`discover`, `enrich`, `dm export`, `dm apply`, `score`, `export`, `run`, `status`,
  `suppress`) asserting **exactly one** `LF_DIGEST` line and that it parses with the required keys
  (`ok, cmd, run, counts, warnings, artifacts, next`).

**`tests/test_extract.py`** — add edge cases:
- HTML-entity-encoded emails (`&#109;ail&#64;x.com`), two `data-cfemail` spans on one page,
  a UK number parsed with `region="GB"`, and a page where the only "name" is a stopword (assert no candidate).

**Gate:** `pytest -q` green with materially more tests than the 59-test baseline; `ruff check src tests` clean.

---

## U8.2 — Live end-to-end validation ⭐ THE MOST IMPORTANT REMAINING STEP

This is the only step that exercises the real scraper. Do it on the operator's actual machine (Windows first).

### Step 1 — bootstrap
```bash
leadforge doctor --fix
```
Expect `[ok]`/`[fixed]` on every line. If `gosom-binary` fails, follow the hint in the digest (usually a
manual download of the named release asset into `leadforge_data/bin/`).

### Step 2 — smallest possible real campaign
Use ONE category and ONE small, specific town — you want real output fast, not volume:
```bash
cp config/icp.example.yaml answers.yaml
# edit: one category, a small town, country set, caps.max_leads ~25
leadforge intake --answers answers.yaml
leadforge run --icp icp.yaml --limit 15 --json
```

### Step 3 — reconcile the field map with reality ← the actual point of U8.2
The scaffold's `GOSOM_FIELD_MAP` (`src/leadforge/providers/gosom.py`) was written from the engine's README,
**not** from real bytes. Expect drift.

1. `ls -t leadforge_data/cache/gosom_*.json | head -1` → open it.
2. List the **actual** top-level keys of one record. Compare against `GOSOM_FIELD_MAP`.
3. Fix the map for anything renamed, nested, or missing (e.g. if `complete_address` is a string not a dict,
   handle both in `normalize.split_address`).
4. Copy 2–3 real records into `tests/fixtures/gosom_sample.ndjson`. Strip anything you don't want committed
   (this is public business data, but keep the file small).
5. Add `tests/test_providers.py::test_gosom_real_fixture_maps_to_business`: parse the fixture through
   `to_business()` and assert name, phone (E.164), website, city and `place_id` all populate.

**This converts the single riskiest assumption in the codebase into a covered fact.** Do not skip it.

### Step 4 — grid flags (only if you want geographic tiling)
`discovery.grid_mode` ships as `off`. Before switching it to `auto`, run the binary's `-h` and confirm the
exact spelling/format of the grid flags, then test one tiled query and compare result counts against the
plain text query. If the flags differ from `providers/gosom.py`, fix them and note it in `icm/STATE.md`.

### Step 5 — the DM loop, for real
```bash
leadforge dm export --max 20        # read the batch file
# label by hand into dm_labels.ndjson: {"biz":"...","pick":0,"confidence":0.9}
leadforge dm apply --in dm_labels.ndjson
leadforge run --icp icp.yaml --resume --json
```

### Step 6 — open the sheet (this is the G6 visual gate)
Open the exported `.xlsx` in **Excel on Windows** and in **LibreOffice**. Verify: autofilter works, the header
row is frozen, tier colors show, Website/Maps links click through, the **Summary** and **About** tabs read
correctly, and the CSV opens without mojibake (it is `utf-8-sig` for exactly this reason).

### Acceptance for U8.2
- [ ] ≥20 businesses in SQLite from a real run; ≥90% have a phone or a website; zero duplicate `place_id`.
- [ ] `GOSOM_FIELD_MAP` reflects reality and a real fixture test guards it.
- [ ] DM export → hand-label → apply → resume completes and DM columns populate in the sheet.
- [ ] Sheet verified visually in Excel and LibreOffice.
- [ ] Any drift or surprise recorded in the notes section of `icm/STATE.md`.
- [ ] Commit `U8.2: live validation + real gosom fixture`.

**If a run gets rate-limited or shows consent/captcha pages:** that is expected occasionally. The correct
response is to slow down (lower `discovery.concurrency`, raise delays, run fewer queries) or wait, and let the
pipeline mark those queries degraded and resume later. **Do not add captcha handling or evasion**
(`icm/SCOPE.md` #1) — if volume genuinely can't be reached politely, tell the operator; they can supply
proxies via config or narrow the campaign.

---

## U8.3 — CI

`.github/workflows/ci.yml` exists. Verify it runs on a push, then confirm it covers:
matrix `ubuntu-latest` + `windows-latest` × Python `3.11`, `3.12`; steps `pip install -e .[dev]` →
`ruff check src tests` → `pytest -q`. Every network-dependent test must stay skipped/mocked in CI — the suite
is already offline-only; keep it that way (a CI that scrapes the live web is both flaky and rude).

**Acceptance:** green run on both OSes; a deliberately broken test fails the build.

---

## U8.4 — Guardrails audit

Walk this checklist against the actual code and record pass/fail in `icm/STATE.md`:

1. robots.txt checked before every business-site fetch (incl. the browser path, if U4.5 landed).
2. One request in flight per host; per-host delay with jitter applied.
3. `caps.max_leads` / `max_sites` / `max_tiles` enforced as hard stops.
4. Suppression consulted at discover-upsert, enrich-queue, and export.
5. No SMTP RCPT probing anywhere (`grep -ri "rcpt\|smtplib" src/` → nothing).
6. No login/cookie/session handling anywhere (`grep -ri "cookie\|login\|session=" src/` → nothing relevant).
7. No LinkedIn scraping (`grep -ri "linkedin" src/` → only the social-link allowlist, which merely records a
   public profile URL found on the business's own site; it never fetches LinkedIn).
8. No evasion code: no captcha solving, fingerprint spoofing, or UA rotation-for-evasion.
9. `leadforge_data/` is gitignored — no scraped personal data in the repo.
10. Region compliance reminder appears on the exported Summary sheet.

Any failure is a bug; fix it before shipping.

---

## U8.5 — Tag & share

1. Update `CHANGELOG.md` with what landed since the scaffold.
2. Push to GitHub, then **actually test the install matrix from the repo URL** on the partner's harness:
   - Claude Code: `/plugin marketplace add <owner>/leadforge` → `/plugin install leadforge@leadforge`
   - Codex: `codex plugin marketplace add <owner>/leadforge` → install from `/plugins`
   - Then: `leadforge doctor --fix` and one small campaign, without editing any code.
   Fix whatever breaks — **this is the real acceptance test for the whole project.**
3. `git tag v0.1.0 && git push --tags`.

**Gate G8 (ship):** partner installs from the link and completes a campaign unaided; CI green; guardrail audit
clean; docs match behavior; tag pushed.
