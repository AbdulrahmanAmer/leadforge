# ICP interview guide + answers.yaml schema

## Question bank (adapt tone; batch 2–3 per message; skip anything already stated)

1. **Offer:** "What exactly are you selling/offering, in one sentence? What's the outcome for the client?"
2. **Category:** "What type of businesses should I hunt for? Any sub-types to include or avoid?" (map to Google Maps category phrasing:
   'auto repair shop', 'real estate agency', 'dental clinic', …)
3. **Geography — ALWAYS ask the country:** "Which country, and which city/area within it?" Get an ISO2
   country code (US, GB, EG, AE, …) plus a specific area. In federal countries (US/CA/AU/BR/IN/MX) insist on
   the state/region too: "Houston, TX". Reject vague answers ("the Gulf", "downtown", "up north") and ask
   which named city — intake will error without a country, and geocoding refuses ambiguous matches rather
   than guessing, so a vague answer just costs a round trip.
4. **Size band:** "Any size filter — minimum review count, solo operators ok, chains ok?"
5. **Hard disqualifiers:** "What makes a business an automatic NO? (franchise, no phone, already a client, competitor…)"
6. **Soft qualifications:** "What makes one a GREAT fit for the offer? (e.g. for web design: outdated/no website; for booking software:
   busy but phone-only)" — these become scoring `needs` signals.
7. **Decision maker:** "Who do you want to reach — Owner? GM? Marketing? In priority order."
8. **Volume + region profile:** "How many leads do you want (default 200)? Outreach region for the compliance note: us/uk/eu?"

Defaults if user shrugs: size=none, hard=[no_phone], dm titles=[Owner, General Manager, Manager], caps.max_leads=200, region=us.

## answers.yaml (write exactly this shape; intake validates)

```yaml
version: 1
campaign: short-slug-you-invent          # kebab, unique per campaign
offer:
  what: "Website redesign + booking system for auto repair shops"
  value_prop: "More booked jobs, fewer missed calls"
  sender: "Striker's agency"
target:
  categories: ["auto repair shop", "transmission shop"]   # 1-5, Maps-style phrasing
  geography:
    country: US                          # REQUIRED, ISO2 — ask the user; never guess
    areas: ["Houston, TX"]               # 1-3 specific place names; OR bbox: [minLng,minLat,maxLng,maxLat]
  size:
    min_reviews: 10                      # or null
qualify:
  hard: [franchise_or_chain, no_phone]   # from vocabulary below; unknown terms rejected
  soft: [website_missing, low_rating_high_volume, stale_site]
decision_maker:
  titles_priority: ["Owner", "General Manager", "Service Manager"]
caps:
  max_leads: 200
compliance:
  region_profile: us                     # us | uk | eu
notes: "free text from the user worth keeping"
```

### Qualifier vocabulary (v0.1)

hard: `franchise_or_chain` · `no_phone` · `no_website_hard` (exclude sites-less businesses — only when the offer NEEDS a site) ·
`closed_or_unverified` · `competitor:<name-substring>` · `existing_client:<name-substring>`

soft (need signals; each maps to a scoring signal + hook template): `website_missing` · `website_no_ssl` · `stale_site` (old copyright/
broken pages) · `low_rating_high_volume` (rating ≤ 3.9 & reviews ≥ 50) · `few_reviews` (< 10) · `weak_social_presence` · `phone_only_booking`
(no online booking detected) · `hiring` (careers page present)

If the user's criterion fits no vocabulary term, put it in `notes` and tell them scoring can't automate it yet (it can still guide DM
labeling and their outreach).

## Worked examples

- **Web agency → auto repair, Houston** (above): `website_missing`/`stale_site` are POSITIVE need signals; scoring handles the inversion
  automatically via the ICP — never "fix" it by hand.
- **B2B SaaS (booking tool) → dental clinics, 3 cities:** categories: ["dental clinic","orthodontist"]; soft: [phone_only_booking,
  low_rating_high_volume]; dm: ["Practice Manager","Owner","Office Manager"].
- **Commercial realtor → growing SMBs:** categories: ["coworking space","gym","daycare"]; soft: [hiring]; hard: [franchise_or_chain];
  dm: ["Owner","Founder","Managing Director"].

## Scaling a campaign to 1,000+ leads

**Why one query can't just "go deeper":** Google Maps stops serving results at roughly 100–120 per search,
server-side. The scraper already scrolls to the end (`discovery.depth: 10`) — measured live, "auto repair
shop in Birmingham" returns ~106 listings no matter what. Deepening does nothing; you need *more searches*.

Two ways to widen, and they combine:

1. **More categories × more towns** (below) — always available, no extra setup.
2. **Grid tiling** — splits ONE town into map cells, each its own search with its own ~120 budget. This is
   how you exhaust a big city instead of skimming its first 106 results. Opt-in:
   `leadforge config set discovery.grid_mode auto` (per-campaign: `target.geography.grid: auto`,
   cell size `discovery.grid_cell_km`, cap `caps.max_tiles`). Always run `leadforge plan` first — the digest
   reports map cells, total queries and estimated hours before you spend them, and a tiled plan is easily
   10–60× the queries of a text plan. Tiling needs a geocode (Nominatim) per area, so it adds a network
   dependency a plain text plan doesn't have.

