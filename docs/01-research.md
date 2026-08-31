# Research Digest — verified 2026-08-31

Findings that drive the architecture. Every claim was verified against live sources on 2026-08-31; URLs inline. Items that could not be
fully verified are marked **UNVERIFIED**.

---

## 1. Harness integration (Claude Code + Codex)

### 1.1 Claude Code plugins

- Manifest: `.claude-plugin/plugin.json` (only file that lives inside `.claude-plugin/` besides `marketplace.json`); component dirs sit at
  repo root. Key fields: `name` (kebab-case, required), `version`, `description`, `author`, `skills` (adds to default `skills/`),
  `commands`, `agents`, `hooks`, `mcpServers`, `userConfig`, `dependencies`. Docs: <https://code.claude.com/docs/en/plugins-reference>
- A repo becomes **its own marketplace** via `.claude-plugin/marketplace.json` with `plugins: [{"name": …, "source": "./"}]`.
  Docs: <https://code.claude.com/docs/en/plugin-marketplaces>
- Install flow from a shared GitHub link (current syntax):
  `/plugin marketplace add <owner>/<repo>` → `/plugin install <plugin>@<marketplace-name>` (marketplace-name = `name` field in
  marketplace.json). Headless: `claude plugin install name@marketplace`. Local dev: `claude --plugin-dir ./repo`, `claude plugin validate`.
  Docs: <https://code.claude.com/docs/en/discover-plugins>
- Plugin skills live at `skills/<name>/SKILL.md`, **auto-trigger from `description`**, are namespaced `/plugin:skill`.
  `${CLAUDE_PLUGIN_ROOT}` is substituted inside skill bodies. Docs: <https://code.claude.com/docs/en/skills>
- **Portability rule:** only 6 frontmatter fields are legal across all Agent-Skills consumers — `name`, `description`, `license`,
  `compatibility`, `metadata`, `allowed-tools`. Claude-only extras (`argument-hint`, `context: fork`, …) break claude.ai packaging and are
  ignored elsewhere → **we restrict SKILL.md to the 6 spec fields**.

### 1.2 OpenAI Codex

- **Skills are native and GA**, spec-compliant with the open Agent Skills standard. Discovery: `$REPO/.agents/skills`, `~/.agents/skills`,
  `/etc/codex/skills` (legacy `~/.codex/skills` no longer documented — **UNVERIFIED** whether still read; target `.agents/skills`).
  Docs: <https://developers.openai.com/codex/skills>
- **Codex has a plugin/marketplace mechanism** (≈ Mar 2026), deliberately parallel to Claude's: manifest `.codex-plugin/plugin.json`
  (`name`, `version`, `skills`, `mcpServers`, `hooks`, `interface{displayName…}`), marketplace at `.agents/plugins/marketplace.json`.
  CLI: `codex plugin marketplace add owner/repo` then install via the `/plugins` browser.
  Docs: <https://developers.openai.com/codex/plugins> · build guide <https://developers.openai.com/plugins/build/plugins>
- `AGENTS.md` (the agents.md standard, adopted by Codex/Gemini CLI/Cursor/Copilot/…) is read git-root→cwd, concatenated, 32 KiB default
  cap. Custom prompts (`~/.codex/prompts`) are **deprecated** in favor of skills. Docs:
  <https://developers.openai.com/codex/guides/agents-md> · <https://agents.md/>
- Cross-tool bridge installer exists: `npx skills add owner/repo` (vercel-labs/skills) installs a repo's `skills/` into ~77 agents.
  <https://github.com/vercel-labs/skills>

### 1.3 Dual-harness layout (proven in the wild)

`obra/superpowers`, `nvidia/skills`, `anthropics/skills` all ship **one `skills/` dir** + per-harness manifests. We copy that pattern:
single `skills/` source of truth; `.claude-plugin/` + `.codex-plugin/` + `.agents/plugins/marketplace.json` + `AGENTS.md` + `CLAUDE.md`.

---

## 2. Maps/Business scraper landscape (OSS, browser-based, no paid APIs)

| Tool | Lang/License | Freshness (verified) | Verdict |
|---|---|---|---|
| **gosom/google-maps-scraper** | Go · MIT | v1.17.4 released **2026-08-22**; 4.2k★ | **PRIMARY** |
| conor-is-my-name/google-maps-scraper | Python/FastAPI · MIT | Feb 2026 selector refactor; 306★ | **FALLBACK** (Docker REST) |
| noworneverev `google-maps-scraper` (PyPI) | Python · MIT | v0.1.2 2026-03-08 | Reserve (importable lib, immature) |
| omkarcloud/google-maps-scraper | "MIT" file, **source removed** | freemium desktop app, paid API $16/mo | **Rejected** — not programmable OSS |
| botasaurus / botasaurus-driver | Python · MIT | driver 4.0.101 2026-08-10 | Reserve DIY framework only |
| D4Vinci/Scrapling | Python · BSD-3 | v0.4.15 **2026-08-23** | Enrichment escalation engine (stealth fetch), not a Maps scraper |
| unclecode/crawl4ai | Python · Apache-2.0 | v0.9.2 2026-07-15 | Enrichment escalation (JS sites → LLM-ready markdown) |

