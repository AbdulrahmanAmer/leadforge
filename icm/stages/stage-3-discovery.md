# Stage 3 — Discovery: remaining units (PRESCRIPTIVE)

**Read `icm/SCOPE.md` first.** Everything here is ordinary API-client and subprocess work on public business
listings; nothing in this stage requires or permits evasion of any kind.

Prereqs: G0–G2 green (`pytest -q` → full suite green (see icm/STATE.md for the current count)). Files you will touch are named exactly.

---

## U3.6 — Fallback REST discovery provider

**Why:** if the primary engine's selectors break (Google changes markup), a second, independently-written
engine keeps campaigns running. Pure resilience, no new data sources.

**File to edit:** `src/leadforge/providers/fallback_rest.py` (currently a stub that raises).
**Engine:** `conor-is-my-name/google-maps-scraper` — MIT, Python/FastAPI, runs locally in Docker, exposes a
REST endpoint. The operator starts it themselves; we only call `http://localhost:8765` (configurable).

### Step 1 — read these first (5 min)
- `src/leadforge/providers/base.py` — the `DiscoveryProvider` ABC you must satisfy.
- `src/leadforge/providers/gosom.py` — a working implementation to mirror in style.
- `src/leadforge/models.py` → `RawListing` — the only thing `fetch()` may return.

### Step 2 — implement exactly this shape

```python
@register
class FallbackRestProvider(DiscoveryProvider):
    name = "fallback_rest"

    def available(self) -> tuple[bool, str]:
        """Health probe. MUST NOT raise. 3s timeout."""
        url = self.cfg.discovery.fallback_rest.url
        try:
            r = httpx.get(f"{url}/docs", timeout=3.0)
            return (r.status_code < 500), (f"rest up at {url}" if r.status_code < 500
                                           else f"rest unhealthy ({r.status_code})")
        except httpx.HTTPError:
            return False, f"fallback REST service not reachable at {url} (start its docker container)"

    def fetch(self, query: PlannedQuery, limit: int | None = None) -> list[RawListing]:
        """One text query -> RawListings. Raises ProviderDegraded on recoverable failure. NEVER ProviderFailed."""
        url = self.cfg.discovery.fallback_rest.url
        params = {"query": query.text, "max_results": limit or 100}
        try:
            r = httpx.get(f"{url}/scrape-get", params=params, timeout=30.0)
            r.raise_for_status()
            rows = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ProviderDegraded(f"fallback_rest failed on '{query.text}': {type(e).__name__}") from e
        if not isinstance(rows, list):
            raise ProviderDegraded(f"fallback_rest returned {type(rows).__name__}, expected list")
        return [RawListing(provider=self.name, fetched_at=now_iso(), data=self._map(row))
                for row in rows if isinstance(row, dict)]

    @staticmethod
    def _map(row: dict) -> dict:
        """Translate this engine's keys into the GOSOM-style keys normalize.py already understands.
        Keep every original key too, so nothing is lost if the map is incomplete."""
        out = dict(row)
        alias = {
            "name": "title", "business_name": "title",
            "website": "web_site", "url": "web_site",
            "phone_number": "phone",
            "rating": "review_rating", "reviews": "review_count", "num_reviews": "review_count",
            "lat": "latitude", "lng": "longitude", "lon": "longitude",
        }
        for src, dst in alias.items():
            if src in row and dst not in out:
                out[dst] = row[src]
        return out
```

Add the imports it needs at the top: `httpx`, `ProviderDegraded`, `now_iso` (see `leadforge.util`).

### Step 3 — rules you must not break
- `fetch()` **ignores `query.tile`** (this engine has no geo-tiling). Do not attempt to emulate tiling.
- Never raise `ProviderFailed` here — the chain in `pipeline._fetch_with_chain` decides what's fatal.
- Do not write to the database from the provider. Normalization + upsert happen in the pipeline.
- Do not add retries inside `fetch()`; the pipeline already handles degradation and the operator sets pacing.

### Step 4 — test (replaces the shipped xfail)
In `tests/test_providers.py`, delete the `test_fallback_rest_parse` xfail stub and write:

```python
def test_fallback_rest_maps_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = FallbackRestProvider(cfg)
    fake_rows = [{"name": "A Shop", "phone_number": "713-555-0100",
                  "website": "http://a.com", "reviews": 12, "lat": 29.7, "lng": -95.3}]
    monkeypatch.setattr("leadforge.providers.fallback_rest.httpx.get",
                        lambda *a, **k: _FakeResp(fake_rows))
    out = prov.fetch(PlannedQuery(text="auto repair in Houston", category="", area=""))
    assert len(out) == 1
    assert out[0].data["title"] == "A Shop"          # aliased
    assert out[0].data["web_site"] == "http://a.com" # aliased
    assert out[0].data["latitude"] == 29.7           # aliased

def test_fallback_rest_unreachable_is_degraded(tmp_path, monkeypatch):
    # httpx.get raising -> ProviderDegraded, never a crash
    ...
```
(`_FakeResp` = tiny class with `raise_for_status()` and `json()`; copy the pattern from `tests/test_geo_guards.py`.)

### Step 5 — acceptance checklist (all must be true)
- [ ] `pytest -q` green, `ruff check src tests` clean.
- [ ] `leadforge discover --provider fallback_rest --limit 5` with the service **down** → exits 0, digest
      `ok:true` with a warning naming the unreachable URL (degraded, not crashed).
- [ ] With the service **up**, the same command upserts ≥1 business.
- [ ] `providers: [gosom, fallback_rest]` in `leadforge.yaml` → gosom tried first, fallback used only after
      gosom degrades.
- [ ] Tick U3.6 in `icm/STATE.md`; commit as `U3.6: fallback REST discovery provider`.

---

## U3.7 — gosom serve mode (OPTIONAL — skip unless multi-hour runs are actually needed)

**File:** `src/leadforge/providers/gosom.py`.

Instead of one subprocess per query, launch the engine once with `-web` (it exposes a local REST API on
:8080 with OpenAPI docs at `/api/docs`) and submit jobs over HTTP.

**Mandatory first step:** start the binary with `-web`, open `http://localhost:8080/api/docs`, and read the
**actual** request/response schema. Do not implement from the README — it may be out of date. Write down the
real endpoint shapes in a comment above your code.

Then: add `discover --serve` to `cli.py`, keep subprocess mode as the default, and make sure the process is
terminated cleanly on exit (`try/finally`, `proc.terminate()`, timeout, then `kill`).

**Acceptance:** a 3-query run completes through the serve path with identical DB results to subprocess mode;
no orphaned process remains afterwards (`ps` check on macOS/Linux, Task Manager on Windows).

---

## Not in this stage

Live validation of the real scraper output (field drift, grid flags) is **U8.2** in
`stage-8-hardening.md`, because it needs network + the downloaded binary. Do U3.6 first — it is fully
testable offline.
