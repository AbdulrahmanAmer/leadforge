"""Enrichment orchestrator (U4.1-4.3 wiring) — drives crawl -> extract -> validate over the site queue.

Politeness invariants live in SiteCrawler; this module handles concurrency across hosts, evidence writing,
and the needs_browser accounting. Called by `leadforge enrich` and the `run` orchestrator.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from selectolax.parser import HTMLParser as _HTMLParser

from leadforge import db
from leadforge.config import Config
from leadforge.enrich import browser
from leadforge.enrich.crawler import Page, SiteCrawler
from leadforge.enrich.extract import (
    classify_email_affinity,
    email_context,
    extract_emails,
    extract_people,
    extract_people_ner,
    extract_phones,
    extract_socials,
    ner_available,
)
from leadforge.enrich.validate import validate_email, validate_emails_parallel
from leadforge.models import Contact, Evidence, Person
from leadforge.normalize import COUNTRY_TO_REGION
from leadforge.util import LOG, HostThrottle, emit_progress, now_iso


def _mailto_anchor_context(html: str, email: str, window: int = 90) -> str | None:
    """Anchor text + parent element text for a mailto address that never surfaces in the page's
    extracted text — an icon-only 'Email us' anchor, or a nav/footer mailto trafilatura strips as
    boilerplate. Kept local to runner.py (not extract.py) because this unit does not own that file.
    -> collapsed text, or None if no matching mailto anchor is found."""
    try:
        tree = _HTMLParser(html)
    except Exception:  # noqa: BLE001 — malformed HTML must never break persistence
        return None
    target = email.strip().casefold()
    for a in tree.css('a[href^="mailto:"]'):
        href = (a.attributes.get("href") or "")[7:].split("?")[0]
        href = href.strip().strip(".,;:<>()[]\"'").casefold()
        if href != target:
            continue
        anchor_text = (a.text() or "").strip()
        parent = a.parent
        parent_text = (parent.text(separator=" ") if parent is not None else "").strip()
        combined = " ".join(x for x in (anchor_text, parent_text) if x)
        combined = " ".join(combined.split())[: 2 * window + len(email) + 40] if combined else ""
        if combined:
            return combined
    return None


def _email_evidence(html: str, text: str, email: str) -> str:
    """Evidence snippet for a found address: page-text context around it (email_context), falling
    back to the mailto anchor's own text (+ its parent element) when the address itself never
    appears in the extracted text — without this fallback the evidence row for a mailto-only,
    icon-style contact link was just the bare address, which proves nothing beyond 'this string
    looks like an email'."""
    ctx = email_context(text, email)
    if len(ctx) > len(email):
        return ctx
    return _mailto_anchor_context(html, email) or ctx


def _region_for_business(b, default_region: str) -> str:
    """Phone region: the listing's own country wins; otherwise the campaign country passed in."""
    if b["address_country"]:
        return COUNTRY_TO_REGION.get(str(b["address_country"]).strip().casefold(), default_region)
    return default_region


