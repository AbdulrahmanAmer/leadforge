# Compliance & Politeness Guardrails

Practical posture for an **internal-use** lead research tool. Not legal advice; see cited sources in `docs/01-research.md` §6 and consult
counsel for jurisdictions you operate in.

## 1. What this tool does / does not do

| Does | Does not |
|---|---|
| Read **publicly accessible** business listings and websites | Log into anything, create accounts, or bypass auth walls |
| Use OSS scrapers' shipped politeness/consent handling | Build or add anti-bot evasion |
| Store business contact data with **source + timestamp** per fact | Scrape LinkedIn or any login-gated source |
| Respect robots.txt + rate limits on business websites | Dial, text, probe mailboxes, or send unattended (v0.3: email goes out only dry-run-by-default, human-approved, armed, capped — ADR-011) |
| Maintain a suppression list honored at crawl/score/export **and inside every send transaction** (v0.3) | Resell or publish the data (internal use only) |

## 2. Risk register

| Risk | Level | Mitigation |
|---|---|---|
| Google Maps ToS breach (contract, not criminal — public data per hiQ line) | Accepted, internal | Conservative defaults (`-c 2`, caps), no fake accounts, no redistribution of Google content; optional proxies only for reliability, not evasion escalation |
| GDPR (named work emails = personal data) | Managed | Legitimate-interest basis for B2B prospecting: keep a short **LIA note per campaign** (template below), provenance stored per contact, suppression honored immediately, `staleness_days` re-verification, data lives only in the operator's `leadforge_data/` |
| PECR/CAN-SPAM for email sent through the v0.3 outreach layer | **LeadForge-owned controls (ADR-011)** | Per-row `Entity Type` + `Lawful Basis (Email)` computed by `compliance.py` from the registry match and the address class; eligibility re-checked in the send transaction; identity + postal address + RFC 8058 one-click unsubscribe + Article-14 first-contact line rendered by the CLI, never by the model; bounces/complaints/unsubscribes/reply opt-outs write suppression rows automatically; per-mailbox caps, warm-up age and send window enforced; circuit breaker at 3% hard bounces / 0.1% complaints; `client_id` scoping so one client's opt-outs never leak into another's list. Phone-first by owner decision: email is the second touch. |
| PECR/CAN-SPAM for outreach the operator does by hand from the sheet | Operator's duty | Export Summary sheet embeds the region-profile reminder (US: opt-out + postal address + honest headers; UK: corporate-subscriber rule; EU: LIA + opt-out) |
| IP-reputation damage from SMTP probing | Avoided | No SMTP RCPT verification — tiers stop at MX/disposable/role analysis. The opt-in `validation.infer_emails` feature (v0.2.0) also never contacts a mail server: it derives a likely address from a real email already found on that domain + an MX record, exports it in a separate `Email (Inferred)` column labeled "likely", and excludes it from published-email coverage figures |
| Business-site overload | Avoided | robots.txt, 1 in-flight/host, 2 s+jitter delay, ≤ 6 pages/site, identifying UA |

## 3. Campaign LIA note (template — 4 lines, stored next to icp.yaml)

```
Purpose: find <category> businesses in <area> likely to need <offer>.
Necessity: minimal public business data (name, role contact, address); no special categories; no minors; B2B only.
Balancing: professional-context data, low intrusion; opt-out honored via suppression; data deleted/refreshed per staleness policy.
Retention: leadforge_data/ local only; purge on campaign end or objection.
```

## 4. Operator checklist (printed on the Summary sheet)

1. Outreach must identify you + postal address and include a working opt-out (all regions).
2. Add any opt-out/complaint immediately: `leadforge suppress add <email|domain>`.
3. UK: individuals/sole traders need consent — filter `Tier` + entity type before mailing UK lists.
4. Don't export `invalid`-tier emails (the exporter already drops them) and don't mail `catch_all/unknown` tiers blind.
5. Keep volumes sane; this is research tooling, not a spam cannon.
