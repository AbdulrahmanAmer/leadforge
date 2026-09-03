"""Browser escalation for JS-rendered sites — ICM unit U4.5 (implemented). Optional [browser] extra.

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

import threading
from urllib.parse import urlsplit

from leadforge.util import LOG, EnvError

# Fallback defaults for callers with no cfg.enrich (e.g. bare test doubles) — real runs always go
# through cfg.enrich.rendered_pages_per_site / cfg.enrich.browser_concurrency / cfg.enrich.render_timeout_s.
MAX_RENDERED_PAGES_PER_SITE = 2
DEFAULT_BROWSER_CONCURRENCY = 4
DEFAULT_RENDER_TIMEOUT_S = 20.0

# v0.3 speed unit (2026-09-02, build item 2): was a hardcoded threading.Semaphore(2) — renders are the
# heavyweight exception (a real Chromium instance) regardless of politeness.workers, so they get their
# own concurrency knob, cfg.enrich.browser_concurrency (default 4). A Semaphore's size is fixed at
# construction, so the gate is lazily (re)built to match whatever concurrency the caller's cfg asks for;
# in production that's set once per process, so this never thrashes mid-run.
_render_gate_lock = threading.Lock()
_render_gate: threading.Semaphore | None = None
_render_gate_size: int | None = None


def _get_render_gate(concurrency: int) -> threading.Semaphore:
    global _render_gate, _render_gate_size
    concurrency = max(1, int(concurrency))
    with _render_gate_lock:
        if _render_gate is None or _render_gate_size != concurrency:
            _render_gate = threading.Semaphore(concurrency)
            _render_gate_size = concurrency
        return _render_gate


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

        # headed mode = a visible, ordinary browser window (debug aid + renders sites that refuse
        # plain HTTP clients). Deliberately NO stealth/anti-detection flags — icm/SCOPE.md red line.
        kwargs = {"verbose": False}
        if getattr(cfg.crawl, "headed_browser", False):
            try:
                from crawl4ai import BrowserConfig
                kwargs = {"config": BrowserConfig(headless=False, verbose=False)}
            except ImportError:
                kwargs = {"headless": False, "verbose": False}

        # v0.3 speed unit (2026-09-02, build item 2): a render's OWN timeout — cfg.crawl.timeout_s is
        # the plain-HTTP home-page timeout (dropped to 10s, nowhere near enough for a real browser
        # render) and cfg.enrich.render_timeout_s (default 20s) is deliberately separate from it.
        render_timeout_s = getattr(getattr(cfg, "enrich", None), "render_timeout_s", DEFAULT_RENDER_TIMEOUT_S)

        async def _run() -> str:
            async with AsyncWebCrawler(**kwargs) as crawler:
                res = await crawler.arun(url=url, page_timeout=int(render_timeout_s * 1000))
                # a failed/4xx+ render is not contact data: without this gate, a parked-domain or
                # 404 page became ok=True and its registrar/ad phone entered the call sheet
                status = getattr(res, "status_code", None)
                if getattr(res, "success", True) is False or (isinstance(status, int) and status >= 400):
                    LOG.info("render rejected %s: success=%s status=%s", url, getattr(res, "success", None), status)
                    return ""
                html = getattr(res, "html", "") or ""
                if html:
                    return html
                md = getattr(res, "markdown", "") or ""
                return _md_to_html(str(md))

        concurrency = getattr(getattr(cfg, "enrich", None), "browser_concurrency", DEFAULT_BROWSER_CONCURRENCY)
        with _get_render_gate(concurrency):
            return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001 — a render failure never kills the run
        LOG.warning("render failed %s: %s", url, type(e).__name__)
        return ""


def _md_to_html(md: str) -> str:
    """crawl4ai may return markdown only; wrap it so the SAME extractors still work."""
    return f"<html><body><pre>{md}</pre></body></html>" if md else ""
