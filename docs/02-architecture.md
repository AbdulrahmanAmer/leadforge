# Architecture

> 🖼️ **Rendered image:** [`diagrams/02-architecture.png`](diagrams/02-architecture.png) — read that first if you
> just want to see the shape of the system. All images: [`diagrams/`](diagrams/).

![Architecture](diagrams/02-architecture.png)

## 1. System context (C4-L1)

```mermaid
flowchart TB
    subgraph HARNESS["Agent harness (Claude Code / Codex)"]
        AGENT["AI agent\n(runs the generate-leads skill)"]
    end
    USER(["Operator\n(Striker / partner)"])
    subgraph HOST["Operator machine (Win/mac/Linux)"]
        CLI["leadforge CLI\n(Python 3.11+)"]
        SCRAPER["gosom google-maps-scraper\n(pinned OSS binary)"]
        DB[("SQLite\nleadforge_data/db.sqlite3")]
        SHEET[["XLSX / CSV exports"]]
    end
    GMAPS["Google Maps / Business\n(public listings)"]
    SITES["Business websites\n(about/team/contact/impressum)"]
    REG["Public registries (opt-in)\nCompanies House / OpenCorporates"]

    USER -- "answers intake questions" --> AGENT
    AGENT -- "subprocess commands,\nreads digest lines only" --> CLI
    CLI -- "spawns + drives" --> SCRAPER
    SCRAPER -- "headless browser" --> GMAPS
    CLI -- "polite HTTP crawl" --> SITES
    CLI -. "REST (free keys)" .-> REG
    CLI <--> DB
    CLI --> SHEET
    SHEET --> USER
```

**The defining boundary:** the agent never touches raw web content. Everything noisy (HTML, scrape output, crawl text) stays on the CLI
side of the line; the agent sees only **digest lines** and **DM snippets** (`docs/06-token-contract.md`).

## 2. Component view (C4-L3, package `leadforge`)

```mermaid
flowchart LR
    subgraph cli_layer["cli.py (Typer)"]
        CMD["commands:\ndoctor · intake · plan · discover\nenrich · dm · score · export · run · status · suppress"]
    end
    subgraph core["core"]
        CFG["config.py\nleadforge.yaml + env"]
        MOD["models.py\npydantic canon: Business, Contact,\nPerson, Evidence, Score, ICP"]
        DBM["db.py\nSQLite schema+migrations,\nupserts, dedupe keys"]
        UTL["util.py\nlogging, retry, politeness,\nrate limiter, digest emitter"]
    end
    subgraph pipeline["pipeline stages"]
        DOC["doctor.py\nenv checks + self-install"]
        INT["intake.py\nanswers.yaml → icp.yaml compiler"]
        GRD["grid.py\ngeo tiling + query planner"]
        PRV["providers/\nbase.py · gosom.py · fallback_rest.py\n· registry stubs"]
        NRM["normalize.py\nraw → canonical Business\n(sheet-ready fields)"]
        ENR["enrich/\ncrawler.py · extract.py\nvalidate.py · dm.py"]
        SCR["score.py\nweighted rubric + explanations\n+ need-hooks"]
        EXP["export.py\nXLSX (styled) + CSV + run report"]
    end
    CMD --> DOC & INT & GRD & PRV & NRM & ENR & SCR & EXP
    PRV --> NRM --> DBM
    ENR --> DBM
    SCR --> DBM
    EXP --> DBM
    DOC --> CFG
    INT --> MOD
    PRV & ENR & SCR & EXP --> MOD
    CMD --> UTL
```

### Responsibilities

