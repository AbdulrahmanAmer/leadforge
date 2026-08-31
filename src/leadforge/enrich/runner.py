"""Enrichment orchestrator (U4.1-4.3 wiring) — drives crawl -> extract -> validate over the site queue.

Politeness invariants live in SiteCrawler; this module handles concurrency across hosts, evidence writing,
and the needs_browser accounting. Called by `leadforge enrich` and the `run` orchestrator.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

from leadforge import db
from leadforge.config import Config
from leadforge.enrich import browser
from leadforge.enrich.crawler import SiteCrawler
from leadforge.enrich.extract import extract_emails, extract_people, extract_phones, extract_socials
from leadforge.enrich.validate import validate_email
from leadforge.models import Contact, Evidence, Person
from leadforge.normalize import COUNTRY_TO_REGION
from leadforge.util import LOG, HostThrottle, now_iso


def _region_for_business(b, default_region: str) -> str:
    """Phone region: the listing's own country wins; otherwise the campaign country passed in."""
    if b["address_country"]:
        return COUNTRY_TO_REGION.get(str(b["address_country"]).strip().casefold(), default_region)
    return default_region


def _process_one(cfg: Config, throttle: HostThrottle, b) -> dict:
    """Crawl + extract for a single business. Returns a plain dict (thread-safe; DB writes happen on main thread)."""
    crawler = SiteCrawler(cfg, throttle)
    try:
        res = crawler.crawl(b["website"])
        out = {"business_id": b["id"], "emails": {}, "phones": [], "socials": {}, "people": [],
               "signals": res.signals, "needs_browser": res.needs_browser, "ok": res.ok, "pages": len(res.pages)}
        if not res.ok:
            return out
        region = _region_for_business(b, cfg.default_region)
        for page in res.pages:
            for email, label in extract_emails(page.html, page.text).items():
                out["emails"].setdefault(email, {"label": label, "url": page.url})
            for phone in extract_phones(page.html, page.text, region):
                if phone not in out["phones"]:
                    out["phones"].append(phone)
            for net, url in extract_socials(page.html).items():
                out["socials"].setdefault(net, url)
            for cand in extract_people(page.text, page.url):
                out["people"].append(cand)
        # U4.5: browser escalation — only when static found nothing and the extra is installed.
        if res.needs_browser and browser.is_available() and not out["emails"] and not out["people"]:
            urls = [p.url for p in res.pages[:browser.MAX_RENDERED_PAGES_PER_SITE]] or [b["website"]]
            rendered_any = False
            for url in urls:
                if not crawler._allowed(url):
                    continue
                rendered = browser.fetch_rendered(url, cfg, throttle)
                if not rendered:
                    continue
                rendered_any = True
                text = SiteCrawler.extract_text(rendered)
                for email, label in extract_emails(rendered, text).items():
                    out["emails"].setdefault(email, {"label": label, "url": url})
                for cand in extract_people(text, url):
                    out["people"].append(cand)
            if rendered_any:
                out["needs_browser"] = False
                out["signals"]["rendered"] = True
        return out
    finally:
        crawler.close()


def run_enrich(conn: sqlite3.Connection, cfg: Config, limit: int, stage: str = "all") -> dict:
    """stage: 'site' (crawl+extract), 'validate' (re-validate emails only), 'all'."""
    counts = {"sites_crawled": 0, "contacts": 0, "dm_candidates": 0, "needs_browser": 0, "emails_valid": 0}
    if stage in ("all", "site"):
        _crawl_stage(conn, cfg, limit, counts)
    if stage in ("all", "validate"):
        _validate_stage(conn, cfg, counts)
    return counts


def _crawl_stage(conn: sqlite3.Connection, cfg: Config, limit: int, counts: dict) -> None:
    queue = db.businesses_for_enrich(conn, limit)
    if not queue:
        return
    throttle = HostThrottle(cfg.politeness.delay_s)
    browser_ok = browser.is_available()
    from leadforge.providers.registry import get_registries
    registries = get_registries(cfg)  # once per run so a 429 disable sticks for the whole batch
    with ThreadPoolExecutor(max_workers=cfg.politeness.workers) as pool:
        futures = {pool.submit(_process_one, cfg, throttle, b): b for b in queue}
        for fut in as_completed(futures):
            b = futures[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 — one bad site never kills the batch
                LOG.warning("enrich failed for %s: %s", b["id"], e)
                db.update_enrich(conn, b["id"], {"crawled_at": now_iso(), "error": str(e)[:200]})
                continue
            _persist(conn, cfg, b, res, counts, registries)
            if res["needs_browser"] and not browser_ok:
                counts["needs_browser"] += 1
    conn.commit()


def _persist(conn: sqlite3.Connection, cfg: Config, b, res: dict, counts: dict,
             registries: list | None = None) -> None:
    counts["sites_crawled"] += 1
    bid = b["id"]
    for email, meta in res["emails"].items():
        tier, vmeta = validate_email(email, meta["label"], cfg)
        if tier == "valid":
            counts["emails_valid"] += 1
        db.add_contact(conn, Contact(business_id=bid, kind="email", value=email, label=meta["label"],
                                     tier=tier, verified_at=now_iso(), meta=vmeta))
        db.add_evidence(conn, Evidence(business_id=bid, ref_table="contacts", fact="email_found",
                                       url=meta["url"], snippet=email, observed_at=now_iso()))
        counts["contacts"] += 1
    for phone in res["phones"]:
        db.add_contact(conn, Contact(business_id=bid, kind="phone", value=phone, label="site", tier="valid",
                                     verified_at=now_iso()))
        counts["contacts"] += 1
    for net, url in res["socials"].items():
        db.add_contact(conn, Contact(business_id=bid, kind="social", value=url, label=net, tier="valid"))
    for cand in res["people"]:
        db.add_person(conn, Person(business_id=bid, name=cand.name, title=cand.title, source_url=cand.source_url,
                                   snippet=cand.snippet, labeled_by="heuristic", labeled_at=now_iso()))
        db.add_evidence(conn, Evidence(business_id=bid, ref_table="people", fact="dm_candidate",
                                       url=cand.source_url, snippet=f"{cand.name} — {cand.title}", observed_at=now_iso()))
        counts["dm_candidates"] += 1
    _registry_cross_check(conn, b, counts, registries or [])
    enrich ={"crawled_at": now_iso(), "signals": res["signals"], "socials": res["socials"],
              "pages": res["pages"], "needs_browser": res["needs_browser"]}
    db.update_enrich(conn, bid, enrich)


def _registry_cross_check(conn: sqlite3.Connection, b, counts: dict, registries: list) -> None:
    """U4.6: officer lookup from public registries — key-gated (empty list when no keys), country-gated."""
    if not registries:
        return
    country = (b["address_country"] or "").strip().upper()
    for reg in registries:
        if country not in reg.jurisdictions():
            continue
        for person, ev in reg.lookup(b):
            db.add_person(conn, person)
            db.add_evidence(conn, ev)
            counts["dm_candidates"] += 1


def _validate_stage(conn: sqlite3.Connection, cfg: Config, counts: dict) -> None:
    rows = conn.execute("SELECT * FROM contacts WHERE kind='email' AND tier IN ('unknown','')").fetchall()
    for c in rows:
        tier, vmeta = validate_email(c["value"], c["label"], cfg)
        conn.execute("UPDATE contacts SET tier=?, verified_at=?, meta_json=? WHERE id=?",
                     (tier, now_iso(), _json(vmeta), c["id"]))
        if tier == "valid":
            counts["emails_valid"] += 1
    conn.commit()


def _json(d: dict) -> str:
    import json
    return json.dumps(d)