def _process_one(cfg: Config, throttle: HostThrottle, b) -> dict:
    """Crawl + extract for a single business. Returns a plain dict (thread-safe; DB writes happen on main thread)."""
    crawler = SiteCrawler(cfg, throttle)
    try:
        # v0.3 polish: business_domain makes signals final_host/offsite_redirect real in production
        # (crawl() computes offsite_redirect only when given a domain to compare the final URL's
        # host against) — omitting it left both signals permanently absent from every live run.
        res = crawler.crawl(b["website"], business_domain=(b["domain"] if "domain" in b.keys() else None))
        out = {"business_id": b["id"], "emails": {}, "phones": [], "socials": {}, "people": [],
               "signals": res.signals, "needs_browser": res.needs_browser, "ok": res.ok,
               "pages": len(res.pages), "error": res.error}
        if not res.ok:
            # site refused the plain HTTP client: try a real browser (same as a person opening it);
            # robots-disallowed sites never reach here (needs_browser stays False for those)
            if res.needs_browser and browser.is_available():
                rendered = browser.fetch_rendered(b["website"], cfg, throttle)
                if rendered:
                    rendered = rendered[: cfg.crawl.max_text_bytes]  # same bound as the static path
                    text = SiteCrawler.extract_text(rendered)
                    page = Page(b["website"], rendered, text)
                    region = _region_for_business(b, cfg.default_region)
                    people_fn = extract_people_ner if ner_available() else extract_people
                    for email, label in extract_emails(rendered, text).items():
                        out["emails"].setdefault(email, {"label": label, "url": b["website"],
                                                          "context": _email_evidence(rendered, text, email)})
                    for phone in extract_phones(rendered, text, region):
                        if phone not in out["phones"]:
                            out["phones"].append(phone)
                    for net, url in extract_socials(rendered).items():
                        out["socials"].setdefault(net, url)
                    for cand in people_fn(text, b["website"]):
                        out["people"].append(cand)
                    # a rescued site scores on the same evidence as a crawled one: signals + profile
                    # (without these, phone_only_booking mis-fired and WE SCORE columns came out empty)
                    out["signals"].update(SiteCrawler.compute_signals([page], cfg.crawl.stale_after_years))
                    from leadforge.enrich.profile import build_profile
                    try:
                        out["profile"] = build_profile([page], b["domain"], b["category"])
                    except Exception as e:  # noqa: BLE001 — profiling is additive, never blocks
                        LOG.warning("profile build failed for %s: %s", b["id"], type(e).__name__)
                    out["ok"] = True
                    out["needs_browser"] = False
                    out["error"] = ""
                    out["signals"]["rendered"] = True
                    out["signals"]["http_blocked"] = True
                    out["pages"] = 1
            return out
        region = _region_for_business(b, cfg.default_region)
        # U4.7: GLiNER zero-shot people extraction when the [ner] extra is installed, else heuristic.
        people_fn = extract_people_ner if ner_available() else extract_people
        for page in res.pages:
            for email, label in extract_emails(page.html, page.text).items():
                out["emails"].setdefault(email, {"label": label, "url": page.url,
                                                  "context": _email_evidence(page.html, page.text, email)})
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
            rendered_cap = getattr(cfg.enrich, "rendered_pages_per_site", browser.MAX_RENDERED_PAGES_PER_SITE)
            urls = [p.url for p in res.pages[:rendered_cap]] or [b["website"]]
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
                    out["emails"].setdefault(email, {"label": label, "url": url,
                                                      "context": _email_evidence(rendered, text, email)})
                for cand in people_fn(text, url):
                    out["people"].append(cand)
            if rendered_any:
                out["needs_browser"] = False
                out["signals"]["rendered"] = True
        return out
    finally:
        crawler.close()


def run_enrich(conn: sqlite3.Connection, cfg: Config, limit: int, stage: str = "all") -> dict:
    """stage: 'site' (crawl+extract), 'registry' (officer lookup incl. site-less), 'gbp' (Google
    Business Profile facts for site-less businesses — the crawl stage covers sited ones inline),
    'infer', 'validate', 'all'.

    v0.3 speed unit (2026-09-02, build item 4): when stage='all' AND cfg.enrich.overlap_stages (default
    True), registry lookups (for businesses that don't need a crawl, then crawled ones as they finish)
    and email validation run CONCURRENTLY with the crawl instead of strictly after it — see
    _run_overlapped. A SINGLE named stage (`enrich --stage site|registry|gbp|infer|validate`) ALWAYS
    uses the original serial per-stage function below, unaffected by this flag."""
    counts = {"sites_crawled": 0, "contacts": 0, "dm_candidates": 0, "needs_browser": 0, "emails_valid": 0}
    if stage == "all" and cfg.enrich.overlap_stages:
        _run_overlapped(conn, cfg, limit, counts)
        _gbp_stage(conn, cfg, counts)
        _infer_stage(conn, cfg, counts)
        _validate_stage(conn, cfg, counts)  # final sweep: anything still 'unknown' (e.g. DNS timeouts)
        return counts
    if stage in ("all", "site"):
        _crawl_stage(conn, cfg, limit, counts)
    if stage in ("all", "registry"):
        _registry_stage(conn, cfg, counts)
    if stage in ("all", "gbp"):
        _gbp_stage(conn, cfg, counts)
    if stage in ("all", "infer"):
        _infer_stage(conn, cfg, counts)
    if stage in ("all", "validate"):
        _validate_stage(conn, cfg, counts)
    return counts


