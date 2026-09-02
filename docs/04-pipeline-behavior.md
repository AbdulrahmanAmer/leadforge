# Pipeline Behavior — end to end

> 🖼️ **Rendered images:** [`diagrams/03-pipeline-sequence.png`](diagrams/03-pipeline-sequence.png) (the timeline)
> and [`diagrams/04-run-states.png`](diagrams/04-run-states.png) (the state machine).

![Pipeline sequence](diagrams/03-pipeline-sequence.png)

![Run state machine](diagrams/04-run-states.png)

## 1. Full-run sequence

```mermaid
sequenceDiagram
    autonumber
    actor U as Operator
    participant A as Agent (skill)
    participant C as leadforge CLI
    participant G as gosom binary
    participant W as Business websites
    participant D as SQLite

    U->>A: "find me leads" (+ answers to intake Qs)
    A->>C: leadforge doctor --fix --json
    C-->>A: LF_DIGEST ok (installed: gosom v1.17.4)
    A->>C: leadforge intake --answers answers.yaml
    C-->>A: LF_DIGEST ok (icp.yaml written, 3 cats, 1 area)
    A->>C: leadforge run --icp icp.yaml
    C->>C: plan: geocode area, build grid tiles + query variants
    C->>G: subprocess -json -input queries -depth -c 2 [-proxies]
    G->>G: headless browser: search each tile, scroll, consent-reject
    G-->>C: NDJSON listings (36 fields)
    C->>D: normalize → upsert businesses (dedupe place_id)
    loop each business with website (≤ caps, politeness)
        C->>W: GET home/about/team/contact/impressum (robots-aware)
        W-->>C: HTML
        C->>C: extract emails/phones/socials/person-title snippets
        C->>D: contacts + people(candidates) + evidence
    end
    C->>C: validate contacts (MX, disposable, phone)
    C-->>A: LF_DIGEST stage=dm_pending (42 businesses need DM label)
    A->>C: leadforge dm export --max 60
    C-->>A: dm_batch.ndjson (≤300-char snippets)
    A->>A: label DMs in ONE pass (no browsing)
    A->>C: leadforge dm apply --in dm_labels.ndjson
    C-->>A: LF_DIGEST ok (38 DMs set, 4 rejected)
    A->>C: leadforge run --icp icp.yaml --resume
    C->>C: score (rubric + hooks) → export XLSX/CSV
    C-->>A: LF_DIGEST stage=exported (leads=142 A=38 B=71 C=33, sheet path)
    A-->>U: summary + sheet location (never pastes the sheet)
```

## 2. Run state machine

```mermaid
stateDiagram-v2
    [*] --> planned : run created (icp hash pinned)
    planned --> discovering : discover
    discovering --> discovered : all tiles done/degraded
    discovered --> enriching : enrich
    enriching --> enriched : site queue drained
    enriched --> dm_pending : DM candidates exist
    enriched --> scoring : --no-dm or none found
    dm_pending --> scoring : dm apply received / --skip-dm
    scoring --> scored
    scored --> exported : export written
    discovering --> failed : fatal (no provider usable)
    enriching --> failed : fatal (db corrupt etc.)
    exported --> [*]
    note right of dm_pending : the ONLY stage that\nwaits on the agent
    note right of discovering : checkpoints per tile →\nresume skips completed tiles
```

`leadforge run` is a **resumable orchestrator**: it reads the run's stage from SQLite and continues from there. `--resume` picks the
latest run for the same ICP hash; every stage is idempotent (upserts, per-tile / per-site checkpoints).

## 3. Stage specs

### 3.1 plan (inside `run`, or `leadforge plan`)
- Geocode each `geography.areas[]` via Nominatim (`https://nominatim.openstreetmap.org/search`, 1 req/s, cached forever in
  `leadforge_data/cache/geocode.json`, UA identifies the tool).
- Build grid: bbox split into tiles of `grid_cell_km` (default 3 km, auto-shrink for dense urban categories) — capped by `caps.max_tiles`.
- Query plan = tiles × category variants. Estimate shown in digest (`tiles`, `queries`, `est_max_results` = queries × 120).

### 3.2 discover
- Provider order from config (`providers.discovery: [gosom, fallback_rest]`). Per query: provider `fetch()` with retry/backoff
  (tenacity: 3 tries, expo 2–30 s).
- gosom invocation (subprocess mode): input file of queries (one per line, gosom `-input`), flags:
  `-json -results <out.ndjson> -depth <cfg> -c <cfg,default 2> -lang <cfg> [-proxies <cfg>] -exit-on-inactivity 3m`; grid runs use
  `-grid-bbox`/`-grid-cell`/`-zoom` per tile instead of plain text queries. Timeout per batch: `discovery.timeout_min` (default 30).
