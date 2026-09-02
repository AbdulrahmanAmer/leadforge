"""U9E#7 — `outreach doctor`: SPF/DKIM/DMARC/MX, identity completeness, warm-up. Fails closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import dns.resolver
import pytest

from leadforge.outreach import doctor as doctor_mod
from leadforge.util import LeadForgeError
from tests.outreach_helpers import make_identity, make_mailbox


class _TxtRecord:
    def __init__(self, text: str):
        self.strings = [text.encode("utf-8")]


def _resolver_answering(records: dict[tuple[str, str], list[str]]):
    """Fake `dns.resolver.resolve` keyed on (name, rtype); a missing key raises like a real NXDOMAIN."""

    def _resolve(name, rtype):
        key = (name, rtype)
        if key not in records:
            raise dns.resolver.NXDOMAIN(f"no such record: {name} {rtype}")
        if rtype == "TXT":
            return [_TxtRecord(t) for t in records[key]]
        return records[key]  # MX: any truthy iterable is enough for check_mx's bool(list(...))

    return _resolve


def _setup_identity(conn, warmup_days=30):
    make_identity(conn, label="doc1", from_email="sales@gooddomain.com", from_name="Sales",
                 postal_address="1 St", privacy_url="https://gooddomain.com/privacy",
                 unsubscribe_mailto="unsub@gooddomain.com")
    warmup_at = (datetime.now(UTC) - timedelta(days=warmup_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    make_mailbox(conn, identity_label="doc1", address="sales@gooddomain.com", warmup_started_at=warmup_at)


def test_all_checks_pass_with_full_records(conn, monkeypatch):
    _setup_identity(conn, warmup_days=30)
    records = {
        ("gooddomain.com", "TXT"): ["v=spf1 include:_spf.google.com -all"],
        ("default._domainkey.gooddomain.com", "TXT"): ["v=DKIM1; k=rsa; p=xxx"],
        ("_dmarc.gooddomain.com", "TXT"): ["v=DMARC1; p=reject"],
        ("gooddomain.com", "MX"): [object()],
    }
    monkeypatch.setattr(dns.resolver, "resolve", _resolver_answering(records))
    results = doctor_mod.run_doctor(conn, "doc1")
    assert all(r.ok for r in results)


def test_missing_dmarc_fails_closed(conn, monkeypatch):
    _setup_identity(conn, warmup_days=30)
    records = {
        ("gooddomain.com", "TXT"): ["v=spf1 include:_spf.google.com -all"],
        ("default._domainkey.gooddomain.com", "TXT"): ["v=DKIM1; k=rsa; p=xxx"],
        # no _dmarc record at all
        ("gooddomain.com", "MX"): [object()],
    }
    monkeypatch.setattr(dns.resolver, "resolve", _resolver_answering(records))
    results = doctor_mod.run_doctor(conn, "doc1")
    dmarc = next(r for r in results if r.name == "DMARC")
    assert not dmarc.ok
    assert not all(r.ok for r in results)


def test_dns_timeout_fails_closed_not_optimistic(conn, monkeypatch):
    _setup_identity(conn, warmup_days=30)

    def _raise(*a, **kw):
        raise dns.resolver.Timeout()

    monkeypatch.setattr(dns.resolver, "resolve", _raise)
    results = doctor_mod.run_doctor(conn, "doc1")
    assert not any(r.ok for r in results if r.name in ("SPF", "DKIM", "DMARC", "MX"))


def test_spf_without_hardfail_suffix_fails(conn, monkeypatch):
    _setup_identity(conn, warmup_days=30)
    monkeypatch.setattr(dns.resolver, "resolve", _resolver_answering({
        ("gooddomain.com", "TXT"): ["v=spf1 include:_spf.google.com ~all"],  # softfail, not -all
    }))
    spf = next(r for r in doctor_mod.run_doctor(conn, "doc1") if r.name == "SPF")
    assert not spf.ok


def test_warmup_under_21_days_fails(conn, monkeypatch):
    _setup_identity(conn, warmup_days=5)
    monkeypatch.setattr(dns.resolver, "resolve", _resolver_answering({}))
    results = doctor_mod.run_doctor(conn, "doc1")
    warmup = next(r for r in results if r.name.startswith("warmup:"))
    assert not warmup.ok


def test_unknown_identity_raises(conn):
    with pytest.raises(LeadForgeError):
        doctor_mod.run_doctor(conn, "ghost")


# ---------------------------------------------------------------------------------------- watched-fail
#   test_missing_dmarc_fails_closed: check_dmarc's `"p=quarantine" in txt or "p=reject" in txt` guard
#     temporarily replaced with `True` (any TXT record passes) -> the missing-record NXDOMAIN path
#     still correctly failed (dmarc.ok stayed False) as expected, so instead the mutation was applied
#     the OTHER way: the CheckResult constructed on the except branch temporarily set ok=True -> the
#     `not dmarc.ok` assertion failed -> red for the right reason. Restored.
#   test_dns_timeout_fails_closed_not_optimistic: the `except Exception` catch in check_spf/dkim/dmarc/
#     mx temporarily changed to construct CheckResult(..., True, ...) on failure (fail OPEN) -> every
#     check reported ok -> red for the right reason (this is exactly the invariant the test exists to
#     guard). Restored.
#   test_warmup_under_21_days_fails: WARMUP_MIN_DAYS temporarily set to 0 -> the 5-day-old mailbox
#     passed -> red for the right reason. Restored.
