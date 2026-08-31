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
from leadforge.enrich.extract import (
    extract_emails,
    extract_people,
    extract_people_ner,
    extract_phones,
    extract_socials,
    ner_available,
)
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
        # U4.7: GLiNER zero-shot people extraction when the [ner] extra is installed, else heuristic.
        people_fn = extract_people_ner if ner_available() else extract_people
        for page in res.pages:
            for email, label in extract_emails(page.html, page.text).items():
                out["emails"].setdefault(email, {"label": label, "url": page.url})
            for phone in extract_phones(page.html, page.text, region):
                if phone not in out["phones"]:
                    out["phones"].append(phone)
            for net, url in extract_socials(page.html).items():
                out["socials"].setdefault(net, url)
            for cand in people_fn(page.text, page.url):
                out["people"].append(cand)
        # WE SCORE account-intel profile (v0.1.1): tech stack, departments, headcount, triggers.
        from leadforge.enrich.profile import build_profile
        try:
            out["profile"] = build_profile(res.pages, b["domain"], b["category"])
        except Exception as e:  # noqa: BLE001 — profiling is additive, never blocks enrichment
            LOG.warning("profile build failed for %s: %s", b["id"], type(e).__name__)
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
                for cand in people_fn(text, url):
                    out["people"].append(cand)
            if rendered_any:
                out["needs_browser"] = False
                out["signals"]["rendered"] = True
        return out
    finally:
        crawler.close()


def run_enrich(conn: sqlite3.Connection, cfg: Config, limit: int, stage: str = "all") -> dict:
    """stage: 'site' (crawl+extract), 'registry' (officer lookup incl. site-less), 'validate', 'all'."""
    counts = {"sites_crawled": 0, "contacts": 0, "dm_candidates": 0, "needs_browser": 0, "emails_valid": 0}
    if stage in ("all", "site"):
        _crawl_stage(conn, cfg, limit, counts)
    if stage in ("all", "registry"):
        _registry_stage(conn, cfg, counts)
    if stage in ("all", "validate"):
        _validate_stage(conn, cfg, counts)
    return counts


def _registry_stage(conn: sqlite3.Connection, cfg: Config, counts: dict) -> None:
    """v0.1.2: registry lookup for EVERY business in a covered jurisdiction — including the ones
    with no website (they never enter the crawl stage, which is where lookups used to happen).
    Also stores the matched company profile (number, incorporation, status, SIC) for the sheet."""
    from leadforge.providers.registry import CompaniesHouseRegistry, get_registries
    registries = get_registries(cfg)
    if not registries:
        return
    rows = conn.execute(
        """SELECT * FROM businesses b
           WHERE json_extract(b.enrich_json,'$.registry_checked') IS NULL
             AND NOT EXISTS (SELECT 1 FROM people p WHERE p.business_id=b.id AND p.labeled_by='registry')"""
    ).fetchall()
    for b in rows:
        country = (b["address_country"] or "").strip().upper()
        for reg in registries:
            if country not in reg.jurisdictions():
                continue
            profile = None
            if isinstance(reg, CompaniesHouseRegistry):
                people, profile = reg.lookup_with_profile(b)
            else:
                people = reg.lookup(b)
            found = []
            for person, ev in people:
                found.append(person)
                db.add_person(conn, person)
                db.add_evidence(conn, ev)
                counts["dm_candidates"] = counts.get("dm_candidates", 0) + 1
            _auto_pick_registry_dm(conn, b, found, counts)
            enrich_update: dict = {"registry_checked": True}
            if profile:
                enrich_update["registry_profile"] = profile
            db.update_enrich(conn, b["id"], enrich_update)
            counts["registry_looked_up"] = counts.get("registry_looked_up", 0) + 1
            if getattr(reg, "disabled", False):
                LOG.warning("registry %s disabled mid-stage (rate limit); stopping stage", reg.name)
                conn.commit()
                return
    conn.commit()


