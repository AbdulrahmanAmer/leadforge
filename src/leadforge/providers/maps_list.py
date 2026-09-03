"""maps_list — native list-first Google Maps discovery provider (speed unit, docs/09, ADR-014).

WHY: gosom visits every place page (subprocess, ~30+ min for a full tiled sweep). The Google Maps
results LIST itself — the div[role="feed"] a person scrolls through — already carries name, CID,
lat/lng, rating, review count, category, street line, open/closed line, phone (119-120/120 cards
measured) and website (83-91/120, as a google.com/url redirect that must be unwrapped). Full postal
address, opening hours and Business-Profile "about" extras are NOT in the list and need a place-page
visit (see visit_details below). Plain Playwright, headless Chromium, identifying UA, locale en-GB,
consent handled by clicking "Reject all" like a person — NO stealth, NO fingerprint changes, NO
anti-bot evasion (icm/SCOPE.md #1; ADR-014 records the owner decision that a self-written engine is
in scope as long as it stays exactly this: plain Playwright, no evasion).

MEASURED (probe_list.py, probe_list2.py; scratchpad/speed/probe_list2_result.json), one browser,
two back-to-back searches: 13.3s to feed / 14.8s scrolling / 28.1s total for the first search (120
cards, end_of_list=True); 9.4s / 15.3s / 24.6s for the second (warm browser, 120 cards). Live-proof
numbers for THIS module are in the module's LIVE PROOF report (see the build report), not repeated
here to avoid the two drifting apart.

DEDUPE: a maps_list card carries no place_id (the list view never exposes one) — only a CID, found in
the card href as `!1s0x<fid>:0x<cid>`. The SECOND hex value is Google's canonical numeric CID; this
module hex-decodes it to the same decimal string gosom's own `cid` field already carries for the same
place (verified live 2026-09-03 against a 3195-row campaign DB: gosom populates `cid` on 1530 rows,
already decimal — hex-decoding a maps_list card's cid reproduces it exactly). Storing the DECIMAL
form (not the raw hex, an earlier version of this module's mistake) is what makes a maps_list card's
cid directly comparable to a gosom row's — both the known-place skip below and normalize.py's
`dedupe_key = f"cid:{cid}"` (used when there is no place_id) depend on the two providers agreeing on
one representation of the same identifier. A maps_list row for a business gosom already found under a
place_id-keyed dedupe_key still won't match on dedupe_key or place_id (gosom's key is `pid:...`, not
`cid:...`) — it merges through db.upsert_business's phone fallback (`_phone_match`) instead, which
requires a shared phone number AND (a shared name token OR the same postcode district). Phone is
present on ~99% of cards (measured 119-120/120 live), so in practice almost every maps_list card that
duplicates an existing business merges cleanly either way.

KNOWN-PLACE SKIP: pipeline.run_discover sets `provider.known_cids` (a set of every CID already in the
DB) before each fetch. Cards whose CID is already known are still returned as listings — refreshing
rating/review_count/last_seen_at via the normal upsert/merge path is cheap and useful — but are marked
`data["known"] = True` and are excluded from the (opt-in) place-page detail visit, which is the
expensive step this skip exists to avoid repeating.

POLITENESS: `search_delay_s` (+/-30% jitter) between searches sharing one browser context; a
per-instance token-bucket cap at `max_searches_per_hour`; the identifying UA below; block detection
(a "sorry/index" redirect, a captcha iframe, or "unusual traffic" text) raises ProviderDegraded so the
pipeline's existing cooldown/retry handles it exactly like a gosom captcha — this module never retries
a block itself. No proxies beyond `cfg.discovery.proxies` passthrough (Playwright's `proxy=` option).
"""

from __future__ import annotations

import atexit
import os
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from leadforge import __version__ as LEADFORGE_VERSION
from leadforge.config import Config
from leadforge.grid import PlannedQuery, Tile
from leadforge.models import RawListing
from leadforge.providers.base import DiscoveryProvider, register
from leadforge.util import LOG, ProviderDegraded, now_iso