# ======================================================================== overlapped 'all' (build item 4)
def _drain_queue(wq: queue.Queue) -> int:
    """Run every write-closure currently queued, in order. Each closure captures `conn` by reference
    but is only ever INVOKED here — on whichever thread calls _drain_queue, which _run_overlapped
    guarantees is always the same thread that owns `conn` (the caller's thread; no driver thread ever
    calls this). That is the entire single-writer discipline: background threads produce closures,
    one thread consumes and runs them, so conn.execute()/commit() only ever happens on its own thread
    and SQLite only ever sees one writer at a time."""
    n = 0
    while True:
        try:
            fn = wq.get_nowait()
        except queue.Empty:
            return n
        fn()
        n += 1


def _run_overlapped(conn: sqlite3.Connection, cfg: Config, limit: int, counts: dict) -> None:
    """v0.3 speed unit (2026-09-02, build item 4): registry lookups (for businesses that don't need a
    crawl first, then crawled ones as they finish) and email validation run CONCURRENTLY with the
    crawl, each in its own thread — while every actual `conn` read/write still happens on THIS
    function's own thread (the single writer). No other thread ever touches `conn`: driver threads do
    network I/O only and hand results back as zero-arg closures on a queue.Queue, which this thread
    drains in a loop; when the queue is momentarily empty but a driver is still working, this thread
    does an incremental validate sweep itself (also DNS-pooled internally — see validate.py) instead
    of idling, so DNS resolution for already-persisted contacts overlaps the crawl/registry I/O too.

    Scope note (say so plainly, not overclaimed): registry lookups for businesses CRAWLED in this run
    happen in the crawl driver thread right after each one finishes (genuinely concurrent, off the
    connection's thread) via _lookup_registry_for; the matching DB write
    (_persist_registry_result, inside the deferred _persist call) still happens on this thread when its
    closure is drained, same as the serial path — that part was never the bottleneck (it's a few
    dict/list writes, not I/O)."""
    from leadforge.providers import social
    from leadforge.providers.registry import get_registries

    browser_ok = browser.is_available()
    crawl_queue = _interleave_by_domain(db.businesses_for_enrich(conn, limit, retry_needs_browser=browser_ok))
    crawl_ids = {b["id"] for b in crawl_queue}
    # registry backlog = everyone _registry_stage would normally cover, MINUS businesses in today's
    # crawl queue (those get their registry lookup via the crawl driver, "as they finish" — see above).
    # Site-less (domain IS NULL) rows sort first, per the build note ("no-crawl-needed ones first").
    registry_backlog = [
        r for r in conn.execute(
            """SELECT * FROM businesses b
               WHERE json_extract(b.enrich_json,'$.registry_checked') IS NULL
                 AND NOT EXISTS (SELECT 1 FROM people p WHERE p.business_id=b.id AND p.labeled_by='registry')"""
        ).fetchall()
        if r["id"] not in crawl_ids
    ]
    registry_backlog.sort(key=lambda r: 0 if not r["domain"] else 1)

    registries = get_registries(cfg)  # once per run, shared — a 429 disable (reg.disabled) sticks
    social_ok, social_msg = social.is_available(cfg)
    if cfg.social.enabled and not social_ok:
        LOG.info("social presence skipped: %s", social_msg)

    wq: queue.Queue = queue.Queue()
    throttle = HostThrottle(cfg.politeness.delay_s)
    errors: list[BaseException] = []

    def _registry_driver() -> None:
        try:
            if not registries or not registry_backlog:
                return
            total = len(registry_backlog)
            for i, b in enumerate(registry_backlog):
                emit_progress("registry", i + 1, total, b["name"] or "")
                people, profile, any_covered = _lookup_registry_for(b, registries)
                if any_covered:  # matches _registry_stage: no covering jurisdiction -> no write at all
                    wq.put(lambda b=b, people=people, profile=profile:
                           _persist_registry_result(conn, b, counts, people, profile))
                if any(getattr(reg, "disabled", False) for reg in registries):
                    LOG.warning("registry disabled mid-stage (rate limit); stopping registry backlog")
                    return
        except Exception as e:  # noqa: BLE001 — a driver thread must never hang the run silently
            LOG.exception("registry driver failed")
            errors.append(e)

    def _crawl_driver() -> None:
        try:
            if not crawl_queue:
                return
            with ThreadPoolExecutor(max_workers=cfg.politeness.workers) as pool:
                futures = {pool.submit(_process_one, cfg, throttle, b): b for b in crawl_queue}
                done_n = 0
                for fut in as_completed(futures):
                    done_n += 1
                    b = futures[fut]
                    emit_progress("enrich", done_n, len(futures), b["name"] or "")
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001 — one bad site never kills the batch
                        msg = str(e)[:200]
                        LOG.warning("enrich failed for %s: %s", b["id"], msg)
                        wq.put(lambda b=b, msg=msg: db.update_enrich(
                            conn, b["id"], {"attempted_at": now_iso(), "error": msg}))
                        continue
                    # registry lookup for a business crawled THIS run happens right here — off the
                    # connection's thread, concurrently with the pool's other in-flight crawls.
                    registry_result = _lookup_registry_for(b, registries) if registries else None

                    def _do_persist(b=b, res=res, registry_result=registry_result) -> None:
                        _persist(conn, cfg, b, res, counts, registries, social_ok, registry_result=registry_result)
                        if res["needs_browser"] and not browser_ok:
                            counts["needs_browser"] += 1
                    wq.put(_do_persist)
        except Exception as e:  # noqa: BLE001
            LOG.exception("crawl driver failed")
            errors.append(e)

    t_crawl = threading.Thread(target=_crawl_driver, name="enrich-crawl-driver", daemon=True)
    t_registry = threading.Thread(target=_registry_driver, name="enrich-registry-driver", daemon=True)
    t_crawl.start()
    t_registry.start()

    while t_crawl.is_alive() or t_registry.is_alive() or not wq.empty():
        if _drain_queue(wq) == 0:
            # nothing to write right now but a driver is still working — resolve any contacts that
            # already appeared with an unresolved tier instead of idling (DNS-pooled; see validate.py).
            _validate_stage(conn, cfg, counts)
            time.sleep(0.02)
    t_crawl.join()
    t_registry.join()
    _drain_queue(wq)
    conn.commit()
    if errors:
        raise errors[0]


