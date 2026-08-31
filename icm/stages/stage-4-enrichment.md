# Stage 4 — Enrichment: remaining units (PRESCRIPTIVE)

**Read `icm/SCOPE.md` first.** Everything here reads pages that businesses publish in order to be contacted,
plus two official government/registry APIs used with their own free keys. No logins, no evasion, no sending.

Prereqs: G3 producing businesses in SQLite. All three units are **optional extras** — each is off unless its
package/keys are present, and the pipeline must work identically without them.

---

## U4.5 — Browser escalation for JavaScript-rendered sites

**Why:** ~10% of small-business sites render their contact info with JS, so the static crawler sees an empty
shell. Those sites are currently flagged `needs_browser` and skipped. This unit covers them.

**File to edit:** `src/leadforge/enrich/browser.py` (stub). Extra: `pip install -e .[browser]` (crawl4ai).

### Step 1 — read first
- `src/leadforge/enrich/crawler.py` — especially `SiteCrawler._get` (robots + throttle) and `looks_js_shell`.
- `src/leadforge/enrich/runner.py::_process_one` — where you will wire this in.

### Step 2 — implement exactly

```python
def is_available() -> bool:
    try:
        import crawl4ai  # noqa: F401
        return True
    except Exception:
        return False


def fetch_rendered(url: str, cfg, throttle) -> str:
    """Render one page with a headless browser and return its HTML ("" on failure).

    MUST honor the same politeness as the static crawler: caller checks robots, we wait on the throttle.
    """
    if not is_available():
        raise EnvError("browser extra not installed — run: pip install -e .[browser] && crawl4ai-setup")
    from urllib.parse import urlsplit
    throttle.wait(urlsplit(url).netloc)          # same per-host pacing as the static path
    try:
        from crawl4ai import AsyncWebCrawler      # verify the current API for the pinned version first
        import asyncio

        async def _run() -> str:
            async with AsyncWebCrawler(verbose=False) as crawler:
                res = await crawler.arun(url=url, page_timeout=int(cfg.crawl.timeout_s * 1000))
                return getattr(res, "html", "") or _md_to_html(getattr(res, "markdown", "") or "")

        return asyncio.run(_run())
    except Exception as e:                        # never let a render failure kill the run
        LOG.warning("render failed %s: %s", url, type(e).__name__)
        return ""


def _md_to_html(md: str) -> str:
    """crawl4ai may return markdown only; wrap it so the SAME extractors still work."""
    return f"<html><body><pre>{md}</pre></body></html>" if md else ""
```

**Before writing this**, confirm the crawl4ai API for the version pinned in `pyproject.toml` (`arun` vs
`arun_many`, sync vs async, attribute names). If it differs, follow the library, keep this function's
signature and return type identical.

### Step 3 — wire into the runner (`enrich/runner.py::_process_one`)
After the static crawl, insert:

```python
if res.needs_browser and browser.is_available():
    rendered = browser.fetch_rendered(b["website"], cfg, throttle)
    if rendered:
        text = crawler_cls.extract_text(rendered)
        # run the SAME extractors — never duplicate extraction logic
        for email, label in extract_emails(rendered, text).items():
            out["emails"].setdefault(email, {"label": label, "url": b["website"]})
        for cand in extract_people(text, b["website"]):
            out["people"].append(cand)
        out["needs_browser"] = False
        out["rendered"] = True
```

Cap it: **render at most 3 pages per site** and only when the static pass found nothing.