FEED_SELECTOR = 'div[role="feed"]'
CARD_LINK_SELECTOR = 'div[role="feed"] a.hfpxzc'
_END_OF_LIST_TEXT = "text=/reached the end of the list/i"

# !1s0x<fid>:0x<cid> — the SECOND hex value is Google's canonical numeric CID (verified live
# 2026-09-03: hex-decoding it reproduces exactly the decimal string gosom's own `cid` field already
# carries for the same place, e.g. hex 0xdc576aff5f40dc49 -> decimal 15877276656365263945). The
# capture group below is the cid half only; the fid half (place-within-Google's-graph, not the
# business identity) is discarded. parse_card() hex-decodes this to a decimal string so a maps_list
# card's cid is directly comparable to — and mergeable with — a gosom-sourced business's cid.
_CID_RE = re.compile(r"!1s0x[0-9a-f]+:(0x[0-9a-f]+)")
_LATLNG_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
# a rating line renders as "4.8" or "4.9(199)" (no space) — one text node, no newline between them
_RATING_RE = re.compile(r"^(\d(?:\.\d)?)\s*\(?([\d,]+)?\)?$")
_OPEN_RE = re.compile(r"^(open|closed|opens)\b", re.IGNORECASE)
_BLOCK_MARKERS = ("unusual traffic", "recaptcha", "g-recaptcha", "detected unusual traffic")
_BLOCK_URL_MARKERS = ("/sorry/", "sorry/index")

# Ported from scratchpad/speed/probe_list2.py's extraction JS: raw DOM -> per-card dict. Semantic
# parsing (category/street/open-state/rating/phone split, CID + lat/lng regex, website-redirect
# unwrap) is done in pure Python below (_parse_card / _card_to_data) so it is unit-testable without
# a browser at all.
_EXTRACT_JS = r"""() => {
  // resolve against a fixed base rather than the DOM `.href` IDL property: a live google.com page
  // resolves relative hrefs against its own origin either way, but the offline fixture test
  // (page.set_content(), no real origin) can only resolve correctly if we say what to resolve
  // against — resolving explicitly makes this identical in both contexts.
  const abs = (el) => {
    const raw = el ? el.getAttribute('href') : null;
    if (!raw) return null;
    try { return new URL(raw, 'https://www.google.com/').href; } catch (e) { return raw; }
  };
  const out = [];
  for (const a of document.querySelectorAll('div[role="feed"] a.hfpxzc')) {
    const card = a.closest('div[jsaction]') || a.parentElement;
    const txt = card ? card.innerText : '';
    const web = card ? card.querySelector('a[data-value="Website"]') : null;
    const lines = txt.split('\n').map(s => s.trim()).filter(Boolean);
    out.push({
      name: a.getAttribute('aria-label'),
      href: abs(a) || '',
      website: abs(web),
      lines: lines,
    });
  }
  return out;
}"""


# --------------------------------------------------------------------------- pure helpers (testable)
def zoom_for_cell_km(cell_km: float) -> int:
    if cell_km <= 1.5:
        return 16
    if cell_km <= 3:
        return 15
    if cell_km <= 6:
        return 14
    return 13


def tile_viewport(tile: Tile) -> tuple[float, float, int]:
    """-> (lat, lng, zoom) for the tile's centre. Tile.bbox is [minLng, minLat, maxLng, maxLat]."""
    min_lng, min_lat, max_lng, max_lat = tile.bbox
    lat = (min_lat + max_lat) / 2
    lng = (min_lng + max_lng) / 2
    return lat, lng, zoom_for_cell_km(tile.cell_km)


def search_url(query_text: str, tile: Tile | None) -> str:
    q = (query_text or "").strip().replace(" ", "+")
    if tile is not None:
        lat, lng, zoom = tile_viewport(tile)
        return f"https://www.google.com/maps/search/{q}/@{lat},{lng},{zoom}z?hl=en"
    # no tile: omit the @lat,lng,zoom viewport entirely and let Maps resolve "<query> in <area>" itself
    return f"https://www.google.com/maps/search/{q}?hl=en"


