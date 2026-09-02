"""Contact validation -> tiers, never booleans (U4.3) — docs/04 §3.3.

Email tier ladder: syntax (email-validator) -> MX (dnspython, cached) -> disposable -> role classification.
No SMTP RCPT probing (ADR: catch-alls lie + IP-reputation risk). Phone validity via phonenumbers.
"""

from __future__ import annotations

from functools import lru_cache

from leadforge.config import Config
from leadforge.enrich.extract import JUNK_LOCALPARTS

try:
    from disposable_email_domains import blocklist as _DISPOSABLE
except Exception:  # noqa: BLE001 — optional dataset; absence just skips the disposable tier
    _DISPOSABLE = set()


@lru_cache(maxsize=1)
def get_resolver(fallbacks: tuple[str, ...] = ("8.8.8.8", "1.1.1.1"), probe_timeout: float = 3.0):
    """System resolver if it can answer MX queries, else a public-nameserver fallback.

    Corporate/VPN resolvers sometimes drop MX queries entirely; without this every email
    would tier as 'unknown'. Probed once per process, cached.
    """
    import dns.resolver

    system = dns.resolver.Resolver()
    try:
        system.resolve("gmail.com", "MX", lifetime=probe_timeout)
        return system
    except Exception:  # noqa: BLE001 — any failure -> try public fallbacks
        pass
    public = dns.resolver.Resolver(configure=False)
    public.nameservers = list(fallbacks)
    return public


def _mx_exists(domain: str, timeout: float) -> bool | None:
    """True/False if resolvable; None on DNS error (-> tier 'unknown', retryable)."""
    try:
        import dns.resolver

        res = get_resolver()
        try:
            answers = res.resolve(domain, "MX", lifetime=timeout)
            return len(answers) > 0
        except dns.resolver.NoAnswer:
            a = res.resolve(domain, "A", lifetime=timeout)  # some domains accept mail on A
            return len(a) > 0
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            return False
    except Exception:  # noqa: BLE001 — timeout/network -> unknown
        return None


@lru_cache(maxsize=4096)
def _mx_cached(domain: str, timeout: float) -> bool | None:
    return _mx_exists(domain, timeout)


def validate_email(email: str, label: str, cfg: Config) -> tuple[str, dict]:
    """-> (tier, meta). tier in valid|risky|role|catch_all|unknown|invalid."""
    meta: dict = {}
    try:
        from email_validator import validate_email as _ev

        info = _ev(email, check_deliverability=False)
        email = info.normalized
        domain = info.domain
    except Exception as e:  # noqa: BLE001 — any syntax/parse failure => invalid
        return "invalid", {"reason": type(e).__name__}

    # Placeholder localparts (test@, sample@, noreply@, ...) are invalid regardless of MX — a domain
    # can have perfectly good mail servers and still never deliver to "test@" as a real contact. Checked
    # BEFORE any DNS call: extract_emails() already drops these at extraction, but an inferred guess or
    # a manually-added contact can still reach validate_email() directly.
    if email.split("@", 1)[0].casefold() in JUNK_LOCALPARTS:
        return "invalid", {"reason": "placeholder"}

    if _DISPOSABLE and domain.lower() in _DISPOSABLE:
        return "risky", {"reason": "disposable_domain"}

    mx = _mx_cached(domain.lower(), cfg.validation.dns_timeout_s)
    if mx is None:
        return "unknown", {"reason": "dns_timeout"}
    if mx is False:
        return "invalid", {"reason": "no_mx"}

    if label == "role":
        return "role", meta
    return "valid", meta


# --------------------------------------------------------------------------------------------------
# THIS is the one place that states the relationship between the two orderings below. Read this before
# touching either TIER_ORDER or rank_email_contacts() — they answer two different questions and are
# NOT meant to agree on where 'inferred' sits relative to 'risky'/'catch_all':
#
#   TIER_ORDER (below) answers "how much do we TRUST this tier, for coverage/display purposes"
#       (best_email_tier(), the Summary sheet's tier counts, the About sheet's tier legend).
#       valid/role are directly confirmed — syntax + MX resolved, so mail *can* be delivered there.
#       risky/catch_all are OBSERVED (a real address was found) but uncertain (disposable domain; a
#       catch-all that accepts anything, proving nothing). inferred is never observed at all — a guess
#       from the domain's demonstrated naming convention — so as a matter of TRUST it ranks BELOW every
#       tier that came from a real, found address, however uncertain that address's deliverability is.
#       unknown is a DNS timeout (retryable, not a verdict). invalid is worst but still stored for the
#       record (a placeholder localpart, bad syntax, or a domain with no mail servers at all).
#
#   rank_email_contacts() (further below) answers "which address should we actually SEND to" per
#       docs/09-v0.3-build-plan.md's SEND ranking: own-domain valid > own-domain role > freemail_linked
#       valid > inferred > risky > unknown > invalid. Here inferred ranks ABOVE risky/catch_all/unknown
#       ON PURPOSE: a risky/catch_all/unknown address is an OBSERVED address we have positive reason to
#       distrust or cannot yet confirm, while an inferred guess follows the domain's own demonstrated
#       pattern and has no adverse signal against it — worth trying before a known-shaky observed one.
#
# So: TIER_ORDER puts inferred last-but-one among non-invalid tiers (trust); rank_email_contacts puts
# it ahead of risky/catch_all/unknown (sendability). Both are correct for what they measure — this is
# two different questions, not a contradiction. See test_rank_email_contacts_inferred_outranks_risky.
TIER_ORDER = ["valid", "role", "risky", "catch_all", "inferred", "unknown", "invalid"]


