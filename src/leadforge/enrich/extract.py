"""Deterministic contact & people extraction (U4.2) — docs/04 §3.3. Pure functions, heavily tested.

Email deobfuscation covered: mailto:, plain regex, Cloudflare data-cfemail XOR, "name [at] domain [dot] com".
People candidates: Title-keyword within 60 chars of a Capitalized Name -> snippet (<= 300 chars) + source URL.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass

import phonenumbers
from selectolax.parser import HTMLParser

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
AT_DOT_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[|\()?\s*(?:at|@)\s*(?:\]|\))?\s*([A-Za-z0-9\-]+)\s*(?:\[|\()?\s*(?:dot|\.)\s*(?:\]|\))?\s*([A-Za-z]{2,})",
    re.IGNORECASE,
)
CFEMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
ROLE_LOCALPARTS = {
    "info", "sales", "office", "contact", "hello", "admin", "support", "team", "mail",
    "enquiries", "inquiries", "service", "booking", "bookings", "reception", "billing", "jobs", "careers",
}
JUNK_EMAIL_HOSTS = ("example.", "sentry.", "wixpress.", "@2x", ".png", ".jpg", ".gif", ".webp")

TITLE_WORDS = (
    "owner", "co-owner", "founder", "co-founder", "ceo", "president", "principal", "partner",
    "general manager", "managing director", "director", "manager", "gm", "broker", "realtor",
    "supervisor", "head of", "chief", "proprietor", "geschäftsführer", "inhaber",
)
TITLE_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in TITLE_WORDS) + r")\b", re.IGNORECASE)
NAME_RE = re.compile(r"\b([A-Z][a-z]{1,15}(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z]{1,20}){1,2})\b")
NAME_STOPWORDS = {
    "our team", "the team", "contact us", "about us", "read more", "learn more", "get in", "call us",
    "monday friday", "customer service", "quality service", "family owned",
}


@dataclass
class PersonCandidate:
    name: str
    title: str
    snippet: str
    source_url: str


def decode_cfemail(hexstr: str) -> str | None:
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    out = bytes(b ^ key for b in data[1:]).decode("utf-8", errors="ignore")
    return out if EMAIL_RE.fullmatch(out) else None


def extract_emails(html: str, text: str) -> dict[str, str]:
    """-> {email: label} with label role|personal."""
    found: dict[str, str] = {}

    def _add(raw: str) -> None:
        email = raw.strip().strip(".,;:<>()[]\"'").lower()
        if not EMAIL_RE.fullmatch(email):
            return
        if any(j in email for j in JUNK_EMAIL_HOSTS):
            return
        local = email.split("@", 1)[0]
        found.setdefault(email, "role" if local in ROLE_LOCALPARTS else "personal")

    tree = HTMLParser(html)
    for a in tree.css('a[href^="mailto:"]'):
        _add(html_mod.unescape((a.attributes.get("href") or "")[7:].split("?")[0]))
    for hexstr in CFEMAIL_RE.findall(html):
        decoded = decode_cfemail(hexstr)
        if decoded:
            _add(decoded)
    for blob in (html_mod.unescape(html), text):
        for m in EMAIL_RE.findall(blob):
            _add(m)
    for m in AT_DOT_RE.finditer(text):
        _add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    return found


def extract_phones(html: str, text: str, region: str) -> list[str]:
    """-> unique E.164 strings (valid numbers only)."""
    out: dict[str, None] = {}
    tree = HTMLParser(html)
    candidates = [
        (a.attributes.get("href") or "")[4:] for a in tree.css('a[href^="tel:"]')
    ]
    for source in candidates:
        try:
            parsed = phonenumbers.parse(source, region)
            if phonenumbers.is_valid_number(parsed):
                out.setdefault(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))
        except phonenumbers.NumberParseException:
            continue
    for match in phonenumbers.PhoneNumberMatcher(text[:100_000], region):
        if phonenumbers.is_valid_number(match.number):
            out.setdefault(phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164))
    return list(out)


def extract_socials(html: str) -> dict[str, str]:
    """-> {network: url} (first URL per network)."""
    from leadforge.util import social_network

    out: dict[str, str] = {}
    tree = HTMLParser(html)
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        net = social_network(href)
        if net and net not in out:
            out[net] = href.split("?")[0]
    return out


def _first_name(zone: str, prefer_end: bool) -> tuple[str, int] | None:
    """Find a plausible person name in a text zone that itself contains no title word.
    prefer_end=True returns the LAST match (name just before a trailing title)."""
    matches = [
        m for m in NAME_RE.finditer(zone)
        if m.group(1).casefold() not in NAME_STOPWORDS
        and len(m.group(1).split()) >= 2
        and not TITLE_RE.search(m.group(1))  # never let a title phrase pose as a name
    ]
    if not matches:
        return None
    m = matches[-1] if prefer_end else matches[0]
    return m.group(1).strip(), m.start()


def extract_people(text: str, source_url: str, max_candidates: int = 8) -> list[PersonCandidate]:
    """A title keyword adjacent to a plausible person name -> candidate + snippet.

    We scan the zones AROUND each title (after it for "Owner Jane Doe", before it for "Jane Doe, Owner")
    rather than a window spanning the title, so the title word is never captured as part of the name.
    """
    out: list[PersonCandidate] = []
    seen: set[str] = set()
    for m in TITLE_RE.finditer(text):
        after = text[m.end() : m.end() + 60]
        before = text[max(0, m.start() - 60) : m.start()]
        hit = _first_name(after, prefer_end=False) or _first_name(before, prefer_end=True)
        if not hit:
            continue
        name, _ = hit
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        snip_start = max(0, m.start() - 140)
        snippet = re.sub(r"\s+", " ", text[snip_start : snip_start + 300]).strip()
        out.append(PersonCandidate(name=name, title=m.group(1).title(), snippet=snippet, source_url=source_url))
        if len(out) >= max_candidates:
            return out
    return out
