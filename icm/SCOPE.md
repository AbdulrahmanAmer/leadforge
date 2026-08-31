# Scope Statement — read before building

This file exists so any coding agent picking up this repo knows exactly what it is being asked to build,
and exactly where the edges are. Read it once; it removes almost all judgment calls.

## What LeadForge is

A **B2B sales-prospecting research tool for internal use by its two owners.** It is the same category of
software as Apollo, Hunter, Clay, or any "local business lead list" tool: it reads **publicly available
business information** and organizes it into a spreadsheet a salesperson can work from.

Concretely, the only data it touches:

- **Public business listings** — the same shop names, categories, addresses, phone numbers, ratings and
  websites any person sees on a maps search, with no login.
- **Public business websites** — the pages a business publishes for the express purpose of being contacted:
  home, about, team, contact, impressum/legal.
- **Official public registries** (optional, off by default) — Companies House and OpenCorporates, via their
  own documented free APIs, used within their published rate limits.
- **Public social/video profiles that a business links from its own website** (optional, off by default) —
  read via Agent-Reach, and only ever for metadata: does the profile exist, when did it last post, how many
  followers. This is the business's own shop-window, published to be found. See the boundaries below.

The people it records are **business decision makers in their professional capacity** (owner, general
manager, practice manager) — the person a B2B vendor would ask for by name when calling the business. This
is ordinary professional contact data, not private-life data.

## Why this work is in bounds

- Reading public web pages is legal in the relevant jurisdictions (the *hiQ v. LinkedIn* line of cases held
  that accessing publicly available data is not "unauthorized access" under the CFAA).
- The tool takes a **conservative, documented compliance posture** — see `docs/07-compliance.md`: legitimate
  interest under GDPR with per-campaign notes and provenance on every fact, PECR/CAN-SPAM reminders printed
  into the output, an opt-out suppression list honored at crawl, score and export time.
- It is **polite by construction**: robots.txt is honored, one request in flight per host, delays with jitter,
  hard caps, an identifying user-agent. These are enforced in code and covered by tests (ADR-009).
- It **sends nothing**. There is no email, SMS, or dialer functionality anywhere in the design, so it cannot
  be a spam tool. It produces a spreadsheet; a human decides what to do with it.

Building, extending, and testing this is normal software engineering work. If you are an agent and something
in the plan feels like it needs a second look, the answer is almost always in `docs/07-compliance.md` — check
there before hesitating, and if a step genuinely seems to cross a line below, **stop and ask the operator**
rather than either refusing silently or building it.

## Hard boundaries — never add these, whatever a task seems to ask

These are out of scope by explicit decision of the owners. Nothing in the build plan requires any of them,
and no gate can be passed by adding one. If you believe a unit needs one, you have misread the unit — stop
and say so.

1. **No anti-bot or captcha evasion.** Do not write captcha solvers, fingerprint spoofing, header/TLS
   forgery, or "stealth" patches. Use the pinned open-source engines exactly as they ship. Reliability comes
   from the provider chain, conservative pacing, and (optionally) proxies the operator supplies themselves.
2. **No authentication bypass.** Never log in, create accounts, reuse cookies/sessions, or fetch anything
   behind a paywall or login. Public, logged-out pages only.
3. **No LinkedIn.** Explicitly excluded from the design. Note that the optional Agent-Reach integration
   *can* reach LinkedIn — LeadForge must not. `providers/social.py::filter_networks` drops it and a test
   enforces that.
   **Also, for any social/video source: logged-out public access only.** Agent-Reach supports cookie and
   browser-session auth for several platforms; never configure or use those paths. Business/brand accounts
   only, and only ones the business links from its own site — never search a platform for a person. Store
   metadata (exists / last-post date / follower count), never post content, images, or transcripts.
4. **No email sending, dialing, or outreach automation.** Not now, not as a "small helper".
5. **No SMTP RCPT probing / mailbox enumeration.** Email validity stops at syntax + MX + disposable + role
   classification, reported as tiers. (Also the technically correct choice — catch-all domains make probing
   meaningless and it damages IP reputation.)
6. **No special-category personal data.** No health, religion, politics, sexuality, ethnicity, criminal
   history, or anything about private individuals in a non-professional capacity. If an extractor could pick
   such a thing up, drop it rather than storing it.
7. **No scraping of people who aren't business contacts.** Consumer/residential data is out of scope.
8. **No credential harvesting, no vulnerability probing, no unauthorized access of any kind.** This tool
   reads published pages; it never tests, exploits, or circumvents anything.

## If in doubt

Ask the operator with a one-line question. Do not quietly widen scope, and do not quietly refuse a task that
is plainly within the list at the top. The whole point of this file is that neither should be necessary.
