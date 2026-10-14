# Decision-maker phone research and improvement loop

Status: active research log, started 2026-10-14

Objective: increase the number of real, named decision-maker phone contacts while keeping the default path
open-source/keyless, public-business-data-only, provenance-preserving, and honest about whether a number is
actually linked to the decision-maker.

The success metric is not raw phone count. The primary metric is:

```text
DM reach rate = calls that reached the named decision-maker / calls made to a supposed DM phone
```

Secondary metrics are named-DM coverage, person-linked phone coverage, direct/mobile coverage, wrong/disconnected
rate, gatekeeper-only rate, callback rate, and source/provider hit rate.

## Non-negotiable boundaries

- Use public business data and operator-supplied data only.
- Respect robots.txt, source terms, jurisdiction policy, rate limits, and the existing LeadForge crawl caps.
- No LinkedIn crawling, login-gated sources, anti-bot evasion, dialer, SMS, SMTP probing, or mailbox enumeration.
- A switchboard is not a DM phone. A company phone is only person-owned when the source provides a defensible
  association to the named person.
- A Companies House officer is a strong identity/title signal, not proof of a phone number. The official API
  exposes officer resources, but ordinary officer data should not be treated as a personal contact directory.

## Evidence-backed research rounds

### Round 1 — Companies House officer data

Finding: the official Companies House API exposes company officers and officer appointments. It is useful for
discovering names, roles, appointment state, and company identity, but it does not make personal phone numbers
available as a normal officer field.

Action: use Companies House to create/corroborate the DM identity and title, then look for a separately published
phone on the company’s official site or another permitted public source. Never synthesize a direct number from the
officer record.

