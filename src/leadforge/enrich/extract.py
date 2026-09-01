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
# Deliberate obfuscation only: "[at]"/"(at)"/" at " + "[dot]"/"(dot)"/" dot ". A bare "@" or "."
# is already a plain email (EMAIL_RE) — matching it here split ordinary words ("strategy.in" -> str@egy.in).
AT_DOT_RE = re.compile(
    r"\b([A-Za-z0-9._%+\-]+)\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\sat\s)\s*([A-Za-z0-9\-]+)"
    r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\sdot\s)\s*([A-Za-z]{2,})\b",
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
# [ \t] only between name words: a newline between two capitalized words is a layout boundary,
# not a name ("Max Sherwin\nMax is..." must yield "Max Sherwin", never "Sherwin\nMax").
NAME_RE = re.compile(r"\b([A-Z][a-z]{1,15}(?:[ \t]+[A-Z]\.)?(?:[ \t]+[A-Z][a-z]{1,20}){1,2})\b")
NAME_STOPWORDS = {
    "our team", "the team", "contact us", "about us", "read more", "learn more", "get in", "call us",
    "monday friday", "customer service", "quality service", "family owned",
}
# Single tokens that never appear in a real person's name — kills "And The Team", "Best Prices ..."
NAME_WORD_STOPLIST = {
    "and", "the", "our", "your", "best", "prices", "price", "guaranteed", "team", "quality",
    "service", "services", "meet", "welcome", "about", "contact", "call", "today", "free",
    "estimate", "estimates", "shop", "auto", "repair", "hours", "open", "book", "now",
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
    trusted: set[str] = set()  # came from mailto/cfemail/text/at-dot — cannot be a markup-split artifact

    def _add(raw: str, trust: bool = True) -> None:
        email = raw.strip().strip(".,;:<>()[]\"'").lower()
        if not EMAIL_RE.fullmatch(email):
            return
        if any(j in email for j in JUNK_EMAIL_HOSTS):
            return
        local = email.split("@", 1)[0]
        found.setdefault(email, "role" if local in ROLE_LOCALPARTS else "personal")
        if trust:
            trusted.add(email)

    tree = HTMLParser(html)
    for a in tree.css('a[href^="mailto:"]'):
        _add(html_mod.unescape((a.attributes.get("href") or "")[7:].split("?")[0]))
    for hexstr in CFEMAIL_RE.findall(html):
        decoded = decode_cfemail(hexstr)
        if decoded:
            _add(decoded)
    for m in EMAIL_RE.findall(html_mod.unescape(html)):
        _add(m, trust=False)  # raw markup can split a local part ("<b>i</b>nfo@x.com" -> "nfo@x.com")
    for m in EMAIL_RE.findall(text):
        _add(m)
    for m in AT_DOT_RE.finditer(text):
        _add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    # Truncation artifacts: drop an email when a longer address ends with it AND either (a) the short
    # one was seen only in raw markup, or (b) the longer one is a role address ("info@" -> "nfo@" can
    # surface in extracted text too). A trusted distinct pair like ann@/joann@ (both personal) survives.
    def _artifact(e: str) -> bool:
        for o in found:
            if o != e and o.endswith(e):
                if e not in trusted:
                    return True
                if o.split("@", 1)[0] in ROLE_LOCALPARTS:
                    return True
        return False

    return {e: lab for e, lab in found.items() if not _artifact(e)}


FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "yahoo.com",
    "yahoo.co.uk", "icloud.com", "aol.com", "btinternet.com", "live.com", "live.co.uk",
    "protonmail.com", "proton.me", "mail.com", "me.com", "msn.com",
}


def email_matches_business(email: str, business_domain: str | None) -> bool:
    """An email found on a business's own site is theirs only if its domain is the site's domain
    (any subdomain) or a personal freemail box. Anything else is a testimonial/client/widget email."""
    if not business_domain:
        return True  # nothing to compare against; keep and let validation tier it
    dom = email.rsplit("@", 1)[-1].lower().removeprefix("www.")
    biz = business_domain.lower().removeprefix("www.")
    return dom == biz or dom.endswith("." + biz) or biz.endswith("." + dom) or dom in FREEMAIL_DOMAINS


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
        and not any(w.casefold() in NAME_WORD_STOPLIST for w in m.group(1).split())
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


# --- U4.7: optional GLiNER zero-shot upgrade (extra [ner]) -----------------------------------
_GLINER_MODEL = None


def ner_available() -> bool:
    try:
        import gliner  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _gliner_model():
    global _GLINER_MODEL
    if _GLINER_MODEL is None:
        from gliner import GLiNER
        _GLINER_MODEL = GLiNER.from_pretrained("urchade/gliner_small-v2.1")
    return _GLINER_MODEL