def best_email_tier(tiers: list[str]) -> str:
    present = [t for t in TIER_ORDER if t in tiers]
    return present[0] if present else "unknown"


# ============================================================================ v0.3 interface (U9.6)
_AFFINITY_RANK = {"own_domain": 0, "freemail_linked": 1, "": 2, "freemail_unlinked": 3, "foreign": 4}
_TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}


def _sendability_group(affinity: str, tier: str) -> int:
    """Coarse "can we actually use this address" bucket — the PRIMARY sort key for rank_email_contacts.

    v0.3 fix: affinity used to be the sole primary key, which let an own-domain address of ANY tier
    (invalid/unknown/risky/inferred) outrank a validated freemail_linked mailbox — docs/09 puts
    freemail_linked-valid ABOVE inferred, and a validated address of any affinity above an unvalidated
    one on the "right" domain. Grouping by tier-shaped sendability first, with the own_domain/
    freemail_linked distinction only breaking the TOP group, restores that order while still keeping
    "never freemail above own-domain" true inside every group (affinity is the secondary key).

    0: own-domain, confirmed deliverable (valid/role) — the address to actually use.
    1: freemail linked to the business by name/initials/person-match, confirmed deliverable.
    2: inferred — a guess from the domain's own naming convention; never independently observed.
    3: confirmed-deliverable but not usefully linked (freemail_unlinked/foreign valid or role), or
       risky/catch_all (observed but deliverability is uncertain either way).
    4: unknown — a DNS timeout, retryable, not a verdict.
    5: invalid — worst, but still stored for the record.
    """
    if tier == "inferred":
        return 2
    if tier in ("valid", "role"):
        if affinity == "own_domain":
            return 0
        if affinity == "freemail_linked":
            return 1
        return 3
    if tier in ("risky", "catch_all"):
        return 3
    if tier == "unknown":
        return 4
    if tier == "invalid":
        return 5
    return 4  # unrecognized tier -> treat like a DNS-timeout unknown, not a verdict


# Guards the SEND-ranking half of the TIER_ORDER docstring above at import time: inferred (group 2) must
# outrank risky/catch_all (group 3) here even though TIER_ORDER (a different ordering, for a different
# question) ranks them the other way round. See test_rank_email_contacts_inferred_outranks_risky for the
# same guarantee proven through the public function, on real contact rows.
assert _sendability_group("", "inferred") < _sendability_group("", "risky"), (
    "rank_email_contacts must rank inferred ABOVE risky (docs/09 SEND order) — see the TIER_ORDER "
    "docstring above for why this differs from TIER_ORDER's coverage/display order on purpose"
)


def rank_email_contacts(contacts: list) -> list:
    """Email contact rows, best first, per docs/09's SEND ranking: own-domain valid > own-domain role >
    freemail_linked valid > inferred > risky > unknown > invalid. See the TIER_ORDER docstring above for
    why this SEND order differs from TIER_ORDER's coverage/display order on 'inferred' vs 'risky' —
    that is intentional, not a contradiction between the two.

    A freemail box never outranks the business's own mailbox (the v0.2 sheet exported a font designer's
    gmail above a real info@ three times), AND an own-domain address that is invalid/unknown/risky/
    inferred never outranks a validated freemail_linked one either — the PRIMARY sort key is a
    sendability group (see _sendability_group), not affinity alone.

    Works on sqlite3.Row and plain dict inputs alike (both support `in .keys()` and `[]`); contacts
    from an old DB that predates the affinity column simply have none and sort as '' (mid-table, below
    freemail_linked, above freemail_unlinked/foreign)."""
    rows = [c for c in contacts if c["kind"] == "email"]

    def key(c):
        affinity = (c["affinity"] if "affinity" in c.keys() else "") or ""
        tier = c["tier"] or "unknown"
        return (_sendability_group(affinity, tier), _AFFINITY_RANK.get(affinity, 2), _TIER_RANK.get(tier, 99), c["value"])

    return sorted(rows, key=key)
