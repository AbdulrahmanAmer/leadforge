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
locality-overlap match rule (U4.6). *Amended by ADR-013 (v0.3): the match must also pass a name-similarity
gate and the company must be active; the auto-pick inherits both.*


## ADR-011 — Email sending is in scope, under guardrails (v0.3, owner decision 2026-09-02)
**Context:** `icm/SCOPE.md` #4 and docs/00 §5 excluded sending so the compliance posture could rest on
"it sends nothing". The owners now need the tool to carry outreach for GainLev and for clients, and the
measured live campaign showed that the honest sendable list is small (45–100 of 816) while 793 rows have a
phone. **Decision:** sending is allowed as a bounded, audited extension: phone-first `Next Action`; dry-run
default; `--live` only with `outreach.armed` + `--i-am`; approval bound to the draft's content hash;
suppression + eligibility + caps + warm-up + send window re-checked inside the send transaction; RFC 8058
one-click unsubscribe; automatic suppression from bounces/complaints/unsubscribes/reply opt-outs; per-mailbox
circuit breaker; Article-14 first-contact line; `client_id` on every identity, target, send and suppression
row so a client's opt-outs never leak into another list (hybrid controller model — the contract decides
whether the client or GainLev owns the sending domain). No dialer, no SMS, no probing, no sending to
non-sendable tiers. **Consequences:** docs/07's PECR/CAN-SPAM row moves from "operator's duty" to
LeadForge-owned controls; the skill gains an outreach section; SCOPE #4 rewritten; the README no longer
claims the tool cannot send.

## ADR-012 — Pluggable transport, in-harness drafting, no paid dependencies (v0.3)
**Context:** the tool is internal open source; the owners want the sending method chosen by the operator at
send time, not baked in, and no API key required anywhere. **Decision:** a `Transport` ABC with `file`
(dry-run `.eml`, the default) and `smtp` (stdlib `smtplib` + `imaplib`, any mailbox) built in and an adapter
registry for platforms later; secrets are referenced by environment-variable *name* in SQLite, never stored.
Drafting is agent-in-the-loop exactly like DM labeling: `draft export` emits evidence packets, the harness
writes the two model slots, `draft apply` runs a mechanical no-fabrication gate. Domain resolution in company
mode uses free methods only (slug candidates + on-page verification). **Consequences:** deliverability
tooling (warm-up, rotation) is the operator's mailbox provider's job or a later adapter; drafting cost is
session tokens (bounded by the packet size) rather than an API bill.

## ADR-013 — Registry-first discovery alongside Maps; registry matches gated by name + status (v0.3)
**Context:** Google Maps serves ~106 results per search server-side; the live campaign covered 4 of 10
cities and, by phone match, held 317 of the 1,045 MOT stations the DVSA lists in those cities. Companies
House matches were accepted on locality alone (7–10% wrong company, 26 DMs from dissolved companies).
**Decision:** official registers are first-class discovery providers — the DVSA "Active MOT test stations"
CSV (OGL) and Companies House advanced search — merged into Maps rows by E.164 phone; tiled Maps queries
that saturate (>= `discovery.subdivide_at`) are subdivided automatically; a registry match requires
`name_similarity >= registry.min_name_similarity` and `company_status == active`, and the profile is
persisted on every match. Provider field maps are registered per provider (`providers/base.py`).
**Consequences:** coverage becomes measurable against a denominator; the sheet's Company No column is true;
dissolved companies never carry a decision maker.
