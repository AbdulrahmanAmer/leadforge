"""Sending-identity health checks (v0.3 unit E, docs/09 Wave 2 E #7).

SPF, DKIM, DMARC, MX and the identity/mailbox warm-up posture. Every check FAILS CLOSED: a DNS
timeout, NXDOMAIN, or missing record reports FAIL, never "assumed ok". Uses `dnspython` (already a
core dependency) via the module-level `dns.resolver` import so tests can monkeypatch
`dns.resolver.resolve` directly.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import dns.resolver

from leadforge.outreach.identity import (
    env_config,
    get_identity,
    is_identity_complete,
    mailboxes_for_identity,
)
from leadforge.util import LeadForgeError

WARMUP_MIN_DAYS = 21
_DEFAULT_DKIM_SELECTORS = ("default", "google")


@dataclass
class CheckResult:
    name: str
    ok: bool
    hint: str = ""


def _txt_strings(domain: str) -> list[str]:
    answers = dns.resolver.resolve(domain, "TXT")  # raises on NXDOMAIN/timeout — caller catches
    out = []
    for rec in answers:
        if hasattr(rec, "strings"):
            out.append(b"".join(rec.strings).decode("utf-8", "replace"))
        else:
            out.append(str(rec).strip('"'))
    return out


def check_spf(domain: str) -> CheckResult:
    try:
        txts = _txt_strings(domain)
    except Exception as e:  # noqa: BLE001 — any DNS trouble fails this check closed
        return CheckResult("SPF", False, f"TXT lookup failed for {domain}: {e}")
    for txt in txts:
        if "v=spf1" in txt and txt.strip().endswith("-all"):
            return CheckResult("SPF", True)
    return CheckResult("SPF", False, f"no v=spf1 ... -all TXT record at {domain}")


def check_dkim(domain: str, selector: str | None) -> CheckResult:
    candidates = [selector] if selector else list(_DEFAULT_DKIM_SELECTORS)
    errors = []
    for sel in candidates:
        host = f"{sel}._domainkey.{domain}"
        try:
            txts = _txt_strings(host)
            if txts:
                return CheckResult("DKIM", True, f"selector={sel}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{host}: {e}")
    return CheckResult("DKIM", False, f"no DKIM TXT found (tried {candidates}); {'; '.join(errors)[:150]}")


def check_dmarc(domain: str) -> CheckResult:
    host = f"_dmarc.{domain}"
    try:
        txts = _txt_strings(host)
    except Exception as e:  # noqa: BLE001
        return CheckResult("DMARC", False, f"TXT lookup failed for {host}: {e}")
    for txt in txts:
        if "p=quarantine" in txt or "p=reject" in txt:
            return CheckResult("DMARC", True)
    return CheckResult("DMARC", False, f"no p=quarantine|reject DMARC record at {host}")


def check_mx(domain: str) -> CheckResult:
    try:
        answers = dns.resolver.resolve(domain, "MX")
    except Exception as e:  # noqa: BLE001
        return CheckResult("MX", False, f"MX lookup failed for {domain}: {e}")
    return CheckResult("MX", bool(list(answers)), "" if list(answers) else f"no MX records at {domain}")


def _warmup_days(warmup_started_at: str | None) -> float:
    if not warmup_started_at:
        return -1.0
    try:
        started = datetime.strptime(warmup_started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return -1.0
    return (datetime.now(UTC) - started).total_seconds() / 86400.0


def run_doctor(conn: sqlite3.Connection, identity_label: str) -> list[CheckResult]:
    """All checks for one identity, fail-closed. Raises LeadForgeError if the identity is unknown."""
    identity = get_identity(conn, identity_label)
    if identity is None:
        raise LeadForgeError(f"unknown identity '{identity_label}'")

    from_domain = identity["from_email"].rsplit("@", 1)[-1] if "@" in (identity["from_email"] or "") else ""
    reply_email = identity["reply_to"] or identity["from_email"] or ""
    reply_domain = reply_email.rsplit("@", 1)[-1] if "@" in reply_email else ""

    results: list[CheckResult] = []
    if not from_domain:
        results.append(CheckResult("SPF", False, "identity has no from_email domain"))
        results.append(CheckResult("DKIM", False, "identity has no from_email domain"))
        results.append(CheckResult("DMARC", False, "identity has no from_email domain"))
    else:
        results.append(check_spf(from_domain))
        mailboxes = mailboxes_for_identity(conn, identity["id"])
        selector = None
        for mb in mailboxes:
            selector = env_config(mb).get("dkim_selector") or selector
        results.append(check_dkim(from_domain, selector))
        results.append(check_dmarc(from_domain))

    results.append(check_mx(reply_domain) if reply_domain else CheckResult("MX", False, "no reply-to domain"))
    results.append(CheckResult("identity_complete", is_identity_complete(identity),
                               "" if is_identity_complete(identity) else "missing from_name/postal_address/"
                               "privacy_url/unsubscribe"))

    mailboxes = mailboxes_for_identity(conn, identity["id"])
    if not mailboxes:
        results.append(CheckResult("warmup", False, "no mailboxes registered for this identity"))
    for mb in mailboxes:
        age = _warmup_days(mb["warmup_started_at"])
        ok = age >= WARMUP_MIN_DAYS
        hint = "" if ok else f"{mb['address']}: warm-up age {age:.1f}d < {WARMUP_MIN_DAYS}d"
        results.append(CheckResult(f"warmup:{mb['address']}", ok, hint))

    return results
