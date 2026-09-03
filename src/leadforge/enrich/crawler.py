"""Polite static-first website crawler (U4.1) — docs/04 §3.3, invariants docs/04 §5 (ADR-009).

Politeness is code, not convention: robots.txt honored, >= delay_s (+jitter) between requests to a host,
one request in flight per host, page/site caps, identifying User-Agent, hard timeouts.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from leadforge.config import Config
from leadforge.util import LOG, HostThrottle

PAGE_KEYWORDS = re.compile(
    r"(about|team|staff|people|leadership|founders?|management|meet|contact|impress|imprint|legal|careers?|jobs?)",
    re.IGNORECASE,
)
_CURRENT_YEAR = date.today().year  # staleness threshold = current - crawl.stale_after_years

# v0.3 unit C1: broadened booking_hint — "book" + a booking-shaped word within 4 words either side
# ("Book A Service", "Book an MOT" — the article "a"/"an" itself counts toward the 4-word distance).
_BOOKING_WORD_RE = re.compile(r"[A-Za-z']+")
_BOOKING_NEARBY_WORDS = {"now", "online", "appointment", "service", "mot", "your", "slot", "test"}
# Known booking platforms, matched against an anchor href or an iframe src (host or path substring).
_BOOKING_PLATFORMS = {
    "bookmygarage": ("bookmygarage",),
    "whocanfixmycar": ("whocanfixmycar",),
    "autodata": ("autodata",),
    "kwik-fit": ("kwik-fit", "kwikfit"),
    "calendly": ("calendly.com",),
    "acuity": ("acuityscheduling.com",),
    "squarespace-scheduling": ("squarespacescheduling.com", "squarespace-scheduling"),
    "setmore": ("setmore.com",),
    "simplybook": ("simplybook.me", "simplybook.it", "simplybook.com"),
    "fresha": ("fresha.com",),
    "treatwell": ("treatwell.co.uk", "treatwell.com", "treatwell.de", "treatwell.fr"),
}


def _booking_html_text(pages: list) -> str:
    """Text for the booking word-distance regex, derived from HTML — not trafilatura's boilerplate-
    stripped Page.text. trafilatura throws away nav/header/footer anchors as "boilerplate" (that IS
    where "Book Online"/"Book Now" nav links actually live), and on a short/single-anchor page it can
    return '' entirely. Parsing the raw HTML directly and keeping nav/header/footer content is what a
    real site's booking language requires; script/style/noscript/template content is decomposed first
    so it never contributes stray words."""
    parts = []
    for p in pages:
        tree = HTMLParser(p.html)
        for sel in ("script", "style", "noscript", "template"):
            for node in tree.css(sel):
                node.decompose()
        if tree.body:
            parts.append(tree.body.text(separator=" "))
    return "\n".join(parts)


def _booking_regex_hint(text_blob: str) -> bool:
    words = [w.casefold() for w in _BOOKING_WORD_RE.findall(text_blob)]
    for i, w in enumerate(words):
        if w != "book":
            continue
        window = words[max(0, i - 4) : i] + words[i + 1 : i + 5]
        if any(x in _BOOKING_NEARBY_WORDS for x in window):
            return True
    return False


def _booking_platform_hit(pages: list) -> str | None:
    """A known booking platform linked as an anchor href or embedded as an iframe src."""
    for p in pages:
        tree = HTMLParser(p.html)
        urls = [a.attributes.get("href") or "" for a in tree.css("a[href]")]
        urls += [f.attributes.get("src") or "" for f in tree.css("iframe[src]")]
        for url in urls:
            low = url.casefold()
            for name, needles in _BOOKING_PLATFORMS.items():
                if any(n in low for n in needles):
                    return name
    return None


@dataclass
class Page:
    url: str
    html: str
    text: str


@dataclass
class CrawlResult:
    ok: bool
    pages: list[Page] = field(default_factory=list)
    needs_browser: bool = False
    signals: dict = field(default_factory=dict)  # https, status, copyright_year, stale_site, has_booking_hint, careers
    error: str = ""



# v0.3 speed unit (2026-09-02): a DNS/connection failure means the HOST is unreachable, not just this
# one URL — the very next request to it (robots.txt, or page 2 of a franchise chain that shares this
# domain and gets its own fresh SiteCrawler instance in _process_one) would fail the exact same way
# after paying the exact same connect/DNS timeout again. Caching "this host is dead" at the CLASS level
# (shared across every SiteCrawler instance in the process — one instance per business) turns a chain of
# N businesses on one dead host into ONE timeout instead of N. Only true connection-establishment
# failures qualify (DNS resolution, refused/unreachable connect) — a slow-but-alive server (ReadTimeout)
# or an HTTP error status is NOT "dead" and must not short-circuit future attempts.
_CONNECTION_FAILURE_TYPES = (httpx.ConnectError, httpx.ConnectTimeout)


def _is_connection_failure(e: Exception) -> bool:
    return isinstance(e, _CONNECTION_FAILURE_TYPES)


class SiteCrawler:
    _dead_hosts: dict[str, str] = {}  # host -> reason, shared process-wide across all instances
    _dead_hosts_lock = threading.Lock()

    def __init__(self, cfg: Config, throttle: HostThrottle | None = None):
        self.cfg = cfg
        self.throttle = throttle or HostThrottle(cfg.politeness.delay_s)
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=cfg.crawl.timeout_s,
            headers={"User-Agent": cfg.politeness.user_agent, "Accept-Language": "en;q=0.9,*;q=0.5"},
        )

    def close(self) -> None:
        self.client.close()

    # --- dead-host cache (process-wide) -------------------------------------------
    @classmethod
    def _host_dead_reason(cls, host: str) -> str | None:
        with cls._dead_hosts_lock:
            return cls._dead_hosts.get(host)

    @classmethod
    def _mark_host_dead(cls, host: str, reason: str) -> None:
        with cls._dead_hosts_lock:
            cls._dead_hosts[host] = reason

    @classmethod
    def reset_dead_hosts(cls) -> None:
        """Test-only: the cache is process-wide by design, so a test that deliberately exercises a
        connection failure must clear it afterward or it leaks into unrelated tests reusing the host."""
        with cls._dead_hosts_lock:
            cls._dead_hosts.clear()

    # --- robots ------------------------------------------------------------------
    def _robots_for(self, base: str) -> urllib.robotparser.RobotFileParser:
        host = urlsplit(base).netloc
        rp = self._robots.get(host)
        if rp is not None:
            return rp
        rp = urllib.robotparser.RobotFileParser()
        dead = self._host_dead_reason(host)
        if dead is not None:
            rp.parse(["User-agent: *", "Disallow: /"])  # known-dead host -> unreachable -> disallow
            self._robots[host] = rp
            return rp
        try:
            self.throttle.wait(host)
            resp = self.client.get(urljoin(base, "/robots.txt"))
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            elif resp.status_code >= 500:
                # RFC 9309 §2.3.1.4: an UNREACHABLE robots.txt means assume complete disallow
                rp.parse(["User-agent: *", "Disallow: /"])
            else:
                rp.parse([])  # 4xx = 'unavailable' -> no robots published -> allow
        except httpx.HTTPError as e:
            if _is_connection_failure(e):
                self._mark_host_dead(host, f"transport:{type(e).__name__}")
            rp.parse(["User-agent: *", "Disallow: /"])  # transport failure = unreachable = disallow
        self._robots[host] = rp
        return rp

    def _allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return rp.can_fetch(self.cfg.politeness.user_agent, url) and rp.can_fetch("*", url)

    # --- fetch -------------------------------------------------------------------
    # A None from _get has four distinct causes; last_failure records which, so crawl() can
    # decide whether a real browser could plausibly do better (only for block-shaped statuses).
    _BLOCK_STATUSES = {401, 403, 405, 406, 429, 503}

    def _get(self, url: str, timeout: float | None = None) -> httpx.Response | None:
        """timeout overrides the client's default (home-page) timeout — pass cfg.crawl.page_timeout_s
        for secondary/candidate pages so a dead or slow non-home page costs less than the home fetch."""
        self.last_failure: str | None = None
        host = urlsplit(url).netloc
        dead = self._host_dead_reason(host)
        if dead is not None:
            self.last_failure = dead
            return None
        if not self._allowed(url):
            LOG.info("robots disallow: %s", url)
            self.last_failure = "robots"
            return None
        self.throttle.wait(host)
        try:
            resp = self.client.get(url) if timeout is None else self.client.get(url, timeout=timeout)
            if resp.status_code >= 400:
                self.last_failure = f"status:{resp.status_code}"
                return None
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                self.last_failure = "non-html"
                return None
            return resp
        except httpx.HTTPError as e:
            LOG.debug("fetch failed %s: %s", url, type(e).__name__)
            self.last_failure = f"transport:{type(e).__name__}"
            if _is_connection_failure(e):
                self._mark_host_dead(host, self.last_failure)
            return None

    # --- text extraction ---------------------------------------------------------
    @staticmethod
    def extract_text(html: str) -> str:
        try:
            import trafilatura

            text = trafilatura.extract(html, include_comments=False) or ""
            if text:
                return text
        except Exception:  # noqa: BLE001 — trafilatura is best-effort; selectolax fallback below
            pass
        tree = HTMLParser(html)
        for sel in ("script", "style", "noscript"):
            for node in tree.css(sel):
                node.decompose()
        return re.sub(r"\n{3,}", "\n\n", tree.body.text(separator="\n") if tree.body else "").strip()

    @staticmethod
    def looks_js_shell(html: str, text: str) -> bool:
        if len(text) >= 400:
            return False
        scripts = html.count("<script")
        root_div = bool(re.search(r'id=["\'](root|app|__next)["\']', html))
        return scripts >= 4 or root_div

    # --- page selection ----------------------------------------------------------
    def _candidate_links(self, base_url: str, home_html: str) -> list[str]:
        tree = HTMLParser(home_html)
        seen: dict[str, None] = {}
        base_host = urlsplit(base_url).netloc.removeprefix("www.")
        for a in tree.css("a[href]"):
            href = (a.attributes.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            full = urljoin(base_url, href)
            parts = urlsplit(full)
            if parts.netloc.removeprefix("www.") != base_host:
                continue
            label = f"{parts.path} {a.text() or ''}"
            if PAGE_KEYWORDS.search(label):
                seen.setdefault(full.split("#")[0], None)
        links = list(seen)
        if len(links) < 2:  # sitemap probe
            resp = self._get(urljoin(base_url, "/sitemap.xml"), timeout=self.cfg.crawl.page_timeout_s)
            if resp is not None:
                for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text)[:200]:
                    if PAGE_KEYWORDS.search(urlsplit(loc).path) and urlsplit(loc).netloc.removeprefix("www.") == base_host:
                        seen.setdefault(loc, None)
                links = list(seen)
        return links[: max(0, self.cfg.crawl.pages_per_site - 1)]

    # --- main --------------------------------------------------------------------
    def crawl(self, website: str, business_domain: str | None = None) -> CrawlResult:
        """business_domain (v0.3, optional): the listing's own domain, used only to compute
        signals["offsite_redirect"] (does the final URL's host differ, www-insensitive?). Existing
        callers that omit it keep working — offsite_redirect just reads False."""
        start = time.monotonic()
        result = CrawlResult(ok=False)
        if not self._allowed(website):
            result.error = "robots-disallowed"  # the site said no — the browser must not go either
            return result
        resp = self._get(website)  # home page: client default timeout (cfg.crawl.timeout_s)
        if resp is None:
            # v0.3 speed unit (2026-09-02, build item 1c): a block-shaped status or a connection error
            # on the HOME page returns right here — the candidate-links loop below is never reached, so
            # a dead/blocking host never pays for secondary-page fetches too. This was already true
            # structurally (the loop is unreachable from this branch); the test suite now proves it
            # against a real server rather than trusting the control flow by inspection.
            cause = getattr(self, "last_failure", None) or "unknown"
            result.error = f"home unreachable ({cause})"
            # Only a block-shaped refusal (WAF/bot wall) earns the rendered-browser retry — a
            # person's browser might still be served. Dead DNS, 404s, PDFs and timeouts get
            # nothing from Chromium and would just burn minutes per site.
            status = int(cause.split(":", 1)[1]) if cause.startswith("status:") else None
            result.needs_browser = status in self._BLOCK_STATUSES
            return result
        home_html = resp.text[: self.cfg.crawl.max_text_bytes]
        home_text = self.extract_text(home_html)
        result.pages.append(Page(str(resp.url), home_html, home_text))
        result.signals["https"] = str(resp.url).startswith("https://")
        result.signals["status"] = resp.status_code
        result.signals["http_status"] = resp.status_code
        final_host = urlsplit(str(resp.url)).netloc.casefold().removeprefix("www.")
        result.signals["final_host"] = final_host
        result.signals["offsite_redirect"] = (
            bool(business_domain) and final_host != business_domain.casefold().removeprefix("www.")
        )

        if self.looks_js_shell(home_html, home_text):
            result.needs_browser = True

        # v0.3 speed unit (2026-09-02, build item 1a): a per-site wall-clock budget — once exceeded,
        # stop fetching further pages and return what was already collected. The home page already
        # loaded (we're past the `resp is None` return above), so this is still ok=True: a partial
        # crawl beats none. Secondary pages get the shorter page_timeout_s (build item 1b) so a single
        # dead/slow non-home page can never eat more than its own small share of the budget.
        for link in self._candidate_links(str(resp.url), home_html):
            if time.monotonic() - start > self.cfg.crawl.site_budget_s:
                result.signals["budget_exhausted"] = True
                break
            sub = self._get(link, timeout=self.cfg.crawl.page_timeout_s)
            if sub is None:
                continue
            html = sub.text[: self.cfg.crawl.max_text_bytes]
            result.pages.append(Page(str(sub.url), html, self.extract_text(html)))

        result.signals.update(self.compute_signals(result.pages, self.cfg.crawl.stale_after_years))
        result.ok = True
        return result

    @staticmethod
    def compute_signals(pages: list[Page], stale_after_years: int) -> dict:
        """Content-derived signals; shared by the static path and the rendered-browser fallback,
        so a rescued site scores on the same evidence as a normally crawled one."""
        blob = "\n".join(p.html for p in pages)
        # v0.3 fix: the word-distance regex must read HTML-derived text (keeps nav anchors), NOT
        # trafilatura's Page.text — trafilatura strips nav/header/footer as boilerplate, which is
        # exactly where "Book Online"/"Book Now" nav links live (regressed True->False vs d7eb4ce).
        booking_text_blob = _booking_html_text(pages)
        signals: dict = {}
        years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\D{0,20}(20\d{2})", blob, re.IGNORECASE)]
        if years:
            latest = max(years)
            signals["copyright_year"] = latest
            signals["stale_site"] = latest < _CURRENT_YEAR - stale_after_years
        signals["careers"] = any(re.search(r"/(careers?|jobs?)\b", p.url) for p in pages)
        # v0.3: platform (iframe/anchor host) beats a plain text mention; text/word-distance regex is
        # the fallback ("book" + a booking-shaped word within 4 words either side, or the older phrasing
        # "schedule an appointment"). booking_source records which fired; "gbp" is set by unit C2.
        platform = _booking_platform_hit(pages)
        if platform:
            signals["booking_hint"] = True
            signals["booking_source"] = f"platform:{platform}"
        elif _booking_regex_hint(booking_text_blob) or re.search(r"schedule (an )?appointment", blob, re.IGNORECASE):
            signals["booking_hint"] = True
            signals["booking_source"] = "regex"
        else:
            signals["booking_hint"] = False
        return signals
