"""Inferred email addresses (v0.2.0) — a LIKELY address, derived from public evidence only.

Most small businesses publish no personal email at all: in the live 709-lead UK run, 154 businesses
had a named decision maker and a working mail domain but no published address. This module proposes
the address that domain's own naming convention implies.

Hard boundaries (icm/SCOPE.md #5 is NOT relaxed by this file):
  - NO SMTP, no RCPT probing, no mailbox enumeration. Evidence is DNS MX + emails already found
    on that domain by the ordinary crawl. Nothing here contacts a mail server.
  - NO ANCHOR, NO GUESS. The local-part shape must be demonstrated by a real personal email already
    seen on the SAME domain. We never assume "first.last" because it is common.
  - Freemail domains are excluded: `jane.smith@gmail.com` is some unrelated real person's mailbox,
    not a business convention.
  - Output is always labeled `inferred`, kept in its own column, and never counted as a found
    email in coverage stats. It is a lead for a human to confirm, not a verified contact.
"""

from __future__ import annotations

import re
import unicodedata

from leadforge.config import Config
from leadforge.enrich.extract import FREEMAIL_DOMAINS, ROLE_LOCALPARTS
from leadforge.util import natural_name

# name particles that belong to the surname ("de la Cruz" -> delacruz)
_PARTICLES = {"de", "del", "della", "der", "van", "von", "da", "di", "du", "la", "le", "el", "bin",
              "ibn", "al", "st", "mac", "mc", "o"}
_LEGAL_TOKENS = {"ltd", "limited", "llc", "llp", "plc", "inc", "gmbh", "corp", "corporation", "co",
                 "company", "group", "holdings", "services", "solutions", "trading"}
# words that prove a dotted local part is NOT a person: 'experienced.hire@', 'new.business@' are
# departmental addresses that would otherwise read as a first.last convention (seen live, 2026-08-31)
_NON_NAME_TOKENS = {
    "hire", "hires", "hiring", "business", "new", "experienced", "graduate", "grad", "student",
    "career", "careers", "job", "jobs", "recruit", "recruitment", "hr", "press", "media", "news",
    "marketing", "sales", "support", "help", "service", "services", "customer", "customers",
    "account", "accounts", "billing", "invoice", "invoices", "payroll", "tax", "audit", "legal",
    "team", "office", "reception", "front", "desk", "general", "main", "enquiry", "enquiries",
    "inquiry", "inquiries", "contact", "hello", "mail", "email", "web", "website", "no", "noreply",
    "do", "not", "reply", "admin", "administrator", "post", "postmaster", "abuse", "privacy",
    "data", "protection", "complaints", "feedback", "quote", "quotes", "booking", "bookings",
}
_NON_ALNUM = re.compile(r"[^a-z0-9]")

# pattern name -> how to build the local part from (first, last)
PATTERNS: dict[str, callable] = {
    "first.last": lambda fn, ln: f"{fn}.{ln}",
    "firstlast": lambda fn, ln: f"{fn}{ln}",
    "first_last": lambda fn, ln: f"{fn}_{ln}",
    "flast": lambda fn, ln: f"{fn[0]}{ln}",
    "f.last": lambda fn, ln: f"{fn[0]}.{ln}",
    "first": lambda fn, ln: fn,
    "last.first": lambda fn, ln: f"{ln}.{fn}",
    "lastf": lambda fn, ln: f"{ln}{fn[0]}",
}