def unwrap_website(raw_href: str | None) -> str | None:
    """A card's website link is `https://www.google.com/url?q=<real url>&opi=...&sa=U&ved=...&usg=...`.
    Unwrap it (URL-decode the `q` param) so normalize.canonical_website sees the real site."""
    if not raw_href:
        return None
    parts = urlsplit(raw_href)
    if parts.netloc.endswith("google.com") and parts.path == "/url":
        qs = parse_qs(parts.query)
        real = qs.get("q", [None])[0]
        return unquote(real) if real else None
    return raw_href


def _locality_and_country_from_query(query: PlannedQuery) -> tuple[str, str | None]:
    """PlannedQuery objects rebuilt from the DB (pipeline.run_discover) carry only text + tile —
    category/area are not persisted — so, like dvsa.py's `_locality_from_query`, this parses the
    locality back out of `text` ("<cat> in <area>[, <country>]"). Returns (locality, country);
    country is the segment after the LAST comma in the area phrase, or None when there isn't one
    (e.g. a bbox-mode plan whose text carries no area at all)."""
    area = (query.area or "").strip()
    text = query.text or ""
    if not area:
        idx = text.rfind(" in ")
        area = text[idx + len(" in "):].strip() if idx != -1 else ""
    if not area:
        return "", None
    parts = [p.strip() for p in area.split(",") if p.strip()]
    if not parts:
        return "", None
    locality = parts[0]
    country = parts[-1] if len(parts) > 1 else None
    return locality, country


def parse_rating(lines: list[str]) -> tuple[str | None, str | None]:
    for ln in lines:
        m = _RATING_RE.match(ln)
        if m:
            return m.group(1), m.group(2)
    return None, None


def parse_category_street(lines: list[str]) -> tuple[str | None, str | None]:
    """The category/street line looks like 'Auto repair shop ·  · 6 Cairo St' — category first
    segment, street last segment (a middle empty segment, presumably a price-level slot, is common
    and dropped). Skipped: the rating line (no middot) and the open/closed line (starts with
    open/closed/opens, also middot-separated but semantically different)."""
    for ln in lines:
        if "·" not in ln or _OPEN_RE.match(ln):
            continue
        segs = [s.strip() for s in ln.split("·") if s.strip()]
        if not segs:
            continue
        category = segs[0]
        street = segs[-1] if len(segs) > 1 else None
        return category, street
    return None, None


def parse_open_and_phone(lines: list[str]) -> tuple[str | None, str | None]:
    """The open-state line looks like 'Closed · Opens 8:30 AM Thu · +44 115 970 8888' — open_state
    is the first segment, phone (when present) is the last segment IF it looks phone-shaped
    (>= 6 digit characters; a bare hours fragment like 'Open 24 hours' has too few digits to match)."""
    for ln in lines:
        if not _OPEN_RE.match(ln):
            continue
        segs = [s.strip() for s in ln.split("·") if s.strip()]
        if not segs:
            return None, None
        open_state = segs[0]
        phone = None
        if len(segs) > 1:
            last = segs[-1]
            if sum(c.isdigit() for c in last) >= 6:
                phone = last
        return open_state, phone
    return None, None


def _cid_from_href(href: str) -> str | None:
    """The hex cid half of !1s0x<fid>:0x<cid>, hex-decoded to the decimal string gosom's own `cid`
    field uses for the same place — see _CID_RE's comment for how this was verified live."""
    m = _CID_RE.search(href)
    if not m:
        return None
    try:
        return str(int(m.group(1), 16))
    except ValueError:
        return None


def parse_card(raw: dict) -> dict | None:
    """One extracted card (JS output) -> a flat dict of parsed fields, or None for a card with no
    name (defensive — every real card has one, but a DOM change should degrade, not crash)."""
    name = (raw.get("name") or "").strip()
    if not name:
        return None
    href = raw.get("href") or ""
    ll_m = _LATLNG_RE.search(href)
    lines = raw.get("lines") or []
    rating, review_count = parse_rating(lines)
    category, street = parse_category_street(lines)
    open_state, phone = parse_open_and_phone(lines)
    return {
        "name": name,
        "href": href,
        "cid": _cid_from_href(href),
        "lat": float(ll_m.group(1)) if ll_m else None,
        "lng": float(ll_m.group(2)) if ll_m else None,
        "rating": rating,
        "review_count": review_count,
        "category": category,
        "street": street,
        "open_state": open_state,
        "phone": phone,
        "website": unwrap_website(raw.get("website")),
    }