- Output NDJSON parsed → `RawListing` → `normalize.to_business()` → upsert (dedupe). Tile marked done + result_count.
- Degradations: empty tile after retries → `degraded`; captcha signature in stderr → global cooldown (default 10 min) then halve
  concurrency; binary crash → try fallback provider for remaining tiles.

### 3.3 enrich
- Site queue = businesses with `domain`, not suppressed, ordered by ICP fit potential (category match first), capped by `caps.max_sites`.
  With the `[browser]` extra installed, sites a previous pass flagged `needs_browser` re-enter the queue for their rendered retry.
- Per site (politeness): robots.txt fetch/parse (cached); skip disallowed paths; per-host delay `politeness.delay_s` (default 2.0, jitter
  ±30%); global concurrency `politeness.workers` (default 4 hosts in parallel, 1 request in flight per host); UA
  `LeadForgeBot/<ver> (internal lead research; +contact-in-repo)`; hard per-page timeout 15 s; max `crawl.pages_per_site` (default 6).
- Page selection: `/` always; then discovered nav links matching (about|team|staff|people|leadership|contact|impress|imprint|legal
  |meet) up to cap; sitemap.xml probe if nav yields < 2 targets.
- Extraction per page (`extract.py`): mailto + email regex + cfemail decode + at/dot normalization; tel: links + phone regex →
  phonenumbers; social links (allowlist hosts); copyright-year → `stale_site` signal if < current_year − 2; person+title candidates:
  lines/headings where a Title keyword (Owner, CEO, Founder, GM, Manager, Director, Principal, Broker, …) sits within 60 chars of a
  Capitalized-Name pattern → snippet (≤300 chars) + source URL stored on `people`.
- JS-shell detection: extracted text < 400 chars AND `<script src=` count high → if extra `[browser]` installed, re-fetch via crawl4ai;
  else mark `needs_browser` (surfaces in digest warnings + Summary).
- Bot-wall fallback (v0.1.4): a site that refuses the plain HTTP client with a block-shaped status (401/403/405/406/429/503) is retried
  once with a real rendered browser — other failures (plain 5xx, timeouts) are not. **Robots-disallowed sites never escalate**: the site
  said no, so the browser must not go either. An unreachable/5xx robots.txt counts as complete disallow per RFC 9309 §2.3.1.4; a 4xx
  robots.txt means none published → allow.
- validate (`validate.py`): emails — syntax (email-validator) → MX (dnspython, 5 s timeout, per-domain cache) → disposable list → role
  classification → tier; phones — `phonenumbers.is_valid_number`; site liveness recorded (status, elapsed).

### 3.4 dm loop (agent-in-the-loop)
- `dm export`: businesses with ≥1 person candidate and no accepted DM → NDJSON, one line per business:
  `{"biz":"biz_ab12","name":"…","category":"…","icp_titles":["Owner",…],"candidates":[{"i":0,"name":"…","title":"…","snippet":"…"}]}` —
  capped `--max` (default 60 businesses/batch).
- Agent labels **from snippets only** (no browsing) → `dm_labels.ndjson`:
  `{"biz":"biz_ab12","pick":0,"confidence":0.9,"title_override":null}` or `{"biz":"…","pick":-1}` (none credible).
- `dm apply`: sets `people.is_dm`, confidence, labeled_by=agent; unlabeled businesses simply export without DM (never blocks the run).
- Registry cross-check: see §3.4b — adds `people` rows labeled_by=registry with evidence; boosts `data_confidence` factor.

### 3.4b registry stage (v0.1.2)
- Runs for **every** business in a covered jurisdiction — including site-less ones, which never enter the crawl stage (where lookups
  used to happen). Silent no-op without a configured key. Run alone via `leadforge enrich --stage registry`
  (`--stage` accepts all|site|registry|validate).
- Companies House (GB/UK; key via `leadforge config set registry.companies_house_key …`): company search by name, candidates filtered
  by locality match against the business address; stores the matched **company profile** (number, incorporation date, status, SIC
  codes) in `enrich_json.registry_profile` → the sheet's Company No / Incorporated / Company Status / SIC Codes columns; then
  `/company/{n}/officers` → active (non-resigned) officers as `people` rows labeled_by=registry, each with evidence.
- Auto-DM (ADR-010): exactly ONE active individual director (corporate officers filtered out) and no DM already chosen → auto-marked DM
  at confidence 0.9 — official-registry evidence beats agent inference, so big runs don't queue the obvious cases. 0 or 2+ individuals
  stay with the agent.
- OpenCorporates (token required) is the fallback for its jurisdictions (GB/US). On a 429 a registry backs off 60 s once, then disables
  itself and the stage stops rather than hammering the rate limit. Every looked-up business is stamped `registry_checked` so re-runs
  never repeat lookups.
