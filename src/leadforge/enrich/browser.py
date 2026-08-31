"""Browser escalation for JS-rendered sites — ICM unit U4.5 (STUB with binding spec). Optional [browser] extra.

=== SPEC (implement exactly; acceptance criteria docs/05 U4.5) ===
1. is_available() -> bool: True iff `import crawl4ai` succeeds.
2. fetch_rendered(url, cfg) -> str (raw HTML) | "":
   - lazy `from crawl4ai import AsyncWebCrawler` (or the current sync API for the pinned version);
     raise EnvError with hint "pip install -e .[browser] && crawl4ai-setup" if import fails.
   - respect politeness: same per-host delay as the static crawler (pass the shared HostThrottle in),
     honor robots (reuse crawler._allowed), cap 3 rendered pages/site.
   - return HTML so the SAME extractors in extract.py run on it (do not fork extraction logic);
     if crawl4ai returns fit_markdown only, wrap it as minimal HTML.
3. Wiring: in the enrich stage, when CrawlResult.needs_browser and is_available(), re-fetch home (+ up to 2
   candidate pages) via fetch_rendered and merge extracted contacts/people. When not available, leave
   needs_browser=True so it surfaces in the digest warning + Summary sheet.
4. tests/test_browser.py: skip (pytest.importorskip) when crawl4ai absent; otherwise assert a known SPA fixture
   yields >= 1 email/person the static path missed.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from leadforge.util import LOG, EnvError

MAX_RENDERED_PAGES_PER_SITE = 3


def is_available() -> bool:
    try:
        import crawl4ai  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def fetch_rendered(url: str, cfg, throttle) -> str:
    """Render one page with a headless browser and return its HTML ("" on failure).

    Caller checks robots (same SiteCrawler._allowed); we wait on the shared throttle so
    the rendered fetch obeys the same per-host pacing as the static path.
    """
    if not is_available():
        raise EnvError("browser extra not installed — run: pip install -e .[browser] && crawl4ai-setup")
    throttle.wait(urlsplit(url).netloc)
    try:
        import asyncio

        from crawl4ai import AsyncWebCrawler

        async def _run() -> str:
            async with AsyncWebCrawler(verbose=False) as crawler:
                res = await crawler.arun(url=url, page_timeout=int(cfg.crawl.timeout_s * 1000))
                html = getattr(res, "html", "") or ""
                if html:
                    return html
                md = getattr(res, "markdown", "") or ""
                return _md_to_html(str(md))

        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001 — a render failure never kills the run
        LOG.warning("render failed %s: %s", url, type(e).__name__)
        return ""


def _md_to_html(md: str) -> str:
    """crawl4ai may return markdown only; wrap it so the SAME extractors still work."""
    return f"<html><body><pre>{md}</pre></body></html>" if md else ""
