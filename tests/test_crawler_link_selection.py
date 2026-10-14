"""Layer 4 fix (2026-09-25): reproduces specific contact-page misses found while diagnosing why
crawled sites yield a median of only 2 pages, using a real local HTTP server (127.0.0.1 only, fully
offline) — same pattern as test_crawler_politeness.py.

Each test isolates one concrete miss cause:
  - footer-only contact link (not in <nav>/body, only <footer>)
  - trailing-slash + tracking query string causing the candidate to look like a duplicate
  - anchor text says "Get in touch" but href doesn't contain "contact"
  - a contact link buried behind several non-contact keyword links (careers/legal/imprint) that would
    otherwise fill the pages_per_site cap first under pure document-order selection
"""
import http.server
import threading

import pytest

from leadforge.config import load_config
from leadforge.enrich.crawler import SiteCrawler


class _Handler(http.server.BaseHTTPRequestHandler):
    pages: dict[str, bytes] = {}
    requests: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).requests.append(self.path)
        path = self.path.split("?", 1)[0]
        if path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
            return
        if path.endswith(".xml"):
            self.send_response(404)
            self.end_headers()
            return
        body = type(self).pages.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    class H(_Handler):
        pages = {}
        requests = []

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", H
    srv.shutdown()


def _crawler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.politeness.delay_s = 0.0
    return cfg, SiteCrawler(cfg)


def _urls(pages) -> set[str]:
    from urllib.parse import urlsplit
    return {urlsplit(p.url).path for p in pages}


def test_footer_only_contact_link_is_selected(tmp_path, monkeypatch, server):
    """Miss cause: contact link exists only in <footer>, not in <nav>/body — must still be a candidate
    since tree.css('a[href]') walks the whole parsed document."""
    base, handler = server
    handler.pages = {
        "/": b"""<html><body>
            <nav><a href="/services">Services</a><a href="/pricing">Pricing</a></nav>
            <p>Welcome to our shop.</p>
            <footer><a href="/contact">Contact</a></footer>
        </body></html>""",
        "/contact": b"<html><body>Contact us at hello@example.com</body></html>",
    }
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        res = crawler.crawl(base)
    finally:
        crawler.close()
    assert res.ok is True
    assert "/contact" in _urls(res.pages)


def test_trailing_slash_and_tracking_query_not_treated_as_duplicate_drop(tmp_path, monkeypatch, server):
    """Miss cause: same contact page linked twice — once bare, once with a trailing slash + utm params —
    must dedup to ONE candidate (not zero, and not silently dropped as unrelated pages competing for the
    cap), and the tracking-param variant must not eat a second budget slot."""
    base, handler = server
    handler.pages = {
        "/": b"""<html><body>
            <a href="/contact-us/?utm_source=homepage&utm_campaign=nav">Contact Us</a>
            <a href="/contact-us">Contact Us</a>
        </body></html>""",
        "/contact-us": b"<html><body>Contact us at hello@example.com</body></html>",
    }
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        candidates = crawler._candidate_links(base + "/", handler.pages["/"].decode())
    finally:
        crawler.close()
    # exactly one candidate url survives dedup, not two competing for the pages_per_site budget
    assert len(candidates) == 1


def test_get_in_touch_anchor_text_without_contact_in_href(tmp_path, monkeypatch, server):
    """Miss cause: anchor text says 'Get in touch' but the href itself ('/reach-out') contains no
    contact-shaped keyword at all — must still match via anchor-text keyword detection."""
    base, handler = server
    handler.pages = {
        "/": b"""<html><body>
            <a href="/reach-out">Get in touch</a>
            <a href="/services">Services</a>
        </body></html>""",
        "/reach-out": b"<html><body>Email us: hello@example.com</body></html>",
    }
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        res = crawler.crawl(base)
    finally:
        crawler.close()
    assert "/reach-out" in _urls(res.pages)


def test_contact_link_survives_page_budget_against_many_low_priority_keyword_links(tmp_path, monkeypatch, server):
    """Miss cause: pages_per_site=6 (5 candidate slots) but nav+footer contain 6 non-contact
    keyword-matching links (careers/legal/imprint/jobs) ahead of a single Contact link in document
    order. Pure document-order truncation would drop Contact; priority ordering must keep it."""
    base, handler = server
    home = b"""<html><body>
        <nav>
            <a href="/careers">Careers</a>
            <a href="/jobs">Jobs</a>
            <a href="/legal">Legal</a>
            <a href="/imprint">Imprint</a>
            <a href="/leadership">Leadership</a>
        </nav>
        <footer>
            <a href="/press-jobs">More Jobs</a>
            <a href="/contact">Contact</a>
        </footer>
    </body></html>"""
    handler.pages = {
        "/": home,
        "/careers": b"<html><body>careers</body></html>",
        "/jobs": b"<html><body>jobs</body></html>",
        "/legal": b"<html><body>legal</body></html>",
        "/imprint": b"<html><body>imprint</body></html>",
        "/leadership": b"<html><body>leadership team</body></html>",
        "/press-jobs": b"<html><body>press jobs</body></html>",
        "/contact": b"<html><body>Contact us: hello@example.com</body></html>",
    }
    cfg = None
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.politeness.delay_s = 0.0
    cfg.crawl.pages_per_site = 6  # 5 candidate slots — 6 keyword links compete, contact must survive
    crawler = SiteCrawler(cfg)
    try:
        res = crawler.crawl(base)
    finally:
        crawler.close()
    assert "/contact" in _urls(res.pages), (
        f"contact page was cut off by the page budget; fetched paths: {_urls(res.pages)}"
    )


def test_public_speaker_and_press_pages_are_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.politeness.delay_s = 0.0
    crawler = SiteCrawler(cfg)
    try:
        links = crawler._candidate_links(
            "https://example.test/",
            """<html><body>
              <a href="/services">Services</a>
              <a href="/speakers">Our Speakers</a>
              <a href="/press">Press</a>
              <a href="/events">Events</a>
            </body></html>""",
        )
    finally:
        crawler.close()
    assert {link.rsplit("/", 1)[-1] for link in links} >= {"speakers", "press", "events"}