def _infer_stage(conn: sqlite3.Connection, cfg: Config, counts: dict) -> None:
    """v0.2.0, opt-in (`validation.infer_emails`): propose the DM's likely address when the domain's
    OWN naming convention is demonstrated by a personal email already found there.

    Runs after the registry stage so registry-auto-picked DMs are covered; re-run
    `leadforge enrich --stage infer` after `dm apply` to cover agent-labeled DMs too.
    Public evidence + MX only — never SMTP (icm/SCOPE.md #5).
    """
    if not cfg.validation.infer_emails:
        return
    from leadforge.enrich.infer_email import infer_email
    rows = conn.execute(
        """SELECT b.id, b.domain, p.name dm FROM businesses b
           JOIN people p ON p.business_id = b.id AND p.is_dm = 1
           WHERE b.domain IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.business_id = b.id
                             AND c.kind = 'email' AND c.tier = 'inferred')"""
    ).fetchall()
    for bi, b in enumerate(rows):
        emit_progress("infer", bi + 1, len(rows), b["dm"] or "")
        known = [c["value"] for c in conn.execute(
            "SELECT value FROM contacts WHERE business_id=? AND kind='email' AND tier!='invalid'",
            (b["id"],))]
        guess = infer_email(b["dm"] or "", b["domain"] or "", known, cfg)
        if not guess:
            continue
        db.add_contact(conn, Contact(business_id=b["id"], kind="email", value=guess["email"],
                                     label="inferred", tier="inferred", verified_at=now_iso(),
                                     meta={"pattern": guess["pattern"], "confidence": guess["confidence"],
                                           "basis": guess["basis"]}))
        # its own fact: an inferred address has no source URL, so it must never claim 'email_found'
        db.add_evidence(conn, Evidence(business_id=b["id"], ref_table="contacts", fact="email_inferred",
                                       url="", snippet=f"{guess['email']} — {guess['basis']} "
                                                       f"(confidence {guess['confidence']})",
                                       observed_at=now_iso()))
        counts["emails_inferred"] = counts.get("emails_inferred", 0) + 1
    conn.commit()


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
    emit_progress("registry", 0, len(rows), "starting")
    for bi, b in enumerate(rows):
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
            _auto_pick_registry_dm(conn, b, found, counts, profile)
            enrich_update: dict = {"registry_checked": True}
            if profile:
                enrich_update["registry_profile"] = profile
            db.update_enrich(conn, b["id"], enrich_update)
            counts["registry_looked_up"] = counts.get("registry_looked_up", 0) + 1
            if getattr(reg, "disabled", False):
                LOG.warning("registry %s disabled mid-stage (rate limit); stopping stage", reg.name)
                conn.commit()
                return
        emit_progress("registry", bi + 1, len(rows), b["name"] or "")  # after the lookup, not before
    conn.commit()


