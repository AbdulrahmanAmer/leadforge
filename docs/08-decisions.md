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

## ADR-014 — Native list-first Maps provider; gosom kept as fallback (speed unit, v0.3)
**Context:** gosom shells out to a subprocess that visits every place page — reliable but slow (tens of
minutes for a tiled sweep). Live probing (2026-09-02, `scratchpad/speed/probe_list.py` /
`probe_list2.py`) showed the Google Maps results LIST itself (the `div[role="feed"]` a person scrolls)
already carries name, CID, lat/lng, rating, review count, category, street, open/closed state, phone
(119-120/120 cards) and website (83-91/120, as a redirect that must be unwrapped) — with plain
Playwright, headless Chromium, an identifying UA, and no stealth. **Decision:** ship `maps_list`, a
second discovery provider that drives one persistent browser per provider instance, scrolls the list to
its end (or a configured cap), and normalizes cards directly — no place-page visit unless
`discovery.maps_list.visit_details` is opted in. It carries no `place_id` (the list view never exposes
one); dedupe instead keys on CID, hex-decoded to the same decimal representation gosom's own `cid`
field already uses, so a maps_list row for a place gosom already found merges cleanly (by CID when the
gosom row also has one, or by phone via `db.upsert_business`'s existing fallback otherwise) rather than
creating a duplicate. gosom remains the primary/default provider (`discovery.providers: [gosom]`);
`maps_list` is opt-in per campaign. `discovery.parallel_queries` (any provider) fans queries across N
provider-chain instances instead of one at a time. **SCOPE.md #1 amended by owner decision
2026-09-03:** a self-written scraping engine is in scope as long as it stays plain Playwright with no
anti-bot evasion, no fingerprint spoofing, and no stealth patches — the same red line that already
governed gosom and crawl4ai usage, now stated to also cover code this repo writes itself, not only
pinned third-party engines. **Consequences:** a maps_list-discovered business is missing full postal
address, opening hours and Business-Profile "about" facts unless a details visit is opted in (a real
coverage trade-off against speed, not a bug); `Business.cid` becomes populated far more often across
both providers, and normalize.py must not assume `place_id` is the only non-name+street dedupe anchor
going forward.

## ADR-016 — Novelty-gated saturation subdivision (v0.4.1)
**Context:** ADR-013 subdivides every tiled query that returns >= `subdivide_at` results, because Google Maps
caps a search at ~100-120 results and the cap hides businesses. Live 2026-09-03 (10 cities, 6 categories):
once the base grid was done, 60 of 60 consecutive depth-1 children came back saturated AND added ~0.6 new
businesses each — their results were already known from neighbouring tiles and sibling categories — while each
still spawned 4 depth-2 children. The plan grew from 1,543 to 4,600+ tiles; discovery ETA drifted from 3 h to
13+ h for a few hundred more rows. **Decision:** record `new_count` per query (schema v4) and subdivide a
saturated tile only when it added >= `discovery.subdivide_min_new` (default 3) new businesses; provide
`leadforge prune-tiles` to drop already-queued children whose parent found nothing new (or every pending child,
for runs recorded before `new_count`). **Consequences:** coverage of a dense area now stops where novelty stops,
not where the result cap stops; a genuinely under-covered tile (many new results) still subdivides exactly as
before; the trade-off is explicit and configurable (0 restores ADR-013 behaviour).

## ADR-015 — Autopilot: `run` finishes itself through the operator's own headless Claude Code (v0.4)
**Context:** the owner asked (2026-09-03) for a fully automated `leadforge run` that ends in a sheet
carrying drafted emails, without the operator manually round-tripping `dm export`/`dm apply` and
`draft export`/`draft apply` for every campaign. ADR-007/012 already rule out any paid API — the tool must
stay zero-cost to run. **Decision:** `pipeline.autopilot` (default true) makes `run` continue on its own
past enrichment: labeling -> scoring -> drafting -> export, in one call. The judgment step (decision-maker
labeling, drafting) is delegated to the operator's OWN Claude Code, invoked headless in print mode
(`claude -p --output-format text --no-session-persistence`, auto-detected on PATH via
`leadforge/agent_runner.py`) — the same account and terms the operator is already using interactively, no
API key, no new billing relationship. Every agent step has a deterministic, clearly-labelled fallback so a
run never blocks on the agent being absent or failing: DM labeling falls to `heuristic_labels`
(`labeled_by=heuristic_auto` — exactly one matching-title candidate, never a reject); drafting falls to
`template_draft` (`messages.author=template` — deterministic sentences built only from packet facts, same
no-fabrication constraints as agent drafting). `--no-autopilot` / `pipeline.autopilot: false` restores the
exact pre-v0.4 pause-for-the-agent behavior at `stage=dm_pending`, unchanged. **Nothing sends**: outreach
still requires `outreach approve` and `--live` — autopilot only reaches as far as a drafted, unsent sheet.
**Consequences:** a campaign can run genuinely unattended end to end on a machine that has `claude` on
PATH; on one that doesn't (or with `agent.command: []`), the same run still finishes via the heuristic and
template fallbacks, just with lower label/draft coverage — the digest (`dm_unlabeled`, `draft_rejected`,
`draft_abstained`, `runner`) says honestly which path was taken, and `next` tells the operator exactly what
manual step (if any) would improve it. The agent runner shells out to a real `claude` process per batch,
so autopilot's wall-clock cost is bounded by `agent.timeout_s` × the number of batches
(`agent.max_batches` caps it per stage per run) — acceptable because it is the operator's own already-paid
session, not a metered API call.