def extract_people_ner(text: str, source_url: str, max_candidates: int = 8) -> list[PersonCandidate]:
    """GLiNER path — same return type and caps as extract_people(); caller falls back when unavailable."""
    model = _gliner_model()
    # 0.4 keeps short titles like "Owner" (scores ~0.45) that 0.5 drops.
    ents = model.predict_entities(text[:6000], ["person name", "job title"], threshold=0.4)
    names = [e for e in ents if e["label"] == "person name"]
    titles = [e for e in ents if e["label"] == "job title"]

    def _gap(t, n):
        # character gap between the two spans (0 when adjacent/overlapping)
        return max(t["start"] - n["end"], n["start"] - t["end"], 0)

    def _plausible_name(name: str) -> bool:
        words = name.split()
        return (1 <= len(words) <= 4
                and all(w[0].isupper() for w in words)
                and not any(w.casefold() in NAME_WORD_STOPLIST for w in words)
                and name.casefold() not in NAME_STOPWORDS)

    out: list[PersonCandidate] = []
    seen: set[str] = set()
    for n in names:
        name = re.sub(r"\s+", " ", n["text"]).strip()  # GLiNER spans can cross layout newlines
        key = name.casefold()
        if key in seen or not _plausible_name(name):
            continue
        # a title belongs to a name only when nearly adjacent — 40 chars, not a whole sentence away
        near = [t for t in titles if _gap(t, n) <= 40]
        title = min(near, key=lambda t: _gap(t, n))["text"] if near else ""
        seen.add(key)
        snip_start = max(0, n["start"] - 140)
        snippet = re.sub(r"\s+", " ", text[snip_start : snip_start + 300]).strip()
        out.append(PersonCandidate(name=name, title=re.sub(r"\s+", " ", title).strip().title(),
                                   snippet=snippet, source_url=source_url))
        if len(out) >= max_candidates:
            break
    return out


# ============================================================================ v0.3 interfaces (U9.6)
# Wave-1 unit C1 owns the real implementations; the bodies below are the minimal contract so parallel
# units can build against the final names today.
JUNK_LOCALPARTS = {
    "test", "sample", "demo", "example", "someone", "yourname", "your", "user", "username", "name", "email",
    "noreply", "no-reply", "no_reply", "donotreply", "postmaster", "abuse", "webmaster", "hostmaster", "root",
    "mailer-daemon", "null", "none", "asdf", "xxx",
}


def classify_email_affinity(email: str, business_domain: str | None, business_name_norm: str = "",
                            people_names: list[str] | None = None) -> str:
    """-> 'own_domain' | 'freemail_linked' | 'freemail_unlinked' | 'foreign'.

    own_domain: the address is on the business's own domain (or a subdomain).
    freemail_linked: a gmail/hotmail/... box whose local part plausibly belongs to this business — it
        shares a token (>= 3 chars) or initials with the business name, or matches a person on record
        ('First Last' or 'Last, First').
    freemail_unlinked: a freemail box with no such link (a template credit, a client, a stranger).
    foreign: any other domain — never the business's own."""
    dom = email.rsplit("@", 1)[-1].casefold().removeprefix("www.")
    local = email.split("@", 1)[0].casefold()
    if business_domain:
        biz = business_domain.casefold().removeprefix("www.")
        if dom == biz or dom.endswith("." + biz) or biz.endswith("." + dom):
            return "own_domain"
    if dom not in FREEMAIL_DOMAINS:
        return "own_domain" if not business_domain else "foreign"
    local_alnum = re.sub(r"[^a-z0-9]", "", local)
    tokens = [t for t in re.split(r"[^a-z0-9]+", (business_name_norm or "").casefold()) if len(t) >= 3]
    if any(t in local_alnum for t in tokens):
        return "freemail_linked"
    initials = "".join(t[0] for t in re.split(r"\s+", (business_name_norm or "").casefold()) if t)
    if len(initials) >= 2 and local_alnum.startswith(initials):
        return "freemail_linked"
    for raw in people_names or []:
        parts = [re.sub(r"[^a-z0-9]", "", p) for p in re.split(r"[\s,]+", str(raw).casefold()) if p]
        parts = [p for p in parts if len(p) >= 3]
        if parts and (any(p in local_alnum for p in parts)):
            return "freemail_linked"
    return "freemail_unlinked"


def email_context(text: str, email: str, window: int = 90) -> str:
    """The page text around an address, for the evidence row (was: the bare address, which proves nothing)."""
    if not text:
        return email
    idx = text.casefold().find(email.casefold())
    if idx < 0:
        return email
    start, end = max(0, idx - window), min(len(text), idx + len(email) + window)
    return " ".join(text[start:end].split())