**Why gosom is primary** (<https://github.com/gosom/google-maps-scraper>):

- MIT, actively maintained (release 9 days before verification date), prebuilt per-OS binaries incl. native
  `google_maps_scraper-…-windows-amd64.exe` → **no Go toolchain, no Docker required on Windows**.
- 36 output fields: title, category, address, complete_address, phone, website, plus_code, review_count/rating, lat/lng, **cid, place_id**,
  hours, popular_times, owner, about, thumbnail, optional `-email` website crawl, … JSON/CSV/Postgres out.
- **Native geo-gridding** (`-grid-bbox`, `-grid-cell`, `-zoom`, `-radius`) — the standard workaround for Google's ~120-results-per-query
  cap (cap confirmed: <https://www.octoparse.com/blog/scraping-google-maps>).
- Consent-screen rejection handled in source (`gmaps/job.go` targets `form[action*="consent.google"]`).
- Three programmatic surfaces: subprocess CLI (our default), `-web` REST API on :8080 with OpenAPI docs (our long-run mode), Postgres queue.
- Proxy passthrough (`-proxies`), concurrency (`-c`), depth, language/geo flags.

**Reliability picture 2026:** Google Maps is rated ~90/100 scraping difficulty (rate limiting, fingerprinting, `/sorry` captchas —
<https://blog.scrappey.com/blog/a-guide-to-scraping-google-maps-in-2026>); mitigations that matter for us: low concurrency, gridding +
`place_id` dedupe, caps per run, optional proxies. No open "fully broken" issue on gosom at verification date.

---

## 3. Enrichment stack (websites → contacts → DM)

| Layer | Choice | Why |
|---|---|---|
| Static fetch (90% of SMB sites) | **httpx + selectolax + trafilatura** | Pure-python (zero friction on Windows), fastest, trafilatura = best OSS main-content extractor w/ sitemap+robots helpers. <https://github.com/adbar/trafilatura> |
| JS-rendered escalation | **crawl4ai** (optional extra) | Playwright-based, pruned `fit_markdown` = most token-efficient page representation. <https://github.com/unclecode/crawl4ai> |
| Anti-bot-gated escalation | **Scrapling StealthyFetcher** (optional extra) | Patchright stealth, maintained (v0.4.15, Aug 2026). <https://github.com/D4Vinci/Scrapling> |
| Emails | mailto + regex + **Cloudflare `data-cfemail` XOR decode** + `[at]/[dot]` normalization | No single lib covers obfuscation; ~30 lines of deterministic code |
| Phones | **python-phonenumbers** (Apache-2.0, pushed 2026-07-03) | libphonenumber port: parse→validate→E.164+type. <https://github.com/daviddrysdale/python-phonenumbers> |
| Email validity | **email-validator** (syntax+IDNA) + **dnspython** MX + **disposable-email-domains** (CC0) | Layered probabilistic tiers; deliberately **no SMTP RCPT probing** (catch-alls lie, IP-reputation risk). <https://github.com/JoshData/python-email-validator> |
| Addresses | **usaddress**/**pyap** (pure-py) default; libpostal only via conda/Docker (C build hostile on Windows) | Sheet-ready address splitting without native compiles |
| DM extraction | Heuristic title-keyword×name candidates locally → **calling agent labels final DM from ≤300-char snippets** | GLiNER (zero-shot NER, Apache-2.0) is the optional local upgrade; agent-labeling is the token-optimal default. <https://github.com/urchade/GLiNER> |
| Domain OSINT | theHarvester (maintained, pushed 2026-06-12) | Optional; effectiveness now key-gated → not on default path |

## 4. Public registries (free, opt-in)

- **UK Companies House API** — free key, clean REST (`/company/{n}/officers`, PSC), 600 req/5 min. Best free DM cross-check for UK.
  <https://developer.company-information.service.gov.uk>
- **OpenCorporates** — free token (approval lag), 140+ jurisdictions incl. US states; the practical US aggregate (per-state SoS scraping is
  not uniformly scriptable). <https://api.opencorporates.com/documentation/API-Reference>
- Both require registration → shipped as **optional providers, off by default**, config slots only.

## 5. Scoring model (adapted to scraped data)

2026 B2B rubrics split fit vs intent (e.g. 65/35, tiers A≥80/B 60–79/C<60 —
<https://www.digitalapplied.com/blog/b2b-icp-scoring-framework-2026-lead-qualification-playbook>). A scrape-first pipeline has **no
behavioral intent**, so LeadForge re-weights to fit + reachability and treats public "need signals" as the intent proxy:

**Fit 50** (industry 18, size 12, geography 10, model 10) · **DM reachability 25** (DM named 12, verified direct contact 13) ·
**Need signals 15** (no/weak website, stale site, low review-response, hiring, …) · **Data confidence 10** (corroboration + freshness) ·
**Negatives cap −40** (franchise HQ, competitor, out-of-area, suppressed). Tiers **A ≥ 75, B 55–74, C < 55**. Every factor stores a
one-line explanation → auditable sheet.

## 6. Compliance snapshot (practical; not legal advice)

- Scraping **public** data ≠ CFAA violation (hiQ v. LinkedIn line of cases); the real exposure is **ToS/contract** (Google Maps ToS
  prohibits scraping — internal-use risk accepted & documented), fake accounts (we use none), and barrier circumvention (we build none).
- **GDPR:** B2B prospecting under legitimate interest requires a documented LIA; a named work email **is** personal data → store source +
  date per contact (we do, as `evidence`), honor objections (suppression list).
- **PECR (UK):** corporate subscribers may be emailed B2B without prior consent; sole traders/individuals need consent. **CAN-SPAM (US):**
  opt-out regime — identity, postal address, working unsubscribe. LeadForge does not send email; the sheet's compliance notes remind the
  operator.
- **robots.txt:** honored on business websites (+ per-host delay, identifying UA). Maps itself has no applicable robots regime for a
  logged-out browser session; the scraper's politeness knobs (low `-c`, caps) are our posture.

Full guardrails: `docs/07-compliance.md`.
