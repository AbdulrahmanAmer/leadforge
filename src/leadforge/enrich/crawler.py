"""Polite static-first website crawler (U4.1) — docs/04 §3.3, invariants docs/04 §5 (ADR-009).

Politeness is code, not convention: robots.txt honored, >= delay_s (+jitter) between requests to a host,
one request in flight per host, page/site caps, identifying User-Agent, hard timeouts.
"""

from __future__ import annotations

import re
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


class SiteCrawler:
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

    # --- robots ------------------------------------------------------------------
    def _robots_for(self, base: str) -> urllib.robotparser.RobotFileParser:
        host = urlsplit(base).netloc
        rp = self._robots.get(host)
        if rp is not None:
            return rp
        rp = urllib.robotparser.RobotFileParser()
        try:
            self.throttle.wait(host)
            resp = self.client.get(urljoin(base, "/robots.txt"))
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])  # unreachable robots -> default allow, stay polite regardless
        self._robots[host] = rp
        return rp

    def _allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return rp.can_fetch(self.cfg.politeness.user_agent, url) and rp.can_fetch("*", url)

    # --- fetch -------------------------------------------------------------------
    def _get(self, url: str) -> httpx.Response | None:
        if not self._allowed(url):
            LOG.info("robots disallow: %s", url)
            return None
        self.throttle.wait(urlsplit(url).netloc)
        try:
            resp = self.client.get(url)
            if resp.status_code >= 400:
                return None
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return None
            return resp
        except httpx.HTTPError as e:
            LOG.debug("fetch failed %s: %s", url, type(e).__name__)
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
            resp = self._get(urljoin(base_url, "/sitemap.xml"))
            if resp is not None:
                for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text)[:200]:
                    if PAGE_KEYWORDS.search(urlsplit(loc).path) and urlsplit(loc).netloc.removeprefix("www.") == base_host:
                        seen.setdefault(loc, None)
                links = list(seen)
        return links[: max(0, self.cfg.crawl.pages_per_site - 1)]

    # --- main --------------------------------------------------------------------
    def crawl(self, website: str) -> CrawlResult:
        result = CrawlResult(ok=False)
        if not self._allowed(website):
            result.error = "robots-disallowed"  # the site said no — the browser must not go either
            return result
        resp = self._get(website)
        if resp is None:
            # blocked/403/unreachable to a plain HTTP client — a real browser may still be served,
            # exactly like a person opening the site; flag it for the browser escalation pass
            result.error = "unreachable to http client"
            result.needs_browser = True
            return result
        home_html = resp.text[: self.cfg.crawl.max_text_bytes]
        home_text = self.extract_text(home_html)
        result.pages.append(Page(str(resp.url), home_html, home_text))
        result.signals["https"] = str(resp.url).startswith("https://")
        result.signals["status"] = resp.status_code

        if self.looks_js_shell(home_html, home_text):
            result.needs_browser = True

        for link in self._candidate_links(str(resp.url), home_html):
            sub = self._get(link)
            if sub is None:
                continue
            html = sub.text[: self.cfg.crawl.max_text_bytes]
            result.pages.append(Page(str(sub.url), html, self.extract_text(html)))

        blob = "\n".join(p.html for p in result.pages)
        years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\D{0,20}(20\d{2})", blob, re.IGNORECASE)]
        if years:
            latest = max(years)
            result.signals["copyright_year"] = latest
            result.signals["stale_site"] = latest < _CURRENT_YEAR - self.cfg.crawl.stale_after_years
        result.signals["careers"] = any(re.search(r"/(careers?|jobs?)\b", p.url) for p in result.pages)
        result.signals["booking_hint"] = bool(
            re.search(r"(book (now|online)|schedule (an )?appointment|calendly|acuity|squarespace-scheduling)", blob, re.IGNORECASE)
        )
        result.ok = True
        return result
