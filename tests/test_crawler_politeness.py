"""U8.1: politeness invariants proved against a real local HTTP server (127.0.0.1 only)."""
import http.server
import threading
import time

import pytest

from leadforge.config import load_config
from leadforge.enrich.crawler import SiteCrawler


class _Handler(http.server.BaseHTTPRequestHandler):
    requests: list[str] = []
    home_html = b"<html><body>hello</body></html>"
    robots = b""
    page_status = 200  # non-robots paths: set 403 to fake a WAF block

    robots_status = 200

    def do_GET(self):  # noqa: N802 — http.server API
        type(self).requests.append(self.path)
        if self.path == "/robots.txt":
            if type(self).robots_status != 200:
                self.send_response(type(self).robots_status)
                self.end_headers()
                return
            body = self.robots
        elif self.path.endswith(".xml"):
            self.send_response(404)
            self.end_headers()
            return
        elif type(self).page_status != 200:
            self.send_response(type(self).page_status)
            self.end_headers()
            return
        else:
            body = self.home_html
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def server():
    class H(_Handler):
        requests = []
        robots = b""
        home_html = _Handler.home_html
        page_status = 200
        robots_status = 200

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", H
    srv.shutdown()


def _crawler(tmp_path, monkeypatch, delay=0.0):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.politeness.delay_s = delay
    return cfg, SiteCrawler(cfg)


def test_per_host_delay_is_honored(tmp_path, monkeypatch, server):
    base, handler = server
    cfg, crawler = _crawler(tmp_path, monkeypatch, delay=1.0)
    try:
        t0 = time.monotonic()
        assert crawler._get(f"{base}/") is not None
        assert crawler._get(f"{base}/again") is not None
        elapsed = time.monotonic() - t0
    finally:
        crawler.close()
    # robots fetch + 2 pages = at least 2 waits on one host; jitter is ±30%
    assert elapsed >= 0.7, f"two same-host fetches took only {elapsed:.2f}s with delay_s=1.0"


def test_robots_disallow_is_respected(tmp_path, monkeypatch, server):
    base, handler = server
    handler.robots = b"User-agent: *\nDisallow: /private\n"
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        assert crawler._get(f"{base}/private") is None
    finally:
        crawler.close()
    assert "/private" not in handler.requests  # never even requested


def test_crawl_of_robots_disallowed_home_never_flags_browser(tmp_path, monkeypatch, server):
    """The red line, proven at crawl() level against a real server (the runner-level test
    monkeypatches crawl() away, which is how a needs_browser mutation once went green)."""
    base, handler = server
    handler.robots = b"User-agent: *\nDisallow: /\n"
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        res = crawler.crawl(base)
    finally:
        crawler.close()
    assert res.ok is False and res.error == "robots-disallowed"
    assert res.needs_browser is False  # a site that said no must never be browser-rendered
    assert all(p == "/robots.txt" for p in handler.requests)  # homepage never even requested


def test_crawl_of_blocked_home_flags_browser(tmp_path, monkeypatch, server):
    base, handler = server
    handler.page_status = 403  # WAF-style block for the plain HTTP client; robots readable + permissive
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        res = crawler.crawl(base)
    finally:
        crawler.close()
    assert res.ok is False
    assert res.needs_browser is True


def test_crawl_of_dead_home_does_not_flag_browser(tmp_path, monkeypatch, server):
    """404/timeouts/PDFs gain nothing from Chromium — only block-shaped statuses escalate."""
    base, handler = server
    handler.page_status = 404
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        res = crawler.crawl(base)
    finally:
        crawler.close()
    assert res.ok is False
    assert res.needs_browser is False
    assert "404" in res.error


def test_unreachable_robots_is_treated_as_disallow(tmp_path, monkeypatch, server):
    """RFC 9309 §2.3.1.4: a 5xx robots.txt means assume complete disallow — and a site whose
    robots policy was never readable must not be browser-rendered either."""
    base, handler = server
    handler.robots_status = 503
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        res = crawler.crawl(base)
    finally:
        crawler.close()
    assert res.ok is False and res.error == "robots-disallowed"
    assert res.needs_browser is False
    assert all(p == "/robots.txt" for p in handler.requests)


def test_final_host_http_status_and_offsite_redirect(tmp_path, monkeypatch, server):
    """v0.3 unit C1: crawl()'s new signals against a real server (not a mock) — final_host and
    http_status are always set from the actual response; offsite_redirect compares final_host to the
    caller-supplied business_domain, www-insensitive, and stays False with no business_domain given."""
    base, handler = server
    host = base.split("//", 1)[1]  # "127.0.0.1:PORT" — no www to strip either side
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        same = crawler.crawl(base, business_domain=host)
        diff = crawler.crawl(base, business_domain="totally-different.example")
        no_domain = crawler.crawl(base)
    finally:
        crawler.close()
    assert same.signals["http_status"] == 200
    assert same.signals["final_host"] == host.casefold()
    assert same.signals["offsite_redirect"] is False
    assert diff.signals["offsite_redirect"] is True
    assert no_domain.signals["offsite_redirect"] is False  # nothing to compare against -> never flagged


def test_pages_per_site_cap(tmp_path, monkeypatch, server):
    base, handler = server
    links = "".join(f'<a href="/about-{i}">about {i}</a>' for i in range(20))
    handler.home_html = f"<html><body>{links}</body></html>".encode()
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        res = crawler.crawl(base)
    finally:
        crawler.close()
    assert res.ok
    page_hits = [p for p in handler.requests if p.startswith("/about") or p == "/"]
    assert len(res.pages) <= cfg.crawl.pages_per_site
    assert len(page_hits) <= cfg.crawl.pages_per_site
