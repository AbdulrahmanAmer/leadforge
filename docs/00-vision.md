# LeadForge — Vision & Scope

> **Status:** Approved · **Owner:** Striker + partner · **Type:** Internal tool (not a product) · **Doc set:** `docs/00`–`08`

## 1. Problem

Finding qualified B2B leads for a specific offer (web agency services, tech products, real-estate B2B, …) is manual, slow, and repetitive:
search Google Maps for a category in an area, open each business, dig into its website, guess who the decision maker (DM) is, copy contact
details into a sheet, and try to judge fit. Doing this through a raw AI-agent conversation burns enormous token volume on page dumps and
retries, and the results are unstructured and go stale.

## 2. Product statement

**LeadForge** is a self-bootstrapping, open-source-only B2B lead-generation engine packaged as a **dual-harness agent plugin** — one GitHub
repo that installs into **Claude Code** (as a plugin/marketplace) and **OpenAI Codex** (as a plugin + Agent Skills) — with a Python CLI that
does all heavy lifting outside the model context. The agent's job is reduced to: interview the user about their ICP (ideal customer
profile), kick off the pipeline, adjudicate decision-maker candidates from tiny snippets, and hand over a scored, noted lead sheet.

## 3. Users

| User | Mode | Needs |
|---|---|---|
| Striker | Claude Code (Windows) | Fast campaign setup, repeatable runs, clean XLSX output |
| Partner | Codex or Claude Code (OS unknown → must be cross-platform) | Install from the GitHub link, zero manual dependency setup |

## 4. Goals (measurable)

1. **Install-from-link:** working install on a clean machine from the repo URL in ≤ 2 commands per harness.
2. **Self-bootstrap:** `leadforge doctor --fix` detects and installs every missing dependency (Python packages, scraper binary, optional
   browser engine) before any run; no run starts in a broken environment.
3. **Token frugality:** a full campaign (interview → 200 scored leads → sheet) consumes **< 15k agent-visible tokens** end-to-end; no raw
   HTML or raw scrape output ever enters the model context (see `docs/06-token-contract.md`).
4. **Deliverable quality:** every exported lead row carries — business name, category, address, phone (E.164), website, DM name + title
   (when determinable), best email + validity tier, **score 0–100 with per-factor explanation**, a "likely need / hook" note, source
   provenance, and a verified-on date.
5. **Freshness:** contact data is validated at run time (MX, phone plausibility, site liveness) and stamped; re-runs re-verify stale rows.
6. **OSS-only:** zero paid APIs, zero API keys required for the default path. Optional free-registration registries (Companies House,
   OpenCorporates) are opt-in.
7. **Resumable & idempotent:** every stage checkpoints to SQLite; a crashed run resumes without re-scraping; re-runs dedupe against history.

## 5. Non-goals

- Not a SaaS, no UI, no multi-tenant anything — internal CLI + agent skill only.
- ~~No email sending / outreach automation~~ **Amended in v0.3 (owner decision 2026-09-02, ADR-011):** email sending is in
  scope as a bounded, audited extension — phone-first `Next Action`, dry-run default, human approval bound to content, suppression
  and eligibility re-checked at send time, automatic suppression from bounces/complaints, no dialer, no SMS, no probing, no paid
  dependency required (ADR-012). See `docs/07-compliance.md` and `docs/09-v0.3-build-plan.md`.
- No LinkedIn scraping in the default pipeline (ToS/ban risk; explicitly excluded by scope decision).
- No new anti-bot evasion research — we use what the chosen OSS scrapers ship with, politely configured.
- No CRM integration in v1 (XLSX/CSV/SQLite are the interface; CRM import is trivial from CSV).

## 6. Scope decisions (locked 2026-08-31)

| Decision | Choice |
|---|---|
| Data sources | **Open-source scrapers only** — Google Maps/Business via browser automation, business websites, public registries/directories |
| DM discovery | Website deep-crawl **+** public registries **+** Google Business/Maps browser scrape, with a **normalization layer** producing clean sheet-ready fields |
| Output | **XLSX + CSV**, backed by **SQLite** for cross-run dedupe/freshness |
| Harnesses | Claude Code **and** Codex, one repo, auto-install from the GitHub link |
| Language | Python 3.11+ orchestrator; scraper engine is an external pinned OSS binary (Go) driven over subprocess/REST |

## 7. Quality attributes (ranked)

1. **Agent-token efficiency** — the defining constraint; drives the CLI digest protocol and the snippet-based DM loop.
2. **Reliability/graceful degradation** — provider abstraction with fallback scraper; static-first crawling with browser escalation; every
   stage survives partial failure and reports precisely what degraded.
3. **Cross-platform** — Windows-first (primary machine), macOS/Linux equal citizens; no compiled native deps in the default install.
4. **Auditability** — provenance (source URL + timestamp) on every extracted fact; scores explain themselves.
5. **Politeness/compliance posture** — robots.txt respect on business sites, per-host delays, suppression list, documented ToS risk.
6. **Maintainability** — pinned versions, one canonical data model, ADRs for every consequential choice (`docs/08-decisions.md`).

## 8. Success scenario (canonical walkthrough)

> Partner installs from the repo link into Codex. He types "find me B2B leads". The skill triggers, asks 7 intake questions
> (offer, category, geography, size band, qualifications, disqualifiers, DM titles). The agent writes `answers.yaml`, runs
> `leadforge intake`, then `leadforge run --icp icp.yaml`. The CLI bootstraps the Maps scraper binary, grids the geography, scrapes
> listings, normalizes them, crawls each business site politely, extracts contacts + DM candidates, and pauses. The agent runs
> `leadforge dm export`, labels 40 compact candidate snippets in one pass, applies them back, and resumes. Scoring runs, the XLSX lands in
> `leadforge_data/exports/`, and the agent reports: "142 leads, 38 tier-A. Top hook: 61 shops have no online booking. Sheet: <path>."
> Total agent-visible output along the way: a few dozen digest lines.

## 9. Constraints & assumptions

- Google Maps caps ~**120 results per query**; the pipeline compensates with geographic grid tiling + query variants and dedupes by
  `place_id` (see `docs/04`). Scraping Maps breaches Google ToS — accepted internal-use risk, documented in `docs/07`.
- Residential proxies are **not** assumed. Default posture: low concurrency, human-ish pacing, per-run caps. Proxy support is exposed as
  config passthrough for when volume demands it.
- Email verification without paid APIs is probabilistic — we report **tiers**, never a false binary "valid".
- The two harnesses evolve fast; harness-touching surfaces are isolated in `skills/`, `.claude-plugin/`, `.codex-plugin/`, `AGENTS.md` and
  verified against docs current as of 2026-08-31 (`docs/01-research.md`).
