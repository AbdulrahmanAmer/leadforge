"""Contact validation -> tiers, never booleans (U4.3) — docs/04 §3.3.

Email tier ladder: syntax (email-validator) -> MX (dnspython, cached) -> disposable -> role classification.
No SMTP RCPT probing (ADR: catch-alls lie + IP-reputation risk). Phone validity via phonenumbers.
"""

from __future__ import annotations

from functools import lru_cache

from leadforge.config import Config

try:
    from disposable_email_domains import blocklist as _DISPOSABLE
except Exception:  # noqa: BLE001 — optional dataset; absence just skips the disposable tier
    _DISPOSABLE = set()


def _mx_exists(domain: str, timeout: float) -> bool | None:
    """True/False if resolvable; None on DNS error (-> tier 'unknown', retryable)."""
    try:
        import dns.resolver

        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
            return len(answers) > 0
        except dns.resolver.NoAnswer:
            a = dns.resolver.resolve(domain, "A", lifetime=timeout)  # some domains accept mail on A
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


def best_email_tier(tiers: list[str]) -> str:
    order = ["valid", "role", "risky", "catch_all", "unknown", "invalid"]
    present = [t for t in order if t in tiers]
    return present[0] if present else "unknown"