def card_to_data(parsed: dict, locality: str, country: str | None) -> dict:
    """Parsed card -> RawListing.data. Keys match GOSOM_FIELD_MAP's raw names deliberately (title,
    web_site, review_rating, ...) so normalize.to_business dispatches correctly even without this
    module's own FIELD_MAP; FIELD_MAP is still declared explicitly below (maintenance surface, like
    gosom's own field-map docstring says)."""
    street = parsed.get("street")
    if street and locality:
        address = f"{street}, {locality}"
    else:
        address = street or None
    return {
        "title": parsed["name"],
        "place_id": None,  # the list view never exposes one; cid is the dedupe anchor (normalize.py)
        "cid": parsed.get("cid"),
        "phone": parsed.get("phone"),
        "web_site": parsed.get("website"),
        "review_rating": parsed.get("rating"),
        "review_count": parsed.get("review_count"),
        "category": parsed.get("category"),
        "categories": [parsed["category"]] if parsed.get("category") else [],
        "address": address,
        "complete_address": {
            "street": street,
            "city": locality or None,
            "country": country,
        },
        "latitude": parsed.get("lat"),
        "longitude": parsed.get("lng"),
        "open_state": parsed.get("open_state"),
        "link": parsed.get("href"),
        "list_only": True,
    }


