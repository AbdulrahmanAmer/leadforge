"""Layer 4 (2026-09-24): link-in-bio / aggregator hosts (linktr.ee, w3w.co, ...) must never be crawled
as the business site. Offline: pure-function tests for host classification + link selection, plus a
crawl()-level test against a real local HTTP server whose host is monkeypatched into AGGREGATOR_HOSTS
(no real network / DNS involved)."""
import http.server
import threading

import pytest

from leadforge.config import load_config
from leadforge.enrich import crawler as crawler_mod
from leadforge.enrich.crawler import SiteCrawler, is_aggregator_host


def test_is_aggregator_host_matches_known_hosts_and_subdomains():
    assert is_aggregator_host("linktr.ee")
    assert is_aggregator_host("www.linktr.ee")
    assert is_aggregator_host("sub.w3w.co")
    assert is_aggregator_host("beacons.ai")
    assert is_aggregator_host("linkin.bio")
    assert is_aggregator_host("lnk.bio")


def test_is_aggregator_host_rejects_unrelated_hosts():
    assert not is_aggregator_host("example.com")
    assert not is_aggregator_host("notlinktr.ee.evil.com")  # must not match by naive substring
    assert not is_aggregator_host("")


def test_first_real_link_skips_social_and_aggregator_picks_first_real():
    html = """
    <html><body>
      <a href="https://instagram.com/acme">insta</a>
      <a href="https://www.facebook.com/acme">fb</a>
      <a href="https://linktr.ee/other">another aggregator</a>
      <a href="https://acmegarage.co.uk">Visit our site</a>
      <a href="https://second-real-site.example">second</a>
    </body></html>
    """
    target = SiteCrawler._first_real_link("https://linktr.ee/acme", html)
    assert target == "https://acmegarage.co.uk"


def test_first_real_link_returns_none_when_only_social_links_present():
    html = """
    <html><body>
      <a href="https://instagram.com/acme">insta</a>
      <a href="https://www.tiktok.com/@acme">tiktok</a>
      <a href="mailto:hi@acme.com">mail</a>
      <a href="#top">top</a>
    </body></html>
    """
    assert SiteCrawler._first_real_link("https://linktr.ee/acme", html) is None


# --------------------------------------------------------------------------- crawl()-level, offline

class _RealSiteHandler(http.server.BaseHTTPRequestHandler):
    body = b"<html><body>real site content, about us, contact</body></html>"

    def do_GET(self):  # noqa: N802
        if self.path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def real_site_server():
    class H(_RealSiteHandler):
        pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _crawler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    return cfg, SiteCrawler(cfg)


def test_crawl_of_aggregator_follows_first_real_link(tmp_path, monkeypatch, real_site_server):
    """A linktr.ee-shaped page whose only real (non-social) outbound link is the actual local server:
    crawl() must return pages from the REAL site, never a bare/empty crawl of the aggregator itself."""
    real_host = real_site_server.split("//", 1)[1]
    agg_html = (
        f'<html><body><a href="https://instagram.com/acme">insta</a>'
        f'<a href="{real_site_server}/">Visit our site</a></body></html>'
    )
    monkeypatch.setattr(crawler_mod, "AGGREGATOR_HOSTS", {"127.0.0.1"})
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        result = crawler._crawl_aggregator("http://127.0.0.1/fake-agg", None, home_html=agg_html)
    finally:
        crawler.close()
    assert result.ok is True
    assert len(result.pages) >= 1
    assert result.pages[0].url.split("//", 1)[1].startswith(real_host)
    assert "real site content" in result.pages[0].html


def test_crawl_of_aggregator_with_no_real_link_records_named_error(tmp_path, monkeypatch):
    monkeypatch.setattr(crawler_mod, "AGGREGATOR_HOSTS", {"127.0.0.1"})
    agg_html = '<html><body><a href="https://instagram.com/acme">insta</a></body></html>'
    cfg, crawler = _crawler(tmp_path, monkeypatch)
    try:
        result = crawler._crawl_aggregator("http://127.0.0.1/fake-agg", None, home_html=agg_html)
    finally:
        crawler.close()
    assert result.ok is False
    assert result.error == "aggregator"
    assert len(result.pages) == 0


def test_crawl_routes_aggregator_host_before_any_fetch(tmp_path, monkeypatch):
    """crawl(website=...) itself must recognize an aggregator host up front and never treat the
    aggregator page as the business site — proven via a real linktr.ee-classified host with no
    outbound real link, so this only passes if the routing (not just _crawl_aggregator) fired."""
    monkeypatch.setattr(crawler_mod, "AGGREGATOR_HOSTS", {"agg.invalid"})
    cfg, crawler = _crawler(tmp_path, monkeypatch)

    def fake_first_real_link(base_url, home_html):
        return None

    def fake_get(url, timeout=None):
        crawler.last_failure = None
        return type("R", (), {"text": "<html></html>", "url": url})()

    monkeypatch.setattr(SiteCrawler, "_first_real_link", staticmethod(fake_first_real_link))
    monkeypatch.setattr(crawler, "_get", fake_get)
    monkeypatch.setattr(crawler, "_allowed", lambda url: True)
    try:
        result = crawler.crawl("http://agg.invalid/some-user")
    finally:
        crawler.close()
    assert result.ok is False
    assert result.error == "aggregator"
