# Architecture Decision Records

Compact ADRs; context + decision + consequences. Statuses: Accepted unless noted.

## ADR-001 — gosom/google-maps-scraper as primary discovery engine
**Context:** OSS-only constraint; need browser-based Google Maps/Business scraping that is maintained, Windows-native, and programmable.
Research (docs/01 §2) compared 10 tools. **Decision:** pin gosom v1.17.4 (MIT, released 2026-08-22): subprocess `-json` mode default,
`-web` REST for long runs; doctor auto-downloads the per-OS release asset; conor-is-my-name REST adapter as independent-implementation
fallback; noworneverev lib + botasaurus as reserves. omkarcloud rejected (source removed, paid API). **Consequences:** Go binary is an
external artifact (pinned + checksummable); field-map drift is our maintenance surface (fixture test guards it); zero Docker/Go
requirement on Windows.

## ADR-002 — Static-first enrichment, browser as optional extra
**Context:** most SMB sites are static; Playwright stacks are heavy on Windows and slow. **Decision:** httpx+selectolax+trafilatura on
the default path; JS-shell detection escalates to crawl4ai (or Scrapling stealth) **only** when the `[browser]` extra is installed;
otherwise sites are marked `needs_browser` and reported. **Consequences:** default install is light & fast; some JS-only sites yield no
contacts until the extra is installed — visible, not silent.

## ADR-003 — Agent labels the decision maker from CLI-prepared snippets
**Context:** picking "who is the DM" is judgment; local NER (spaCy) gives names but not reliable roles; GLiNER helps but adds a model
download. Token cost of agent-side labeling is small **if** input is pre-shrunk. **Decision:** CLI extracts candidate name+title snippets
(≤300 chars) deterministically; agent labels batches via `dm export`/`dm apply` NDJSON; GLiNER is an optional `[ner]` upgrade for
candidate recall; registries corroborate when keys exist. **Consequences:** best-quality DM calls at ~6–9k tokens per campaign; pipeline
never blocks on the agent (unlabeled rows export without DM).

## ADR-004 — SQLite as the system of record
**Context:** internal tool, single operator per machine, cross-run dedupe/freshness needed. **Decision:** stdlib sqlite3, versioned
migrations, one file under `leadforge_data/`. No ORM (schema is small; explicit SQL keeps deps lean). **Consequences:** zero install,
easy backup; concurrent multi-process writes are out of scope (fine for CLI usage pattern).

## ADR-005 — Subprocess + one-line JSON digest as the agent boundary
**Context:** goal #3 (docs/00): a harness-agnostic, token-minimal interface. MCP server was considered. **Decision:** plain CLI with the
`LF_DIGEST` contract (docs/06); no MCP server in v1 (harnesses run shell natively; MCP adds a runtime + per-harness config). Revisit if a
GUI/host without shell needs to drive it. **Consequences:** works identically in Claude Code, Codex, cron, CI; trivially testable.

## ADR-006 — One `skills/` dir, per-harness manifests, spec-only frontmatter
**Context:** Agent Skills is now a cross-tool standard (agentskills.io) adopted by Codex + dozens of harnesses; Claude plugins and Codex
plugins have parallel manifest formats. **Decision:** single source of truth `skills/generate-leads/`; `.claude-plugin/{plugin,marketplace}.json`
+ `.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` + `AGENTS.md`; SKILL.md restricted to the 6 portable frontmatter
fields; `install.py` bridges user-scope installs. **Consequences:** one skill body to maintain; no Claude-only magic (`!cmd`, `@file`)
inside the skill.

## ADR-007 — No paid APIs anywhere; free-registration registries opt-in
**Context:** explicit scope decision by the owners. **Decision:** default pipeline requires zero keys; Companies House/OpenCorporates
adapters exist but are config-gated no-ops without keys; no SerpAPI/Places/Hunter code paths at all. **Consequences:** reliability rests
on OSS scraper health — mitigated by provider chain + pinning; volume ceilings accepted.

## ADR-008 — Python 3.11+, Typer CLI, pydantic v2, src layout
**Context:** owner requirement (Python), enterprise ergonomics, Windows-first. **Decision:** as titled; core deps only pure-python wheels
(no native compiles — libpostal excluded in favor of usaddress/pyap); extras for heavy optional stacks. **Consequences:** `pip install`
never needs a compiler; typed models validate every boundary; 3.10 not supported (uses 3.11 syntax).

## ADR-009 — Politeness invariants are code, not docs
**Context:** compliance posture (docs/07) must survive refactors. **Decision:** robots.txt check, per-host single-flight + delay,
caps, and suppression filtering are enforced inside `enrich/crawler.py` / `db.py` with tests (U8.1/U8.4), not left to call-site
discipline. **Consequences:** slightly more plumbing; guarantees hold for any future command added on top.


## ADR-010 — Registry DM auto-pick (v0.1.1, amends ADR-003)
**Context:** ADR-003 made the agent the final DM arbiter. Live UK runs showed Companies House returns the
registered directors — official identity, stronger than any inference from website text — and big runs queue
dozens of obvious single-director cases for manual labeling. **Decision:** when a registry returns exactly one
ACTIVE INDIVIDUAL officer (corporate officers like "X Ltd/LLP" excluded by name pattern), the pipeline marks
them the DM automatically (`labeled_by=registry`, confidence 0.9). Zero or 2+ individuals still go to the
agent. **Consequences:** DM batches shrink dramatically on registry-covered campaigns; the agent's judgment is
reserved for genuinely ambiguous cases; a wrong registry match remains possible but is bounded by the
locality-overlap match rule (U4.6).