def _crawl_stage(conn: sqlite3.Connection, cfg: Config, limit: int, counts: dict) -> None:
    queue = db.businesses_for_enrich(conn, limit)
    if not queue:
        return
    throttle = HostThrottle(cfg.politeness.delay_s)
    browser_ok = browser.is_available()
    from leadforge.providers.registry import get_registries
    registries = get_registries(cfg)  # once per run so a 429 disable sticks for the whole batch
    from leadforge.providers import social
    social_ok, social_msg = social.is_available(cfg)
    if cfg.social.enabled and not social_ok:
        LOG.info("social presence skipped: %s", social_msg)
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
            _persist(conn, cfg, b, res, counts, registries, social_ok)
            if res["needs_browser"] and not browser_ok:
                counts["needs_browser"] += 1
    conn.commit()


def _persist(conn: sqlite3.Connection, cfg: Config, b, res: dict, counts: dict,
             registries: list | None = None, social_ok: bool = False) -> None:
    counts["sites_crawled"] += 1
    bid = b["id"]
    from leadforge.enrich.extract import email_matches_business
    for email, meta in res["emails"].items():
        if not email_matches_business(email, b["domain"]):
            continue  # testimonial/client/widget email from someone else's domain
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
    social_presence: dict = {}
    if social_ok:
        from leadforge.providers import social
        social_presence = social.presence(res["socials"], cfg)
        for sig in social.to_signals(social_presence, cfg):
            res["signals"][sig] = True
        for net, p in social_presence.items():
            db.add_evidence(conn, Evidence(business_id=bid, ref_table="businesses", fact="social_presence",
                                           url=p["url"], snippet=f"{net}: last post {p['last_post_at'] or 'unknown'}",
                                           observed_at=now_iso()))
    enrich = {"crawled_at": now_iso(), "signals": res["signals"], "socials": res["socials"],
              "pages": res["pages"], "needs_browser": res["needs_browser"],
              "registry_checked": bool(registries)}
    if res.get("profile"):
        enrich["profile"] = res["profile"]
    if social_presence:
        enrich["social_presence"] = social_presence
    db.update_enrich(conn, bid, enrich)


def _registry_cross_check(conn: sqlite3.Connection, b, counts: dict, registries: list) -> None:
    """U4.6: officer lookup from public registries — key-gated (empty list when no keys), country-gated."""
    if not registries:
        return
    country = (b["address_country"] or "").strip().upper()
    registry_people: list[Person] = []
    for reg in registries:
        if country not in reg.jurisdictions():
            continue
        for person, ev in reg.lookup(b):
            registry_people.append(person)
            db.add_person(conn, person)
            db.add_evidence(conn, ev)
            counts["dm_candidates"] += 1
    _auto_pick_registry_dm(conn, b, registry_people, counts)


_CORPORATE_OFFICER_RE = None


def _is_corporate_officer(name: str) -> bool:
    global _CORPORATE_OFFICER_RE
    if _CORPORATE_OFFICER_RE is None:
        import re
        _CORPORATE_OFFICER_RE = re.compile(r"\b(ltd|llp|limited|plc|inc|gmbh|company|corporation)\b", re.IGNORECASE)
    return bool(_CORPORATE_OFFICER_RE.search(name))


def _auto_pick_registry_dm(conn: sqlite3.Connection, b, registry_people: list[Person], counts: dict) -> None:
    """v0.1.1 (amends ADR-003): exactly ONE active individual director from the official registry is
    stronger evidence than any agent inference — auto-mark them DM so big runs don't queue the
    obvious cases. 0 or 2+ individuals (or corporate officers only) stay with the agent."""
    individuals = [p for p in registry_people if not _is_corporate_officer(p.name)]
    if len(individuals) != 1:
        return
    existing_dm = conn.execute("SELECT 1 FROM people WHERE business_id=? AND is_dm=1 LIMIT 1",
                               (b["id"],)).fetchone()
    if existing_dm:
        return  # a DM is already chosen (agent or earlier run) — never create a second one
    dm = individuals[0]
    conn.execute(
        "UPDATE people SET is_dm=1, dm_confidence=0.9, labeled_at=? "
        "WHERE business_id=? AND name=? AND labeled_by='registry'",
        (now_iso(), b["id"], dm.name))
    counts["dm_auto_picked"] = counts.get("dm_auto_picked", 0) + 1


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
