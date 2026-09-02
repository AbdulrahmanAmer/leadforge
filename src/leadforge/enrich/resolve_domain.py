"""Domain resolution for company-mode businesses (Unit H, docs/09 Wave 2 H).

Companies House has no website field. This guesses candidate domains from the registered legal name
(minus legal-entity tokens) and verifies each by actually fetching it — through the existing
SiteCrawler, so robots.txt and per-host politeness delay are respected exactly as everywhere else in
the pipeline — and requiring the registered postcode, the legal name (minus legal tokens), or the
company number to appear on the page. No search engines, no paid lookups (ADR-012): every candidate is
a guess this module makes itself from public registry data, and every guess is checked directly.
"""

from __future__ import annotations

import json
import re
import sqlite3

from leadforge import db
from leadforge.config import Config
from leadforge.models import Evidence
from leadforge.util import LOG, now_iso

_LEGAL_TOKENS = {
    "limited", "ltd", "llp", "llc", "plc", "lp", "cic", "cio", "inc", "incorporated",
    "corp", "corporation", "co", "company", "the",
}
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_TLDS = (".co.uk", ".com", ".uk")


def _content_words(name: str) -> list[str]:
    words = [w.lower() for w in _WORD_RE.findall(name or "")]
    return [w for w in words if w not in _LEGAL_TOKENS]


def _candidate_domains(name: str) -> list[str]:
    """slug variants (joined / hyphenated / first-two-words) x (.co.uk, .com, .uk), in that order —
    a UK company's own TLD is the most likely hit, so it's worth checking first."""
    words = _content_words(name)
    if not words:
        return []
    joined = "".join(words)
    hyphenated = "-".join(words)
    first_two = "".join(words[:2]) if len(words) >= 2 else joined
    slugs = list(dict.fromkeys([joined, hyphenated, first_two]))  # de-dupe, keep order
    return [f"{slug}{tld}" for slug in slugs for tld in _TLDS]


def _verify(crawler, domain: str, postcode: str, name_tokens: list[str], company_number: str) -> tuple[bool, str]:
    """Fetch the root page (crawler.crawl() — robots/delay enforced there) and look for the registered
    postcode, the legal name minus legal tokens, or the company number. -> (matched, evidence snippet)."""
    result = crawler.crawl(f"https://{domain}")
    if not result.ok or not result.pages:
        return False, ""
    page = result.pages[0]
    hay = (page.text + " " + page.html).casefold()
    hay_nospace = hay.replace(" ", "")
    if postcode and postcode.strip():
        needle = postcode.strip().casefold().replace(" ", "")
        if needle and needle in hay_nospace:
            return True, postcode.strip()
    name_join = " ".join(name_tokens).strip()
    if name_join and name_join.casefold() in hay:
        return True, name_join
    if company_number and company_number.strip().casefold() in hay:
        return True, company_number.strip()
    return False, ""


def resolve(conn: sqlite3.Connection, cfg: Config, business_row) -> dict | None:
    """Try every candidate domain for one business; on the first match, persist website/domain +
    Evidence(fact="domain_resolved") and return a summary dict. On total failure, records
    enrich["domain_resolution"] = {"tried": [...], "resolved": False} itself and returns None."""
    from leadforge.enrich.crawler import SiteCrawler

    name = business_row["name"] or ""
    words = _content_words(name)
    candidates = _candidate_domains(name)
    if not candidates:
        db.update_enrich(conn, business_row["id"], {"domain_resolution": {"tried": [], "resolved": False}})
        return None

    postcode = (business_row["address_postal"] or "").strip()
    enrich = json.loads(business_row["enrich_json"]) if business_row["enrich_json"] else {}
    company_number = str((enrich.get("registry_profile") or {}).get("company_number") or "")

    crawler = SiteCrawler(cfg)
    tried: list[str] = []
    try:
        for domain in candidates:
            tried.append(domain)
            try:
                matched, snippet = _verify(crawler, domain, postcode, words, company_number)
            except Exception as e:  # noqa: BLE001 — one bad candidate must never abort the whole resolve
                LOG.debug("resolve_domain: candidate %s failed for %s: %s", domain, name, type(e).__name__)
                continue
            if matched:
                website = f"https://{domain}"
                conn.execute("UPDATE businesses SET website=?, domain=? WHERE id=?",
                            (website, domain, business_row["id"]))
                db.add_evidence(conn, Evidence(business_id=business_row["id"], ref_table="businesses",
                                               fact="domain_resolved", url=website, snippet=snippet,
                                               observed_at=now_iso()))
                db.update_enrich(conn, business_row["id"],
                                 {"domain_resolution": {"tried": tried, "resolved": True, "domain": domain}})
                conn.commit()
                return {"domain": domain, "website": website, "matched_on": snippet, "tried": tried}
    finally:
        crawler.close()

    db.update_enrich(conn, business_row["id"], {"domain_resolution": {"tried": tried, "resolved": False}})
    return None


def run_resolve(conn: sqlite3.Connection, cfg: Config, limit: int) -> dict:
    """Company-mode businesses (source='companies_house' — the discriminator against a local_business
    run sharing the same DB) with no domain yet and no prior resolution attempt."""
    rows = conn.execute(
        """SELECT * FROM businesses WHERE source='companies_house' AND domain IS NULL
           AND json_extract(enrich_json,'$.domain_resolution') IS NULL
           ORDER BY name LIMIT ?""",
        (limit,),
    ).fetchall()
    counts = {"domain_resolve_attempted": 0, "domain_resolved": 0}
    for b in rows:
        counts["domain_resolve_attempted"] += 1
        if resolve(conn, cfg, b):
            counts["domain_resolved"] += 1
    return counts