def _interleave_by_domain(biz_queue: list) -> list:
    """v0.3 speed unit (2026-09-02, build item 3): reorder the (already category/review_count
    prioritized) queue so ThreadPoolExecutor submission order maximizes DISTINCT hosts among the
    first `workers` futures. businesses_for_enrich requires domain IS NOT NULL, so every row has one.

    Why this matters: HostThrottle already serializes same-host requests to one-in-flight — that
    invariant is untouched. But if a chain's 3 locations (same domain) land consecutively at the front
    of the queue, ThreadPoolExecutor submits all 3 near-simultaneously and 2 of the pool's threads
    immediately block on HostThrottle waiting their turn behind the 3rd, instead of crawling 2 OTHER
    distinct hosts sitting further back. Grouping by domain and round-robining one-per-domain keeps
    each domain group's own internal priority order, and only changes the CROSS-group interleaving —
    so with workers=12 and no repeated domains this is a no-op (every group has exactly 1 member)."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for b in biz_queue:
        domain = b["domain"]
        if domain not in groups:
            groups[domain] = []
            order.append(domain)
        groups[domain].append(b)
    out: list = []
    i = 0
    while len(out) < len(biz_queue):
        for domain in order:
            g = groups[domain]
            if i < len(g):
                out.append(g[i])
        i += 1
    return out


def _crawl_stage(conn: sqlite3.Connection, cfg: Config, limit: int, counts: dict) -> None:
    browser_ok = browser.is_available()
    # with the browser extra present, sites an earlier pass flagged needs_browser get their retry —
    # otherwise the digest's 'pip install .[browser]' advice was a silent no-op on re-run
    biz_queue = _interleave_by_domain(db.businesses_for_enrich(conn, limit, retry_needs_browser=browser_ok))
    if not biz_queue:
        return
    throttle = HostThrottle(cfg.politeness.delay_s)
    from leadforge.providers.registry import get_registries
    registries = get_registries(cfg)  # once per run so a 429 disable sticks for the whole batch
    from leadforge.providers import social
    social_ok, social_msg = social.is_available(cfg)
    if cfg.social.enabled and not social_ok:
        LOG.info("social presence skipped: %s", social_msg)
    with ThreadPoolExecutor(max_workers=cfg.politeness.workers) as pool:
        futures = {pool.submit(_process_one, cfg, throttle, b): b for b in biz_queue}
        done_n = 0
        for fut in as_completed(futures):
            done_n += 1
            emit_progress("enrich", done_n, len(futures), futures[fut]["name"] or "")
            b = futures[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 — one bad site never kills the batch
                LOG.warning("enrich failed for %s: %s", b["id"], e)
                db.update_enrich(conn, b["id"], {"attempted_at": now_iso(), "error": str(e)[:200]})
                continue
            _persist(conn, cfg, b, res, counts, registries, social_ok)
            if res["needs_browser"] and not browser_ok:
                counts["needs_browser"] += 1
    conn.commit()


def _persist(conn: sqlite3.Connection, cfg: Config, b, res: dict, counts: dict,
             registries: list | None = None, social_ok: bool = False, registry_result=None) -> None:
    """registry_result (v0.3 speed unit, optional): a (people_with_evidence, profile, any_covered) tuple already
    looked up off-thread by the overlapped 'all' stage (_run_overlapped) — when given, _persist writes
    it via _persist_registry_result instead of doing the registry NETWORK call itself (which
    _registry_cross_check would otherwise do inline, right here, on whichever thread calls _persist).
    `registries` is still passed through either way — it only gates the registry_checked flag below."""
    if res["ok"]:  # a 0-page failure is not a crawled site — the digest count stays honest
        counts["sites_crawled"] += 1
    bid = b["id"]
    from leadforge.enrich.extract import email_matches_business
    # v0.3 polish (finding 1): this crawl's OWN res['people'] candidates must count too, not just
    # people already in the DB from a prior pass — the people insert loop below runs AFTER this
    # email loop, so without adding them here a freemail box matching a person named for the first
    # time on THIS crawl (e.g. a page naming "John Hoggarth" alongside johnhoggarth@live.co.uk) was
    # misclassified freemail_unlinked purely because of insert order, not because no link existed.
    existing_names = [p["name"] for p in db.people_for(conn, bid)] + [c.name for c in res["people"]]
    for email, meta in res["emails"].items():
        if not email_matches_business(email, b["domain"]):
            continue  # testimonial/client/widget email from someone else's domain
        tier, vmeta = validate_email(email, meta["label"], cfg)
        affinity = classify_email_affinity(email, b["domain"], b["name_norm"], existing_names)
        if affinity == "foreign":
            continue  # never this business's mailbox — the domain gate above only proved freemail-or-own
        if affinity == "freemail_unlinked":
            tier = "risky"
            vmeta = {**vmeta, "reason": "freemail_unlinked"}
        if tier == "valid":
            counts["emails_valid"] += 1
        cid = db.add_contact(conn, Contact(business_id=bid, kind="email", value=email, label=meta["label"],
                                           tier=tier, verified_at=now_iso(), meta=vmeta, affinity=affinity))
        snippet = meta.get("context") or email
        db.add_evidence(conn, Evidence(business_id=bid, ref_table="contacts", ref_id=cid, fact="email_found",
                                       url=meta["url"], snippet=snippet, observed_at=now_iso()))
        counts["contacts"] += 1
    for phone in res["phones"]:
        db.add_contact(conn, Contact(business_id=bid, kind="phone", value=phone, label="site", tier="valid",
                                     verified_at=now_iso()))
        counts["contacts"] += 1
    for net, url in res["socials"].items():
        db.add_contact(conn, Contact(business_id=bid, kind="social", value=url, label=net, tier="valid"))
    for cand in res["people"]:
        db.add_person(conn, Person(business_id=bid, name=cand.name, title=cand.title, source_url=cand.source_url,
                                   snippet=cand.snippet, labeled_by="heuristic", labeled_at=now_iso(),
                                   origin="heuristic"))
        db.add_evidence(conn, Evidence(business_id=bid, ref_table="people", fact="dm_candidate",
                                       url=cand.source_url, snippet=f"{cand.name} — {cand.title}", observed_at=now_iso()))
        counts["dm_candidates"] += 1
    if registry_result is not None:
        registry_people, profile, _any_covered = registry_result
        _persist_registry_result(conn, b, counts, registry_people, profile)
    else:
        _registry_cross_check(conn, b, counts, registries or [])
    _apply_gbp(conn, b, res["signals"], counts)
    res["signals"]["phone_confirmed"] = bool(b["phone_e164"]) and any(p == b["phone_e164"] for p in res["phones"])
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
    enrich: dict = {}
    if res["ok"]:
        enrich["crawled_at"] = now_iso()
    else:
        enrich["attempted_at"] = now_iso()
        enrich["error"] = res.get("error") or "crawl_failed"
    enrich.update({"signals": res["signals"], "socials": res["socials"], "pages": res["pages"],
                   "needs_browser": res["needs_browser"], "registry_checked": bool(registries)})
    if res.get("profile"):
        enrich["profile"] = res["profile"]
    if social_presence:
        enrich["social_presence"] = social_presence
    db.update_enrich(conn, bid, enrich)


def _lookup_registry_for(b, registries: list) -> tuple[list[tuple[Person, Evidence]], dict | None, bool]:
    """v0.3 speed unit (2026-09-02, build item 4): the NETWORK-only half of U4.6's officer lookup for
    ONE business — no `conn` access, so it is safe to call from any thread. Split out of
    _registry_cross_check so the overlapped 'all' stage (_run_overlapped) can run this in its crawl
    driver thread (right after a business finishes crawling, concurrently with everything else) and
    hand the RESULT to _persist_registry_result, which does the actual DB write on the connection's
    owning thread. -> (people_with_evidence, profile, any_jurisdiction_covered).

    any_jurisdiction_covered: True iff at least one registry's jurisdictions() contained this
    business's country — independent of whether a company was actually matched. The overlapped stage's
    registry-backlog driver needs this to replicate _registry_stage's own behavior exactly: a business
    in no covered jurisdiction gets NO write at all (stays eligible for a future run once a covering
    registry exists), not a registry_checked=True that would silently hide it forever."""
    from leadforge.providers.registry import CompaniesHouseRegistry
    country = (b["address_country"] or "").strip().upper()
    registry_people: list[tuple[Person, Evidence]] = []
    profile: dict | None = None
    any_covered = False
    for reg in registries:
        if country not in reg.jurisdictions():
            continue
        any_covered = True
        if isinstance(reg, CompaniesHouseRegistry):
            people, p = reg.lookup_with_profile(b)
            if p:
                profile = p
        else:
            people = reg.lookup(b)
        registry_people.extend(people)
    return registry_people, profile, any_covered


def _persist_registry_result(conn: sqlite3.Connection, b, counts: dict,
                             registry_people: list[tuple[Person, Evidence]], profile: dict | None) -> None:
    """DB-write-only half of U4.6 — MUST run on conn's owning thread. Shared by the inline crawl path
    (_registry_cross_check, below) and the overlapped 'all' stage's registry driver (_run_overlapped),
    so both produce byte-identical DB state for the same lookup result (proved by
    test_overlapped_stages_match_serial_db_state)."""
    for person, ev in registry_people:
        db.add_person(conn, person)
        db.add_evidence(conn, ev)
        counts["dm_candidates"] += 1
    # persist the profile on the crawled path too — it used to be written only on the site-less
    # path (_registry_stage), so a crawled business's Registry Name/Match columns came out blank.
    enrich_update: dict = {"registry_checked": True}
    if profile:
        enrich_update["registry_profile"] = profile
    db.update_enrich(conn, b["id"], enrich_update)
    _auto_pick_registry_dm(conn, b, [p for p, _ in registry_people], counts, profile)


def _registry_cross_check(conn: sqlite3.Connection, b, counts: dict, registries: list) -> None:
    """U4.6: officer lookup from public registries — key-gated (empty list when no keys), country-gated.
    Serial/inline path: look up then persist, both on the calling thread. See _lookup_registry_for /
    _persist_registry_result for the split used by the overlapped 'all' stage."""
    if not registries:
        return
    # NOTE: the crawl-inline path has always unconditionally written registry_checked here even when
    # no jurisdiction covered the business (pre-existing behavior, kept byte-for-byte on purpose — see
    # _run_overlapped's registry-backlog driver for where the distinction DOES matter).
    registry_people, profile, _any_covered = _lookup_registry_for(b, registries)
    _persist_registry_result(conn, b, counts, registry_people, profile)


def _apply_gbp(conn: sqlite3.Connection, b, signals: dict, counts: dict) -> dict:
    """Google Business Profile facts the discover stage already stored in enrich_json.gbp (unit A) —
    a booking link is a strong contactability signal, and an owner-reply signer / repeatedly-credited
    reviewer is a real decision-maker lead even with no website at all."""
    import json
    try:
        enrich_json = json.loads(b["enrich_json"] or "{}") if "enrich_json" in b.keys() else {}
    except (json.JSONDecodeError, TypeError):
        enrich_json = {}
    gbp = enrich_json.get("gbp") or {}
    if not gbp:
        return {}
    if gbp.get("booking_links"):
        signals["booking_hint"] = True
        signals["booking_source"] = "gbp"
    existing = {p["name"].casefold() for p in db.people_for(conn, b["id"]) if p["origin"] == "gbp"}
    maps_url = b["maps_url"] if "maps_url" in b.keys() else ""
    for name in (gbp.get("reply_signatures") or []):
        if not name or name.casefold() in existing:
            continue
        db.add_person(conn, Person(business_id=b["id"], name=name, title="", labeled_by="gbp", origin="gbp",
                                   snippet="signed an owner reply on Google", source_url=maps_url or "",
                                   labeled_at=now_iso()))
        existing.add(name.casefold())
        counts["dm_candidates"] = counts.get("dm_candidates", 0) + 1
    review_counts: dict[str, int] = {}
    for name in (gbp.get("review_names") or []):
        if name:
            review_counts[name] = review_counts.get(name, 0) + 1
    for name, n in review_counts.items():
        if name.casefold() in existing:
            continue
        plural = "review" if n == 1 else "reviews"
        db.add_person(conn, Person(business_id=b["id"], name=name, title="", labeled_by="gbp", origin="gbp",
                                   snippet=f"credited by customers in {n} {plural}", source_url=maps_url or "",
                                   labeled_at=now_iso()))
        existing.add(name.casefold())
        counts["dm_candidates"] = counts.get("dm_candidates", 0) + 1
    return gbp


def _gbp_stage(conn: sqlite3.Connection, cfg: Config, counts: dict) -> None:
    """Site-less businesses never reach _crawl_stage (where _apply_gbp normally runs inline), so
    their GBP facts — booking links, owner-reply signers, review-credited names — need their own pass."""
    import json
    rows = conn.execute(
        """SELECT * FROM businesses b
           WHERE json_extract(b.enrich_json,'$.gbp') IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM people p WHERE p.business_id=b.id AND p.origin='gbp')"""
    ).fetchall()
    for b in rows:
        signals: dict = {}
        _apply_gbp(conn, b, signals, counts)
        if signals:
            prior = json.loads(b["enrich_json"] or "{}").get("signals") or {}
            db.update_enrich(conn, b["id"], {"signals": {**prior, **signals}})
    conn.commit()


_CORPORATE_OFFICER_RE = None


def _is_corporate_officer(name: str) -> bool:
    global _CORPORATE_OFFICER_RE
    if _CORPORATE_OFFICER_RE is None:
        import re
        _CORPORATE_OFFICER_RE = re.compile(r"\b(ltd|llp|limited|plc|inc|gmbh|company|corporation)\b", re.IGNORECASE)
    return bool(_CORPORATE_OFFICER_RE.search(name))


def _auto_pick_registry_dm(conn: sqlite3.Connection, b, registry_people: list[Person], counts: dict,
                           profile: dict | None = None) -> None:
    """v0.1.1 (amends ADR-003): exactly ONE active individual director from the official registry is
    stronger evidence than any agent inference — auto-mark them DM so big runs don't queue the
    obvious cases. 0 or 2+ individuals (or corporate officers only) stay with the agent.

    v0.3: also requires a matched registry `profile` whose company_status is active — the registry
    lookup already filters on this (name_similarity + active_only), but auto-picking a DM is
    consequential enough to assert it here rather than trust the caller silently."""
    if profile is None or (profile.get("company_status") or "").casefold() != "active":
        return
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
    """v0.3 speed unit (2026-09-02, build item 5): MX lookups run in a small thread pool
    (cfg.enrich.dns_workers) via validate_emails_parallel — DNS is I/O-bound and validate_email's own
    per-domain cache is unaffected (still one lookup per distinct domain, just concurrent ones now).
    Only THIS thread ever touches `conn` (the pool workers do network I/O only) — safe to call both
    standalone (`enrich --stage validate`) and repeatedly from the overlapped 'all' stage's idle loop,
    where each call only ever sees whatever is unresolved AT THAT MOMENT (already-resolved rows don't
    match the WHERE clause again, so repeat calls cost a cheap empty-ish SELECT, not re-work)."""
    rows = conn.execute("SELECT * FROM contacts WHERE kind='email' AND tier IN ('unknown','')").fetchall()
    if not rows:
        return
    emit_progress("validate", 0, len(rows), "starting")
    value_by_id = {r["id"]: r["value"] for r in rows}
    results = validate_emails_parallel(rows, cfg)  # [(row_id, tier, meta), ...] in completion order
    for i, (row_id, tier, vmeta) in enumerate(results):
        emit_progress("validate", i + 1, len(rows), value_by_id.get(row_id, ""))  # a 700-email DNS tail is minutes
        conn.execute("UPDATE contacts SET tier=?, verified_at=?, meta_json=? WHERE id=?",
                     (tier, now_iso(), _json(vmeta), row_id))
        if tier == "valid":
            counts["emails_valid"] += 1
    conn.commit()


def _json(d: dict) -> str:
    import json
    return json.dumps(d)