Source: [Companies House API start guide](https://developer.company-information.service.gov.uk/get-started),
[officers API reference](https://developer-specs.company-information.service.gov.uk/companies-house-public-data-api/reference/officers).

### Round 2 — Schema.org Person

Finding: Schema.org `Person` explicitly supports `telephone`, `email`, `jobTitle`, `affiliation`, and `url`, and
the official examples show all of these fields together in JSON-LD and microdata.

Action: extend the structured-person extractor so a person card can emit person-owned phones and emails, not only
the name/title. Require the telephone/email to occur inside the same Person object/card before assigning ownership.

Source: [Schema.org Person](https://schema.org/Person).

### Round 3 — vCard/hCard

Finding: vCard provides a standard `TEL` property associated with a `FN` (formatted name), and hCard markup commonly
places `.fn`, `.tel`, `.email`, and `.title` in one contact card.

Action: parse `.tel`/`[itemprop=telephone]` and `.email`/`[itemprop=email]` inside the same card; retain TEL type
metadata (`cell`, `work`, `voice`) as evidence for phone classification, but do not overclaim `direct` without
context.

Source: [RFC 6350 vCard](https://datatracker.ietf.org/doc/html/rfc6350#section-6.2.4),
[Schema.org Person examples](https://schema.org/Person).

### Round 4 — public licensing registers

Finding: local-authority and sector licensing registers can name licence holders, designated premises supervisors,
managers, or other responsible people. They are useful for identity/title and business-to-person matching, but
often expose only business/premises contact routes, not direct phones.

Action: add source-specific adapters only for stable, permitted registers. Store the named licence holder as a
person observation and the premises phone separately unless the register explicitly links the phone to that person.

Sources: [Barnet premises licence register request](https://www.barnet.gov.uk/foi-requests/17973213),
[Blackpool housing licence public register](https://selfservice.blackpool.gov.uk/Landlords/housinglicensing/PublicRegister),
[Hinckley and Bosworth public registers](https://www.hinckley-bosworth.gov.uk/info/200063/licences_registrations_and_permits/1113/about_public_registers).

### Round 5 — environmental and trade-register datasets

Finding: the UK Environmental Public Registers provide downloadable business registrations and permit data. These
datasets are strong for regulated-business identity, location, and operating status, but are not generally direct-DM
phone directories.

Action: use them to improve business matching and discover responsible-person fields where available; do not label
the regulator or premises phone as a DM phone.

Source: [UK Environmental public registers](https://www.data.gov.uk/collections/environment/public-registers).

### Round 6 — public-register privacy/provenance boundary

Finding: Companies House states that its public register makes company information available, but personal details
can be suppressed in some circumstances. Public availability does not remove the need to record source, purpose,
jurisdiction, and field-level provenance.

Action: keep person observations tied to the exact source URL and jurisdiction; do not broaden a public register
identity into unrelated personal contact data.

Source: [Companies House personal-information guidance](https://www.gov.uk/guidance/your-personal-information-on-the-companies-house-register).

### Round 7 — licensed contact datasets

Finding: licensed UK business-contact datasets advertise telephone-researched senior decision-maker records and
verified contact details. They are not OSS, but they are a viable optional provider path if licensing, jurisdiction,
cost, and use restrictions are recorded.

Action: keep these behind the existing disabled-by-default paid adapter interface. Require provider-level cost,
verification state, source terms, and person ownership. Benchmark provider DM reach rate against the open-source path.

Source: [UK Business Contacts Plus Data listing](https://www.applytosupply.digitalmarketplace.service.gov.uk/g-cloud-14/services/552152090994615).

### Round 8 — WESCORE workbook evidence

Finding: Workbook 2 `List #2` and `List #3` contain 122 populated benchmark rows with named DMs and a
`DM Direct Phone / Best Route` field. They also contain 63 DM emails and titles. This is the strongest immediate
benchmark for the actual objective because it distinguishes the company phone from the DM route.

Action: import these fields as person-owned BYOL observations with workbook/tab/row provenance. Preserve the
disposition as an outcome label: reached DM, callback, gatekeeper, voicemail, no answer, wrong/disconnected, or
not interested.

Source: operator-supplied WESCORE workbooks; paths are intentionally not embedded in source code.

### Round 9 — directness is an association problem

Finding: increasing page count alone will mostly increase generic company phones. The breakthrough is to carry a
person identity through extraction and attach only same-card/same-structured-object/same-labeled-field phones.

Action: extend `PersonCandidate` with structured phones/emails; persist each value with `person_id`, phone type,
evidence, URL, and confidence. Treat unassociated page phones as business-level `main_line`/`unknown`.

### Round 10 — outcome feedback is the quality signal

Finding: `Voicemail` and `Gatekeeper` do not prove that a DM phone is wrong; `Wrong / Disconnected` does. `Callback`
and `Reached DM` are the strongest evidence that the route is useful. `Not Interested` is a successful contact but a
commercial outcome, not an enrichment failure.

Action: calculate source/provider/contact-type metrics separately for reachability and commercial outcome. Downgrade
wrong/disconnected numbers, preserve voicemail as reachable-but-unconfirmed, and promote sources that produce DM
callbacks/reaches.

## Iteration loop

For every implementation iteration:

1. Freeze a benchmark slice with company identity, named DM, title, phone route, source, and call outcome.
2. Run the pipeline on the same businesses without changing the benchmark labels.
3. Compare named-DM coverage, person-linked phones, direct/mobile phones, DM reach rate, gatekeeper rate, and wrong-number rate.
4. Inspect false positives by evidence shape: same card, same structured object, nearby label, business-only, or inferred.
5. Change one extractor/ranking/source adapter at a time.
6. Add a fixture and regression test for the discovered pattern.
7. Keep the change only when DM reachability improves or error rates fall without weakening compliance.
8. Record the result here with the next hypothesis.

## Current implementation backlog, ranked by expected DM-phone impact

1. **Structured person contact ownership** — Schema.org `telephone`/`email`, vCard/hCard TEL/email/title in one
   person object; highest immediate open-source impact.
2. **WESCORE DM-field import** — import `Decision Maker / Ask For`, `DM Title`, and `DM Direct Phone / Best Route`
   as person-owned observations and benchmark labels.
3. **Person-adjacent phone extraction** — same DOM card, heading-to-phone window, `tel:` links, extension labels,
   and work/mobile TEL types.
4. **Public-registry identity cross-check** — registry officers/PSC names and titles corroborate the website person,
   but never provide an unproven phone.
5. **Trade-license adapters** — regulated-sector named responsible persons and permitted contact fields.
6. **Press/speaker/exhibitor source selection** — public professional contact pages with explicit person association.
7. **Optional licensed provider adapters** — only after open-source benchmark metrics establish the gap and provider
   cost/terms are recorded.
8. **Outcome-driven source ranking** — learn from DM reached/callback vs gatekeeper/wrong outcomes.

## Breakthrough criteria

A breakthrough is verified only if a fixed benchmark rerun demonstrates a meaningful improvement in DM-linked direct
or mobile coverage and/or DM reach rate without increasing wrong/disconnected or unsupported-person assignments.
Until that happens, this document remains active and the next iteration must be evidence-driven rather than a
speculative source expansion.

### Round 11 — structured contact ownership implemented

Change: `PersonCandidate` now carries zero or more phones and emails. Schema.org Person JSON-LD reads
`telephone`/`email`; vCards and Schema.org microdata read same-card TEL/email values. Values are deduplicated
within the person card, normalized before persistence, and written as legacy contacts plus field observations with
`person_id`, URL, evidence, provider, confidence, and freshness scheduling.

Validation: 79 focused extraction/runner/foundation tests passed; Ruff passed. Existing text-based candidate
constructors remain compatible because the new fields default to empty tuples.

Result: the pipeline can now prove that a published phone/email belongs to a named person when the site publishes
that relationship in structured markup. It still does not claim ownership for generic page phones, and it does not
turn registry identity into a phone.

Next hypothesis: the operator workbooks' `DM Direct Phone / Best Route` rows can provide a high-value, human-called
benchmark and BYOL calibration set. Import them conservatively as person-owned observations, keeping workbook
provenance separate from public-source observations and using dispositions to measure actual DM reachability.

### Round 12 — operator benchmark importer implemented

Change: added `leadforge benchmark dm-fields`, preview-only by default. It reads the existing CSV/TSV/XLSX
workbook reader, recognizes `DM Title`, `DM Direct Phone / Best Route`, and DM email fields, and writes only
uniquely matched businesses. Each imported person/contact observation is labeled `human_calling:<sheet>` with
provider `human_calling`, workbook URL/sheet evidence, person ownership, and jurisdiction. Existing OSS values are
not overwritten; repeated imports are deduplicated.

Validation: benchmark and foundation tests passed; the real supplied workbooks parse to 122 DM-route rows in
`List #2`/`List #3` of Workbook 2. The importer is not run against the repository database automatically.

### Round 13 — common team-card coverage

Research/action: public sites frequently publish people in CSS team/staff/employee cards without full Schema.org
markup. The structured extractor now accepts conservative card selectors (`team-member`, `staff-member`,
`employee`, and Schema.org/vCard cards), takes names from headings, titles from role/position fields, and only
associates phones/emails found within that same card. A fixture covers the common heading + role + tel/mailto shape.

Guardrail: generic page-level phones remain business-level observations; no nearest-text association was added.
This preserves precision while increasing person-linked direct/mobile coverage where the site explicitly groups the
contact details.

### Round 14 — regression validation and merge safety

The first full serial run exposed a real initialization-order bug in the duplicate-markup merge path. It was fixed
immediately and the changed-area suite now passes 84 tests with Ruff clean. The prior full run reached 779 passing and
1 skipped before that single new-test failure; no existing test failed.

Lesson: every recall improvement that merges evidence must have both a first-observation fixture and a duplicate-
representation fixture. This is now part of the acceptance gate before adding more source adapters.

### Round 15 — export was hiding the DM route

Audit finding: the data model already allowed person-owned phone observations, but `_row_for` selected the business
phone for the exported `Phone` column. A direct DM route could therefore be discovered correctly and still be hidden
from the caller by the switchboard.

Change: exports now include `DM Phone`, `DM Phone Type`, and `DM Phone Source`. Selection is restricted to the
accepted DM's own phone observations and ranks verified/observed state, direct/mobile type, provider, and confidence.
The original `Phone` column remains the business-level number for backward compatibility.

Metrics now distinguish `accepted_dms_with_person_phone`, `accepted_dms_with_direct_or_mobile`, and
`accepted_dms_with_person_email` from generic business contact coverage.

Validation: 23 export/foundation tests passed with Ruff clean. This is a direct accuracy improvement because the
calling artifact now exposes the DM route instead of only the company route; the next benchmark rerun should measure
whether this raises DM reach rate without changing extraction.

### Round 16 — outcome attribution distinguishes DM routes

Change: when a calling workbook row contains `DM Direct Phone / Best Route`, outcome import now defaults its
`contact_type` to `dm_direct`; ordinary phone rows remain `phone`. This lets source/provider attribution compare
actual DM-route outcomes against switchboard outcomes instead of mixing them.

Validation: 28 benchmark/export/foundation tests passed with Ruff and diff checks clean. This does not infer that a
callback reached the DM; it records the route the operator dialed, leaving the disposition as the independent result.

### Round 17 — public event/press page discovery

Change: the polite same-host crawler now treats press, media, news, speaker, conference, exhibitor, and event links
as public enrichment candidates, with high priority alongside about/team/contact pages. This increases the chance of
reaching publicly published professional biographies before the page cap is exhausted.

Validation: an offline crawler fixture proves speaker, press, and event links are selected. No external host,
login-gated page, LinkedIn page, or search-engine scraping was added.

### Round 18 — nested event Person ownership

Research: official event structured-data guidance permits `performer` as a Person and `organizer` as a Person;
Schema.org Person defines telephone/email/jobTitle fields. The extractor now follows only those explicit Event roles
and carries their same-object phones/emails. It intentionally does not recursively follow review authors or arbitrary
Article authors, which would create false DM candidates.

Validation: 63 extraction/crawler/foundation tests passed with Ruff clean. The expected value is higher recall on
public speaker/organizer pages while retaining an explicit person-to-contact association.