- Heartbeat: the `LF_PROGRESS` stream carries `registry` and `validate` stages alongside `discover`/`enrich` (docs/06).

### 3.4c infer stage (v0.2.0, opt-in — `validation.infer_emails`, default off)
- Runs after the registry stage (so registry-auto-picked DMs are covered); re-run alone with
  `leadforge enrich --stage infer` after `dm apply` to cover agent-labeled DMs too.
- Fires only when **all three** hold: the business has a DM with a usable person name, its domain's **MX confirms it
  receives mail**, and a **real personal email already found on that same domain** demonstrates the local-part
  convention (`bob.jones@` → first.last, `b.jones@` → f.last). **No anchor → no guess** — a common pattern is never
  assumed. Role mailboxes (`info@`), departmental lookalikes (`experienced.hire@`, `new.business@`) and freemail
  domains are rejected as anchors.
- **No SMTP, ever** (icm/SCOPE.md #5): evidence is DNS MX + already-crawled emails. Nothing contacts a mail server.
- Output: a contact with tier `inferred`, its own `email_inferred` evidence (no source URL is claimed), and its own
  `Email (Inferred)` sheet column reading `addr (likely, N% — pattern X from <anchor>)`. Excluded from `with_email`
  coverage (reported separately as `with_inferred_email`); scored below any observed address.
- Segment reality (measured 2026-08-31): trades publish `info@` or nothing — 0 anchors across 709 UK garages;
  professional-services campaigns do carry `first.last@` conventions. The stage correctly produces nothing where
  there is no evidence.

### 3.5 score
- Load `src/leadforge/data/scoring.default.yaml` (packaged, via importlib.resources) ⊕ `icp.scoring.weights_override`. For each business: evaluate factor functions (pure, unit-tested)
  → points + one-line `why`; sum, clamp 0–100; apply negative rules (cap −40); tier A ≥ 75 / B ≥ 55 / C else / DQ if any hard qualifier
  hit. Need-hooks: rule table (e.g. `website_missing` → "No website — pitch full build + booking"), ranked, top hook exported.

### 3.6 export
- XLSX via openpyxl (Leads / Summary / About sheets per `docs/03` §5): frozen header, autofilter, tier conditional fill, hyperlinks,
  column widths, zebra rows. CSV mirror `utf-8-sig`. JSON run report → `leadforge_data/exports/<run>/report.json`.
- **Zero blank cells** (v0.1.2): a cell is never empty — placeholder text says why it would have been ("none published",
  "not matched in registry", …); anything still empty is written as `-`. Summary/report coverage counts only real data, never
  placeholders.
- **Call Readiness** (derived per row): `READY - named contact` (validated phone + DM) / `READY - ask switchboard` (validated phone,
  no DM) / `UNVERIFIED PHONE - confirm number` (only a raw unparsed number) / `NO PHONE - research first`.
- Digest artifacts list absolute paths. Nothing is printed from the sheet itself.

## 3b. v0.3 behaviour changes (truth + coverage; decided 2026-09-02, docs/09)

### 3b.1 Discovery
- **Registry-first providers.** `discovery.providers` may list `dvsa` (the DVSA "Active MOT test stations" CSV,
  OGL, cached 90 days under `leadforge_data/cache/dvsa/`, filtered by the query's town) and, in company mode,
  `companies_house` (advanced search by SIC × location). Each provider registers its own raw-field map
  (`providers/base.py`); `normalize.to_business` dispatches on `RawListing.provider`. A registry row with no
  `place_id` **merges into the Maps row that carries the same E.164 phone** when a name token or postcode
  district agrees (`db.upsert_business`); otherwise it becomes its own row with `source=dvsa`.
- **Geocoder.** Nominatim is queried with `featureType=settlement` first (bare city names resolve to the city,
  not a bus stop, a hospital or the county) and retried once without it after the 1 req/s delay; two hits within
  0.15° whose boxes overlap are the same place, never "ambiguous"; waterways/amenities never beat a city row;
  `discovery.area_bbox["<area>"] = [minLng, minLat, maxLng, maxLat]` bypasses Nominatim entirely. Every geocode
  error message names that override.
- **Saturation subdivision.** A tiled query returning ≥ `discovery.subdivide_at` (default 100) listings is split
  into 4 quadrant children (`tile_json.depth + 1`) persisted **before** the parent is marked done, up to
  `discovery.max_subdivisions` (default 2); children are picked up by the same loop and by `--resume`; a
  resumed run never duplicates children. `leadforge plan` reports `est_runtime_min` from the measured per-query
  constants (`discovery.est_min_per_query` / `est_min_per_tiled_query`) and, for tiled plans, the worst case
  if every generation subdivided.
- **Resume completes discovery.** `run --resume` re-enters discovery from **any** stage while the latest run
  has `pending` or `degraded` queries (the 2026-08-31 campaign was `exported` with 18 pending) — except when
  the run stopped on its lead cap and the businesses credited to it still fill the cap. `caps.max_leads` is a
  per-run stop across resumes (credited count seeds `processed`); `--limit N` means "at most N new this
  invocation".
- **Google Business Profile facts kept.** `businesses.enrich_json.gbp` holds appointment attributes, booking
  links (`order_online`), owner reply signatures (reviewer echoes rejected), review-credited first names,
  `status` (unreliable in gosom v1.17.4 — never drives a hook), description.

### 3b.2 Enrichment
- `crawled_at` is stamped **only** when the crawl succeeded; failures record `attempted_at` + `error`
  (robots-disallowed, unreachable, HTTP code). A 0-page crawl can therefore no longer look "crawled".
- Emails: `<style>/<script>/<noscript>/<template>` content is stripped before regex extraction; placeholder
  local parts (`test`, `sample`, `noreply`…) are invalid regardless of MX; each address is classified
  `own_domain | freemail_linked | freemail_unlinked | foreign` (`extract.classify_email_affinity`) — foreign is
  dropped, unlinked freemail is stored `risky`, and the evidence row keeps a ±90-char context window with
  `ref_id` pointing at the contact. Best-email ranking (`validate.rank_email_contacts`): own-domain valid >
  own-domain role > linked freemail valid > inferred > risky > unknown > invalid.
- People: candidates inside review/testimonial context or form labels are skipped; `people.origin` records
  heuristic | registry | gbp and survives agent labeling; `dm export` shows it.
- Registry: a Companies House hit must pass `name_similarity ≥ registry.min_name_similarity` **and** locality
  overlap, and be `active` when `registry.active_only`; the profile (`company_number, legal_name, company_status,
  incorporated, sic_codes, match_similarity`) is persisted on both the crawl path and the site-less path.
  Auto-pick (ADR-010) inherits both gates.
- Signals: `booking_hint` reads nav anchors from HTML (not trafilatura text), knows the UK booking platforms,
  and is set from a GBP booking link (`booking_source=gbp`); `final_host` / `offsite_redirect` / `http_status`
  / `phone_confirmed` are recorded. A `gbp` stage adds owner-reply names for site-less businesses.

### 3b.3 Scoring and export
- **Fit and contactability are separate.** `total`/tier come from fit only (industry 25 via
  `data/category_aliases.yaml`, need 25, size 10, geography 10, business_model 10, data_confidence 20; A ≥ 80,
  B ≥ 60). `contactability` (0–100: DM, best-email class, validated phone, registry, phone_confirmed, mobile)
  and `status` (READY | CALL_ONLY | RESEARCH | DQ — phone-first: a callable row is never RESEARCH) are meta
  factors. Profiles are pluggable (`score.register_profile`; `company` is added by company mode).
- Hooks fire only on observed evidence: `crawled_at` set **and** pages > 0 **and** the signal key present;
  templates say only what was seen; all hooks are exported.
- New columns: Fit, Contactability, Status, **Next Action** (phone-first, or the outreach state when a lead is
  enrolled), Entity Type, Lawful Basis (Email), Registry Name, Registry Match, Chain (shared domain/phone),
  Site Status (live / redirects / not crawlable (robots) / dead / unreachable / not crawled), Email Confidence,
  All Hooks. The Summary sheet shows the funnel (site → any email → own-domain → eligible → call-ready).

## 4. Error taxonomy

| Class | Examples | Behavior | Exit code |
|---|---|---|---|
| `EnvError` | missing dep, no binary, no network | doctor message + fix hint in digest | 3 |
| `InputError` | invalid answers.yaml/icp.yaml | field-level errors in digest | 4 |
| `ProviderDegraded` | captcha, empty tiles, timeouts | recorded, run continues | 0 (warnings) |
| `ProviderFailed` | all providers dead | run → failed, partial data kept | 5 |
| `PipelineBug` | unexpected exception | traceback → `leadforge_data/logs/`, digest ok=false | 1 |

## 5. Politeness & safety invariants (enforced in code)

1. Business-site crawl **always** checks robots.txt and per-host delay; no more than 1 in-flight request per host.
2. Global caps from ICP (`max_leads`, `max_sites`, `max_tiles`) are hard stops.
3. Suppression list consulted at discover-upsert, enrich-queue, and export.
4. Maps scraping runs at conservative defaults (`-c 2`, depth 10) unless the operator raises them in `leadforge.yaml`.
5. Raw scrape/crawl artifacts live only in `leadforge_data/cache/` (gitignored) with a size-capped LRU (default 500 MB).