### Step 4 — rules
- Do **not** bypass robots.txt for rendered pages (check with the same `SiteCrawler._allowed`).
- Do **not** add stealth/undetected flags, proxies-by-default, or fingerprint options. This is for rendering
  JS, not for defeating anything (`icm/SCOPE.md` #1).
- If the extra is missing, behavior must be exactly as today: `needs_browser` stays true and shows in the digest.

### Step 5 — acceptance
- [ ] `tests/test_browser.py` starts with `pytest.importorskip("crawl4ai")`; asserts a JS-shell fixture yields
      ≥1 email or person that the static path missed.
- [ ] Without the extra installed: full suite still green, `needs_browser` still reported.
- [ ] `leadforge enrich --stage site` on a run with flagged sites reduces the `needs_browser` count.
- [ ] Tick U4.5 in `icm/STATE.md`; commit `U4.5: browser escalation for JS-rendered sites`.

---

## U4.6 — Public registry cross-check (opt-in)

**Why:** corroborating an owner/officer name from an official registry raises confidence and fills DM gaps.

**File:** `src/leadforge/providers/registry.py` (stub). Enabled only when a key is configured
(`registry.companies_house_key` / `registry.opencorporates_token` in `leadforge.yaml`).

### Implement exactly this interface

```python
class RegistryProvider(Protocol):
    def jurisdictions(self) -> set[str]: ...          # e.g. {"GB"}
    def lookup(self, business_row) -> list[Person]: ...  # never raises; [] on any problem
```

**CompaniesHouseRegistry** (UK):
1. Auth: HTTP Basic, key as username, empty password. Base `https://api.company-information.service.gov.uk`.
2. `GET /search/companies?q=<business name>&items_per_page=3`.
3. Accept a match **only if** the company's address locality or postcode overlaps the business's
   `address_city`/`address_postal`. No overlap → return `[]` (a wrong match is worse than none).
4. `GET /company/{company_number}/officers?register_type=directors` → for each **active** officer
   (`resigned_on` absent) build `Person(business_id=…, name=<title-cased>, title=<officer_role humanized>,
   labeled_by="registry", is_dm=0, source_url=<company profile URL>)`.
5. Rate limit: **600 requests / 5 minutes**. Implement a module-level token bucket. On HTTP 429: sleep 60s
   once, then disable this registry for the rest of the run.

**OpenCorporatesRegistry**: `GET https://api.opencorporates.com/v0.4/companies/search?q=<name>&api_token=…`
plus `jurisdiction_code` (`us_tx`, `gb`, …). Same matching discipline. Treat 403/429 as "disable for this run".

### Rules
- Never call a registry without its key configured — the module's network paths must not even be reached.
- Never block the pipeline: wrap everything, log, return `[]`.
- Record `Evidence(fact="registry_officer", url=<profile url>, snippet=<role + appointed_on>)` per person.
- Use only the documented public endpoints within published limits. Do not scrape around auth or rate caps.

### Acceptance
- [ ] `tests/test_registry.py` with canned JSON fixtures (no network) proves: correct `Person` mapping,
      locality-mismatch → `[]`, 429 → disabled without raising.
- [ ] With no keys: unit is a silent no-op and the whole suite passes.
- [ ] With a key: a known company yields ≥1 officer row and `data_confidence` scoring rises for it
      (`score.py::_f_data_confidence` already rewards `labeled_by == "registry"` — verify it lights up).
- [ ] Tick U4.6; commit `U4.6: public registry cross-check`.

---

## U4.7 — GLiNER decision-maker candidate upgrade (opt-in)

**Why:** the shipped extractor is a keyword/proximity heuristic. GLiNER (zero-shot NER) finds name+title pairs
the heuristic misses, giving the agent better candidates. The agent still makes the final call (ADR-003).

**File:** hook inside `src/leadforge/enrich/extract.py`, selected in `enrich/runner.py`.

```python
def extract_people_ner(text: str, source_url: str, max_candidates: int = 8) -> list[PersonCandidate]:
    """GLiNER path. Import lazily; caller falls back to extract_people() when unavailable."""
    from gliner import GLiNER
    model = _gliner_model()                      # module-level cache; load once per process
    ents = model.predict_entities(text[:6000], ["person name", "job title"], threshold=0.5)
    # pair each "person name" with the nearest "job title" within 80 characters, then build
    # PersonCandidate(name=…, title=…, snippet=<=300 chars around the pair, source_url=source_url)
```

Selection rule in the runner: `extract_people_ner` **if** `gliner` imports **else** `extract_people`. Identical
return type, identical caps (≤8 candidates, ≤300-char snippets) — the DM export format must not change.

### Acceptance
- [ ] On a fixture team page, the GLiNER path returns ≥ as many correct name/title pairs as the heuristic.
- [ ] Tests skip cleanly when `gliner` is absent; suite green either way.
- [ ] `leadforge dm export` output format is byte-compatible with before.
- [ ] Tick U4.7; commit `U4.7: GLiNER decision-maker candidate extraction`.

---

## U4.8 — Social & video presence signals via Agent-Reach (opt-in)

**Why:** the crawler already extracts the social/video profiles a business publishes on its own site. Knowing
whether those profiles exist and are *active* is a strong, honest outreach signal — "no YouTube channel",
"Instagram hasn't been posted to since 2024" — and directly feeds the need-hook column.

**Backend:** [Agent-Reach](https://github.com/Panniantong/Agent-Reach) (`pip install agent-reach`), already
installed on the operator's machine. Routes to free OSS backends (yt-dlp, OpenCLI, Jina Reader, …) for
YouTube, Facebook, Instagram, X, Reddit, RSS, GitHub, web search. Zero API fees.

**File:** `src/leadforge/providers/social.py` — **the stub's docstring is the complete binding spec**
(function signatures, return shapes, wiring points, config keys). Read it and follow it literally.

### Boundaries specific to this unit — these are not negotiable
1. **LinkedIn is excluded.** Agent-Reach can reach it; LeadForge must not (`icm/SCOPE.md` #3).
   `filter_networks()` already drops it — call it, and write a test that proves LinkedIn never reaches a
   subprocess call.
2. **Logged-out public access only.** Agent-Reach supports cookie/browser-session auth for several
   platforms. Never configure or use those paths (`icm/SCOPE.md` #2). Unreadable logged-out → `unknown`.
3. **Only profiles the business links from its own site.** Never search a platform for a person, and never
   profile a private individual (`icm/SCOPE.md` #7).
4. **Metadata only** — existence, last-activity date, follower count, profile URL. Do not store posts,
   comments, images, or video transcripts.
5. **Never blocks the run.** Any failure → log, return `{}`, continue.

### What is already wired for you (do not re-plumb)
- `SocialCfg` in `config.py` + the `social:` block in `config/leadforge.example.yaml` (all defaults off).
- Signal names in `models.py::SOFT_QUALIFIERS`: `stale_social`, `no_social_presence`, `no_video_presence`.
- Hook templates for those three in `src/leadforge/data/scoring.default.yaml`.
- `score.py::_need_hits` already reads them from `enrich_json["signals"]`.

So your job is only: implement `is_available` / `presence` / `to_signals` per the docstring, and call them
from `enrich/runner.py::_persist` after socials are extracted.

### Acceptance
- [ ] `tests/test_social.py`: `filter_networks` drops linkedin; `presence()` with a mocked subprocess returns
      the documented shape; a failing/missing binary returns `{}` without raising; **a test asserting no
      subprocess is ever invoked with a linkedin URL**.
- [ ] With `social.enabled: false` (default): zero behavior change, full suite green.
- [ ] With it enabled on a real run: businesses gain `enrich_json["social_presence"]`, and a business whose
      site links no YouTube gets the `no_video_presence` hook in the exported sheet.
- [ ] Tick U4.8 in `icm/STATE.md`; commit `U4.8: social/video presence signals via Agent-Reach`.

### Optional follow-on (only if the operator asks)
Agent-Reach's web-search and Jina Reader backends could serve as another escalation for sites the static
crawler can't read (an alternative to U4.5). Don't build it speculatively — U4.5 covers that need.

---

## Stage-wide rules (apply to all four units)

1. Reuse `SiteCrawler`'s robots check and `HostThrottle`. Never open a socket that skips them.
2. Every extracted fact gets an `Evidence` row with a URL and timestamp — that provenance is what makes the
   compliance posture real (`docs/07`).
3. Email validation stops at syntax + MX + disposable + role. **No SMTP RCPT probing, ever** (`icm/SCOPE.md` #5).
4. Business/professional contact data only. If an extractor could capture special-category personal data,
   drop it rather than store it (`icm/SCOPE.md` #6).
5. Optional extras must degrade silently and visibly: absent = the old behavior + a digest warning, never a crash.