To reach 1,000 records by widening:

```yaml
target:
  categories: ["accounting firm", "bookkeeping service", "tax consultant"]   # 3–5 variants
  geography:
    country: GB
    areas: ["Guildford", "Woking", "Farnham", "Godalming", "Camberley", "Basingstoke"]  # 6–10 towns
caps: { max_leads: 1000, max_sites: 1200, max_tiles: 60 }
```

What to expect and set:
- **Runtime**: discovery is the slow stage — each full-depth query visits every place page (~20–35 min per
  query). 18 queries ≈ several hours. Run it and walk away: every query checkpoints, `--resume` continues
  after any interruption, and a timeout salvages the listings already scraped.
- **`discovery.timeout_min: 45`** in `leadforge.yaml` for full-depth runs (default 30 truncates the
  biggest queries; salvage keeps what was scraped either way).
- **Enrichment** of ~1,000 sites takes ~1–2 h at the default politeness settings. Do not lower
  `politeness.delay_s` below 2.0 to go faster — slower is the compliant way to scale.
- **DM labeling** arrives in batches of ≤60; expect ~15–20 batches for 1,000 leads. Label a batch whenever
  one is ready (`dm export` → `dm apply`) — the pipeline waits at the DM gate, nothing is lost.
- If runs get rate-limited (degraded queries pile up), pause and resume hours later; never work around it.

## Account-fit profile (WE SCORE-style prospecting)

For "prospecting engine" briefs (fixed 0-100 account rubric, A-D grades, contactability + data-confidence
columns, MANUAL_REVIEW/READY_FOR_OUTREACH statuses), set `scoring: { profile: account_fit }` in answers.yaml.
Full worked example: `config/icp.wescore.example.yaml` (MENA, six target industries).

What it adds per lead: employee estimate + band (50-500 = target), departments detected, tech stack
(Microsoft 365 via MX records; CRM/ERP/WMS via site fingerprints — UNKNOWN is never treated as NO),
industry-specific buying triggers with freshness banding, and a separate Contactability (0-100) and
Data Confidence (0-100) score. Hard rule inherited from the spec: unknown facts never disqualify;
only CONFIRMED negatives do (e.g. a stated headcount under 20).

## Company mode (Companies House, not Google Maps) — docs/09 Wave 2 H

Set `target.mode: company` when the brief is "find UK-registered companies matching these SIC codes",
not "find local businesses near a place" — most naturally GainLev's own client-acquisition pipeline,
but usable for any UK B2B prospecting run. Full worked example: `config/icp.company.example.yaml`.

What's different from the default `local_business` mode:

- **Targeting is by UK SIC code, not Maps category phrasing.** `target.sic_codes` (>= 1 five-digit
  code, e.g. `"62012"`) replaces `target.categories` as the thing that actually selects businesses —
  categories may be left `[]` in company mode. `target.geography.areas` is still required (Companies
  House advanced-search needs a location string; a bbox alone isn't usable here) and `country` must be
  `GB` — the API only covers UK-registered companies.
- **Discovery provider**: set `discovery: { providers: [companies_house] }` in `leadforge.yaml`
  (workspace config, not the ICP) and `registry: { companies_house_key: "<key>" }` — the same free key
  from https://developer.company-information.service.gov.uk that the registry cross-check already uses.
  Query plan is (SIC-code shards of <=5 codes) x areas — see `leadforge.company.build_company_plan`.
- **Scoring**: `scoring.profile` is set to `company` automatically by intake when target.mode is
  `company` and no profile was set explicitly. The rubric is SIC overlap with your target list
  (industry_fit), incorporation-age banding (a 3-10y-old company scores highest — established but still
  growable), a new-director trigger (an officer appointed within the last 12 months — often means fresh
  budget/priorities), a hiring signal (careers page detected on the crawled site), whether a domain was
  resolved for the company at all, and data confidence. Contactability and a plain-language status note
  are separate, informational — they don't move the Score/Tier. No review-count sizing (Companies House
  doesn't have reviews) and no chain/group penalty (a subsidiary is not disqualifying here).
- **No website field exists on a Companies House record** — `leadforge.enrich.resolve_domain` guesses
  candidate domains from the registered legal name (minus "Limited"/"Ltd"/etc.) and verifies each by
  actually fetching it (robots.txt + politeness delay respected, same as every other crawl) and
  requiring the registered postcode, the legal name, or the company number to appear on the page. No
  search engines, no paid lookups (ADR-012) — every candidate is a guess made from public registry
  data and checked directly, never assumed.
- **`GAINLEV_ICP_SIC`** (`leadforge/company.py`) is the curated "everyone who sells B2B" list this
  example ICP uses — agencies, SaaS, consultancies, recruiters, wholesalers, estate/letting agents,
  accountants, solicitors — deliberately excluding `82200` (call centres, owner decision 7).