def _ascii_fold(s: str) -> str:
    """'Müller' -> 'muller': mail local parts are ascii in practice."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def split_person_name(raw: str) -> tuple[str | None, str | None]:
    """'Sean Vincent Murphy' -> ('sean','murphy'). Returns (None, None) when the string is not a
    usable person name (single token, empty, or company-shaped)."""
    name = natural_name(raw or "").strip()
    tokens = [_NON_ALNUM.sub("", _ascii_fold(t)) for t in name.split()]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return None, None
    if any(t in _LEGAL_TOKENS for t in tokens):
        return None, None  # a company, not a person
    first = tokens[0]
    # trailing particles belong to the surname: "Ana de la Cruz" -> "delacruz"
    rest = tokens[1:]
    tail_start = len(rest) - 1
    while tail_start > 0 and rest[tail_start - 1] in _PARTICLES:
        tail_start -= 1
    last = "".join(rest[tail_start:])
    if not first or not last:
        return None, None
    return first, last


def pattern_from_anchor(local: str, first: str, last: str) -> str | None:
    """Which naming convention would turn (first,last) into this local part? None if unrecognizable.

    The anchor is a real address already seen on the domain; matching it against a KNOWN person's
    name is what converts 'a common guess' into 'this domain's demonstrated convention'.
    """
    local = _ascii_fold(local or "")
    if not local or not first or not last:
        return None
    if local in ROLE_LOCALPARTS:
        return None  # info@/sales@ says nothing about how people are addressed
    for name, build in PATTERNS.items():
        if build(first, last) == local:
            return name
    return None


def _mx_ok(domain: str, timeout: float) -> bool:
    """Domain actually receives mail. Uses the same cached MX lookup as validation (no SMTP)."""
    from leadforge.enrich.validate import _mx_cached
    return _mx_cached(domain.lower(), timeout) is True


def infer_email(person_name: str, domain: str, known_emails: list[str], cfg: Config) -> dict | None:
    """Propose the address `person_name` most likely has at `domain`, or None.

    known_emails are addresses the crawl already found (any domain); only same-domain PERSONAL ones
    act as anchors. Returns {"email","pattern","confidence","basis"} — never a bare string, because
    the sheet must always be able to show WHY.
    """
    if not getattr(cfg.validation, "infer_emails", False):
        return None  # opt-in only
    domain = (domain or "").strip().lower().removeprefix("www.")
    if not domain or domain in FREEMAIL_DOMAINS:
        return None
    first, last = split_person_name(person_name)
    if not first or not last:
        return None

    # 1) find anchors: real addresses on THIS domain whose shape we can explain
    anchors: list[tuple[str, str]] = []  # (pattern, anchor_email)
    for addr in known_emails:
        addr = (addr or "").strip().lower()
        if "@" not in addr:
            continue
        a_local, _, a_domain = addr.partition("@")
        if a_domain != domain or a_local in ROLE_LOCALPARTS:
            continue
        # the anchor's own name is unknown, so infer the shape structurally: a local part that
        # splits into two name-ish chunks reveals the separator/order convention
        pat = _shape_of(a_local)
        if pat:
            anchors.append((pat, addr))
    if not anchors:
        return None

    # 2) the convention is the most common shape among anchors; agreement raises confidence
    counts: dict[str, int] = {}
    for pat, _ in anchors:
        counts[pat] = counts.get(pat, 0) + 1
    pattern = max(counts, key=lambda p: counts[p])
    agreeing = counts[pattern]
    example = next(a for p, a in anchors if p == pattern)

    # 3) MX must confirm the domain receives mail at all
    if not _mx_ok(domain, cfg.validation.dns_timeout_s):
        return None

    local = PATTERNS[pattern](first, last)
    confidence = min(0.75, 0.45 + 0.15 * (agreeing - 1))  # never presented as near-certain
    return {
        "email": f"{local}@{domain}",
        "pattern": pattern,
        "confidence": round(confidence, 2),
        "basis": f"pattern {pattern} from {example}"
                 + (f" (+{agreeing - 1} more)" if agreeing > 1 else ""),
    }


def _shape_of(local: str) -> str | None:
    """Infer a naming convention from an anchor local part alone, without knowing whose it is.

    'bob.jones' -> first.last · 'b.jones' -> f.last · 'bjones' is ambiguous (flast vs firstlast)
    and is deliberately NOT used as an anchor: guessing from it would be guessing twice.
    """
    local = _ascii_fold(local or "")
    if not local or local in ROLE_LOCALPARTS:
        return None
    for sep, two, one in ((".", "first.last", "f.last"), ("_", "first_last", None)):
        if local.count(sep) == 1:
            a, _, b = local.partition(sep)
            if not (a.isalpha() and b.isalpha() and len(b) >= 2):
                continue
            if a in _NON_NAME_TOKENS or b in _NON_NAME_TOKENS:
                return None  # departmental address wearing a person's shape
            return one if len(a) == 1 else two
    return None
