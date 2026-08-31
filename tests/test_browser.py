"""U4.5 browser escalation tests.

The crawl4ai render itself is only exercised when the [browser] extra is installed; the wiring
(escalate only when static found nothing, robots respected, needs_browser cleared) is mocked.
"""
import pytest

from leadforge.enrich import browser
from leadforge.util import EnvError, HostThrottle


def test_fetch_rendered_without_extra_raises_envhint(monkeypatch):
    monkeypatch.setattr(browser, "is_available", lambda: False)

    class _Cfg:
        pass

    with pytest.raises(EnvError, match=r"\[browser\]"):
        browser.fetch_rendered("http://example.com", _Cfg(), HostThrottle(0))


def test_md_to_html_wraps_markdown():
    assert browser._md_to_html("hello a@b.com") == "<html><body><pre>hello a@b.com</pre></body></html>"
    assert browser._md_to_html("") == ""


def test_escalation_wiring_merges_rendered_contacts(monkeypatch, tmp_path):
    from leadforge.config import load_config
    from leadforge.enrich import runner
    from leadforge.enrich.crawler import CrawlResult, Page

    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    shell = CrawlResult(ok=True, needs_browser=True,
                        pages=[Page("http://spa.example", "<div id='root'></div>", "")])
    monkeypatch.setattr(runner.SiteCrawler, "crawl", lambda self, url: shell)
    monkeypatch.setattr(runner.SiteCrawler, "_allowed", lambda self, url: True)
    monkeypatch.setattr(runner.browser, "is_available", lambda: True)
    monkeypatch.setattr(runner.browser, "fetch_rendered",
                        lambda url, cfg, throttle: "<html><body>Contact: <a href='mailto:hi@spa.example'>hi@spa.example</a></body></html>")
    b = {"id": 1, "website": "http://spa.example", "address_country": None}
    out = runner._process_one(cfg, HostThrottle(0), b)
    assert "hi@spa.example" in out["emails"]
    assert out["needs_browser"] is False
    assert out["signals"].get("rendered") is True


def test_escalation_respects_robots(monkeypatch, tmp_path):
    from leadforge.config import load_config
    from leadforge.enrich import runner
    from leadforge.enrich.crawler import CrawlResult, Page

    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    shell = CrawlResult(ok=True, needs_browser=True,
                        pages=[Page("http://spa.example", "<div id='root'></div>", "")])
    monkeypatch.setattr(runner.SiteCrawler, "crawl", lambda self, url: shell)
    monkeypatch.setattr(runner.SiteCrawler, "_allowed", lambda self, url: False)
    monkeypatch.setattr(runner.browser, "is_available", lambda: True)
    calls = []
    monkeypatch.setattr(runner.browser, "fetch_rendered", lambda *a: calls.append(a) or "")
    b = {"id": 1, "website": "http://spa.example", "address_country": None}
    out = runner._process_one(cfg, HostThrottle(0), b)
    assert calls == []  # robots-disallowed page never rendered
    assert out["needs_browser"] is True


@pytest.mark.skipif(not browser.is_available(), reason="crawl4ai extra not installed")
def test_rendered_fixture_yields_contact():
    pytest.importorskip("crawl4ai")
    # Live-render acceptance is exercised at U8.2 on a machine with the extra + a JS-only site.


def test_http_blocked_site_gets_browser_fallback(monkeypatch, tmp_path):
    """A site that 403s the plain client is retried with a real browser (like a person opening it)."""
    from leadforge.config import load_config
    from leadforge.enrich import runner
    from leadforge.enrich.crawler import CrawlResult

    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    blocked = CrawlResult(ok=False, needs_browser=True, error="unreachable to http client")
    monkeypatch.setattr(runner.SiteCrawler, "crawl", lambda self, url: blocked)
    monkeypatch.setattr(runner.browser, "is_available", lambda: True)
    monkeypatch.setattr(runner.browser, "fetch_rendered",
                        lambda url, cfg, throttle: "<html><body>Call 020 7946 0958 or "
                                                   "<a href='mailto:hi@blocked.example'>hi@blocked.example</a></body></html>")
    b = {"id": 1, "website": "http://blocked.example", "address_country": "GB", "domain": "blocked.example",
         "category": "car garage"}
    out = runner._process_one(cfg, HostThrottle(0), b)
    assert out["ok"] is True and out["signals"]["http_blocked"] is True
    assert "hi@blocked.example" in out["emails"]


def test_robots_disallowed_site_never_gets_browser(monkeypatch, tmp_path):
    from leadforge.config import load_config
    from leadforge.enrich import runner
    from leadforge.enrich.crawler import CrawlResult

    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    denied = CrawlResult(ok=False, needs_browser=False, error="robots-disallowed")
    monkeypatch.setattr(runner.SiteCrawler, "crawl", lambda self, url: denied)
    monkeypatch.setattr(runner.browser, "is_available", lambda: True)
    monkeypatch.setattr(runner.browser, "fetch_rendered",
                        lambda *a: pytest.fail("browser used on a robots-disallowed site"))
    b = {"id": 1, "website": "http://private.example", "address_country": "GB",
         "domain": "private.example", "category": "car garage"}
    out = runner._process_one(cfg, HostThrottle(0), b)
    assert out["ok"] is False