| Module | Responsibility | Key rule |
|---|---|---|
| `cli.py` | Command surface; **every command ends with one `LF_DIGEST {json}` line** | No command may print unbounded output |
| `config.py` | Layered config: defaults → `config/leadforge.example.yaml` copy → workspace `leadforge.yaml` → env `LEADFORGE_*` | All paths under `leadforge_data/` |
| `models.py` | Canonical pydantic v2 models; the **only** schema in the system | Providers/enrichers must emit these — nothing else crosses module lines |
| `db.py` | Schema DDL + versioned migrations; idempotent upserts | Dedupe key = `place_id`, else `sha1(name_norm+addr_norm)` |
| `doctor.py` | Detect/install: pip deps, gosom binary (pinned, per-OS asset), optional Playwright, network reachability, disk | **Runs before every pipeline command** (cheap cached check) |
| `intake.py` | Validate agent-collected `answers.yaml` → compile `icp.yaml` (queries, qualifications, weights, caps) | Schema-validated; rejects vague ICPs with actionable errors |
| `grid.py` | Geocode area (Nominatim, cached) → bbox → grid tiles sized to density → query plan | Respects Google ~120-results/query cap by design |
| `providers/` | `DiscoveryProvider` ABC: `plan(icp) -> [Query]`, `fetch(query) -> [RawListing]`; gosom via subprocess `-json`; fallback via REST | Provider failures degrade, never abort the run |
| `normalize.py` | Raw listing → canonical `Business`: E.164 phone, split address (usaddress/pyap), category mapping, name cleanup, url canonicalization | **This is the "clean sheet formatting" guarantee** |
| `enrich/crawler.py` | Static-first polite crawl (httpx+selectolax+trafilatura): home, about, team, contact, impressum, sitemap probe; ≤ N pages/site; robots + per-host delay; escalation hooks to crawl4ai/Scrapling extras | Never crawls suppressed/out-of-scope domains |
| `enrich/extract.py` | Emails (mailto/regex/cfemail/at-dot), phones, socials, copyright-year staleness, person+title candidate snippets | Every fact → `Evidence(url, ts, snippet)` |
| `enrich/validate.py` | Email tiers (syntax→MX→disposable→role), phone validity, site liveness | Tiers, never booleans |
| `enrich/dm.py` | DM candidate store; `dm export` NDJSON snippets for the agent; `dm apply` labels back | Snippets ≤ 300 chars; batch caps |
| `score.py` | Rubric from `config/scoring.default.yaml` ⊕ ICP overrides; per-factor explanations; "likely need" hook synthesis | Deterministic, unit-tested |
| `export.py` | Styled XLSX (Leads + Summary + About sheets), CSV mirror, JSON run report | Column dictionary in `docs/03-data-model.md` §5 |

## 3. Tech stack & rationale

| Concern | Choice | Rationale (see `docs/08-decisions.md`) |
|---|---|---|
| Orchestrator | Python 3.11+, `src/` layout, pyproject | User requirement; ubiquitous on target machines |
| CLI | Typer + Rich(minimal) | Typed commands, `--json` everywhere, low dep weight |
| Models | pydantic v2 | Validation at every boundary; JSON-schema export for the skill docs |
| Discovery engine | gosom binary (pinned v1.17.4) via subprocess | ADR-001: MIT, maintained, native Windows exe, gridding + consent handling built in |
| Fallback discovery | conor-is-my-name REST (Docker) / noworneverev lib | ADR-001: independent selector implementations |
| Static crawl | httpx + selectolax + trafilatura | ADR-002: pure-python, Windows-clean, fastest |
| Browser escalation | crawl4ai / Scrapling as **optional extras** `[browser]` | Heavy Playwright dep off the default path |
| Contact validation | phonenumbers, email-validator, dnspython, disposable-email-domains | All permissive licenses, no native builds |
| DM identification | snippet export → **agent labels** (default); GLiNER extra `[ner]` | ADR-003: token-optimal + best quality |
| Storage | SQLite (stdlib) | ADR-004: zero-install, single-file, transactional |
| Export | openpyxl (xlsx) + csv (utf-8-sig) | Styled, filterable, Excel-safe |
| Resilience | tenacity retries, per-host token-bucket, stage checkpoints | Politeness + resumability |

## 4. Degradation ladder

```mermaid
flowchart TD
    A["discover via gosom subprocess"] -->|"binary missing"| B["doctor --fix auto-downloads pinned release"]
    A -->|"captcha / empty tiles"| C["reduce concurrency, backoff,\nremaining tiles marked degraded"]
    C -->|"still failing"| D["fallback provider (REST) if configured"]
    E["static site crawl"] -->|"JS shell / empty text"| F["escalate to crawl4ai IF extra installed"]
    F -->|"not installed"| G["mark site 'needs-browser', continue"]
    H["email MX check"] -->|"DNS timeout"| I["tier=unknown, retry queue"]
    J["registry lookup"] -->|"no key configured"| K["skip silently (opt-in feature)"]
```

Every degradation is **recorded on the run** and surfaces in the digest (`warnings`) and the Summary sheet — the run never silently lies
about coverage.

## 5. Cross-platform notes

- No native compiles anywhere on the default path (libpostal explicitly excluded; usaddress/pyap instead).
- gosom binary selected per `platform.system()/machine()`; stored in `leadforge_data/bin/`; never requires PATH edits.
- All subprocess calls: `shell=False`, explicit `encoding="utf-8"`, timeouts.
- Paths via `pathlib` only; LF endings enforced by `.gitattributes`; CSV written `utf-8-sig` so Excel on Windows opens clean.
- Long-run mode (`leadforge discover --serve`) drives gosom's `-web` REST API instead of one-shot subprocess — same provider interface.

## 6. Security & privacy

- No credentials required or stored by default; optional registry keys live in workspace `leadforge.yaml` (gitignored) or env vars.
- `leadforge_data/` is gitignored — scraped personal data never lands in the shared repo.
- Suppression table filters opted-out domains/emails at crawl, score, and export time.