def _chromium_binary_present() -> tuple[bool, str]:
    """Cheap capability probe: no subprocess, no browser launch — just import + a filesystem glob
    against Playwright's own browser cache layout. `available()` must never be expensive; a caller
    (the pipeline's provider-chain availability check) may call it once per query."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "playwright not installed — pip install -e .[browser]"

    candidates: list[Path] = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        candidates.append(Path(env_path))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "ms-playwright")
    else:
        candidates.append(Path.home() / ".cache" / "ms-playwright")
        candidates.append(Path.home() / "Library" / "Caches" / "ms-playwright")

    exe_names = ("chrome.exe", "chrome", "headless_shell", "headless_shell.exe")
    for base in candidates:
        try:
            if not base.is_dir():
                continue
        except OSError:
            continue
        for exe in exe_names:
            hits = sorted(base.glob(f"chromium*/**/{exe}"))
            if hits:
                return True, str(hits[-1])
    return False, "chromium binary not found — run: playwright install chromium"


class _TokenBucket:
    """Simple per-instance rate limiter: at most `per_hour` acquire() calls in any trailing 3600s
    window, sleeping (never raising) when the bucket is full — the same "back off, don't fail"
    posture as HostThrottle."""

    def __init__(self, per_hour: int):
        self.per_hour = max(1, per_hour)
        self._times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._times = [t for t in self._times if now - t < 3600]
            if len(self._times) >= self.per_hour:
                sleep_for = 3600 - (now - self._times[0])
                if sleep_for > 0:
                    LOG.warning("maps_list: rate bucket full (%d/hr) — sleeping %.0fs",
                                self.per_hour, sleep_for)
                    time.sleep(sleep_for)
            self._times.append(time.monotonic())


def _jittered(base: float, frac: float = 0.3) -> float:
    return max(0.2, base * (1 + random.uniform(-frac, frac)))


# --------------------------------------------------------------------------- provider
@register
class MapsListProvider(DiscoveryProvider):
    name = "maps_list"
    supports_tiles = True

    # v0.3 speed unit: explicit even though these key names already match GOSOM_FIELD_MAP's raw
    # names 1:1 (deliberate — see card_to_data docstring) — keeps this provider's contract visible
    # and independent of gosom's map ever drifting.
    FIELD_MAP = {
        "name": ["title"],
        "category": ["category"],
        "categories": ["categories"],
        "address": ["address"],
        "complete_address": ["complete_address"],
        "phone": ["phone"],
        "website": ["web_site", "website"],
        "rating": ["review_rating", "rating"],
        "review_count": ["review_count", "reviews"],
        "lat": ["latitude"],
        "lng": ["longitude"],
        "place_id": ["place_id"],
        "cid": ["cid"],
        "maps_url": ["link", "url"],
    }

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._consent_done = False
        self._last_search_at: float | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._bucket = _TokenBucket(cfg.discovery.maps_list.max_searches_per_hour)
        # pipeline.run_discover sets this before fetch() when it knows about known CIDs (v0.3
        # speed unit known-place skip); absent/empty means "treat every card as new".
        self.known_cids: set[str] = set()
        atexit.register(self.close)

    # --- capability probe --------------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        ok, detail = _chromium_binary_present()
        if not ok:
            return False, f"maps_list: {detail}"
        return True, f"maps_list: chromium ready ({Path(detail).name})"

    # --- lifecycle -----------------------------------------------------------------------------
    def _ensure_browser(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) LeadForgeBot/{LEADFORGE_VERSION} (internal lead research)"
        self._pw = sync_playwright().start()
        launch_kwargs: dict = {"headless": True}
        proxies = self.cfg.discovery.proxies
        if proxies:  # Playwright's own proxy option — no fingerprint/stealth changes (icm/SCOPE.md #1)
            launch_kwargs["proxy"] = {"server": proxies[0]}
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            locale="en-GB", viewport={"width": 1280, "height": 900}, user_agent=ua,
        )
        self._page = self._context.new_page()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for obj, closer in ((self._context, "close"), (self._browser, "close")):
                if obj is not None:
                    try:
                        getattr(obj, closer)()
                    except Exception:  # noqa: BLE001 — teardown must never raise
                        pass
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._page = self._context = self._browser = self._pw = None

    def __del__(self):  # noqa: D105
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # --- politeness ------------------------------------------------------------------------------
    def _politeness_wait(self) -> None:
        base = self.cfg.discovery.maps_list.search_delay_s
        if self._last_search_at is not None:
            target = self._last_search_at + _jittered(base)
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._last_search_at = time.monotonic()

    # --- block / consent ---------------------------------------------------------------------
    def _check_block(self, page) -> None:
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            url = ""
        if any(m in url for m in _BLOCK_URL_MARKERS):
            raise ProviderDegraded("maps_list: block page (sorry/index redirect)")
        try:
            has_captcha_iframe = page.locator('iframe[src*="recaptcha"]').count() > 0
        except Exception:  # noqa: BLE001
            has_captcha_iframe = False
        if has_captcha_iframe:
            raise ProviderDegraded("maps_list: block page (captcha iframe)")
        try:
            content = page.content().lower()
        except Exception:  # noqa: BLE001
            content = ""
        if any(m in content for m in _BLOCK_MARKERS):
            raise ProviderDegraded("maps_list: block page (unusual traffic)")

    def _handle_consent(self, page) -> None:
        if self._consent_done:
            return
        for sel in ('button:has-text("Reject all")', 'button:has-text("Alle ablehnen")',
                    'form[action*="consent"] button'):
            try:
                page.locator(sel).first.click(timeout=4000)
                self._consent_done = True
                return
            except Exception:  # noqa: BLE001 — no consent wall this session/region; that's fine
                continue

    # --- scrolling -----------------------------------------------------------------------------
    def _scroll_to_end(self, page, max_cards: int, max_steps: int = 60) -> bool:
        """Measured live 2026-09-03 (tile-zoomed search, zoom 15 from a 3km cell — a tighter view
        than probe_list2.py's untiled zoom-13 searches): card growth is BURSTIER at a closer zoom —
        several consecutive scrolls can each take the full wait_for_function timeout with zero
        growth, then growth resumes. A first cut of this method ported probe2's stall=4 threshold
        verbatim but DROPPED probe2's extra `time.sleep(0.6)` pacing between polls; the combination
        stopped at 40/120 cards with end_of_list still False — a real premature-stall bug, not a
        genuine end of results (confirmed by re-scrolling the identical tile with no stall logic at
        all: it reached the true end, 120 cards, at step 29). Fixed here: the 0.6s pacing is
        restored and the stall threshold is raised to 8 (worst case ~8 * (2.5s wait + 0.6s pace) =
        ~25s of extra patience beyond the last real growth, which is still well inside the
        per-search politeness budget)."""
        last = -1
        stalls = 0
        for _ in range(max_steps):
            feed = page.query_selector(FEED_SELECTOR)
            if feed is None:
                return False
            page.evaluate("(el) => el.scrollTo(0, el.scrollHeight)", feed)
            try:
                page.wait_for_function(
                    "(n) => document.querySelectorAll('div[role=\"feed\"] a.hfpxzc').length > n",
                    arg=last, timeout=2500,
                )
            except Exception:  # noqa: BLE001 — a stalled page just means "count didn't grow" yet
                pass
            if page.locator(_END_OF_LIST_TEXT).count():
                return True
            n = page.locator(CARD_LINK_SELECTOR).count()
            if n >= max_cards:
                return False
            stalls = stalls + 1 if n == last else 0
            last = n
            if stalls >= 8:
                return False
            time.sleep(0.6)  # extra pacing between polls — restores probe2's proven-working cadence
        return False

    # --- fetch -----------------------------------------------------------------------------------
    def fetch(self, query: PlannedQuery, limit: int | None = None) -> list[RawListing]:
        d = self.cfg.discovery.maps_list
        self._ensure_browser()
        self._bucket.acquire()
        self._politeness_wait()

        url = search_url(query.text, query.tile)
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:  # noqa: BLE001 — Playwright's own TimeoutError subclasses Exception
            raise ProviderDegraded(f"maps_list: navigation failed for '{query.text}': "
                                    f"{type(e).__name__}") from e

        self._check_block(self._page)
        self._handle_consent(self._page)

        try:
            self._page.wait_for_selector(FEED_SELECTOR, timeout=30_000)
        except Exception as e:  # noqa: BLE001
            self._check_block(self._page)
            if "/maps/place/" in (self._page.url or ""):
                # a very specific query can redirect straight to one place page instead of a list —
                # rare edge case, not an error; just nothing to list here.
                LOG.info("maps_list: single-place redirect for '%s' — no list to scrape", query.text)
                return []
            raise ProviderDegraded(f"maps_list: results feed never appeared for '{query.text}'") from e

        # `limit` is a soft cap on OUTPUT for every other provider (gosom truncates listings the
        # same way) — but for maps_list, honoring it here too means a smoke-test call
        # (`discover --limit 60`, or a query close to caps.max_leads) doesn't pay for a full
        # scroll-to-end when far fewer cards are wanted. Measured live 2026-09-03: an untruncated
        # scroll on a fresh tile costs ~25s regardless of `limit`; capping the scroll target itself
        # is what actually saves that time (a card total on screen may include known/duplicate
        # cards, so this remains a soft cap exactly like gosom's, not a guarantee of N new rows).
        scroll_cap = min(d.max_cards, limit) if limit else d.max_cards
        end_of_list = self._scroll_to_end(self._page, scroll_cap)
        raw_cards = self._page.evaluate(_EXTRACT_JS)
        if len(raw_cards) > d.max_cards:
            raw_cards = raw_cards[: d.max_cards]

        locality, country = _locality_and_country_from_query(query)
        fetched_at = now_iso()
        seen_cids: set[str] = set()
        listings: list[RawListing] = []
        for raw in raw_cards:
            parsed = parse_card(raw)
            if parsed is None:
                continue
            cid = parsed.get("cid")
            if cid:
                if cid in seen_cids:
                    continue
                seen_cids.add(cid)
            data = card_to_data(parsed, locality, country)
            if cid and cid in self.known_cids:
                data["known"] = True
            listings.append(RawListing(provider=self.name, fetched_at=fetched_at, data=data))

        if limit:
            listings = listings[:limit]

        if d.visit_details:
            self._visit_details(listings, d)

        LOG.info("maps_list: %d listing(s) for '%s' (end_of_list=%s, cards_seen=%d)",
                 len(listings), query.text, end_of_list, len(raw_cards))
        return listings

    # --- optional place-page detail visit -------------------------------------------------------
    def _visit_details(self, listings: list[RawListing], d) -> None:
        targets = [ln for ln in listings if not ln.data.get("known") and ln.data.get("link")]
        if not targets:
            return
        n_tabs = max(1, min(d.detail_tabs, len(targets)))
        pages = []
        try:
            pages = [self._context.new_page() for _ in range(n_tabs)]
            for i, listing in enumerate(targets):
                self._visit_one_detail(pages[i % n_tabs], listing, d)
        finally:
            for p in pages:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    pass

    def _visit_one_detail(self, page, listing: RawListing, d) -> None:
        url = listing.data.get("link")
        if not url:
            return
        time.sleep(_jittered(d.delay_s))
        t0 = time.monotonic()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            self._check_block(page)
            # domcontentloaded can fire before the place-page SPA hydrates the info panel (measured
            # live 2026-09-02: without this, full_address landed on only 6/10 cards); a short
            # best-effort wait for the address button — never fatal, never blocks past its timeout —
            # meaningfully improves the hit rate for a field this module exists to add.
            try:
                page.wait_for_selector('button[data-item-id="address"]', timeout=3000)
            except Exception:  # noqa: BLE001 — some places genuinely don't show an address button
                pass
            _extract_detail_fields(page, listing.data)
        except ProviderDegraded:
            raise  # a block is a block, regardless of which page hit it
        except Exception as e:  # noqa: BLE001 — any selector miss = field absent, never a crash
            LOG.debug("maps_list: detail visit failed for %s: %s", url, type(e).__name__)
        finally:
            listing.data["detail_visit_s"] = round(time.monotonic() - t0, 2)


def _extract_detail_fields(page, data: dict) -> None:
    """Best-effort place-page extraction. Every lookup is wrapped so a selector miss (Google
    changes the DOM, the field genuinely isn't shown) leaves the field simply absent."""
    try:
        addr_el = page.locator('button[data-item-id="address"]').first
        if addr_el.count():
            aria = (addr_el.get_attribute("aria-label") or "").strip()
            if ":" in aria:
                aria = aria.split(":", 1)[1].strip()
            if aria:
                data["full_address"] = aria
    except Exception:  # noqa: BLE001
        pass
    try:
        plus_el = page.locator('button[data-item-id="oloc"]').first
        if plus_el.count():
            aria = (plus_el.get_attribute("aria-label") or "").strip()
            if ":" in aria:
                aria = aria.split(":", 1)[1].strip()
            if aria:
                data["plus_code"] = aria
    except Exception:  # noqa: BLE001
        pass
    try:
        # measured live 2026-09-03: no full weekly-hours table without an extra click/expand (not
        # "cheap"); this button's aria-label DOES carry a one-line summary for free, e.g.
        # "Thursday, Open 24 hours, Copy open hours" — cheap, honest about being TODAY's line only
        # (never conflated with normalize's `hours` dict field, which expects a structured weekly map).
        hours_el = page.locator('button[aria-label*="open hours"]').first
        if hours_el.count():
            aria = (hours_el.get_attribute("aria-label") or "").strip()
            aria = aria.replace(", Copy open hours", "").strip()
            if aria:
                data["hours_today_text"] = aria
    except Exception:  # noqa: BLE001
        pass
    if not data.get("web_site"):
        try:
            web_el = page.locator('a[data-item-id="authority"]').first
            if web_el.count():
                href = web_el.get_attribute("href")
                if href:
                    data["web_site"] = href
        except Exception:  # noqa: BLE001
            pass
    try:
        body_text = page.locator("body").inner_text(timeout=2000).casefold()
        if "appointment required" in body_text:
            data["appointments"] = "required"
        elif "appointment" in body_text and "recommended" in body_text:
            data["appointments"] = "recommended"
    except Exception:  # noqa: BLE001
        pass
