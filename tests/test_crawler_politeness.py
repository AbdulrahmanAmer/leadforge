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

    def do_GET(self):  # noqa: N802 — http.server API
        type(self).requests.append(self.path)
        if self.path == "/robots.txt":
            body = self.robots
        elif self.path.endswith(".xml"):
            self.send_response(404)
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
