# Enrichment Phase 0 baseline

Status: 2026-10-14. This document records the baseline before behavior changes.

## Current behavior

- The default path is OSS-only and keyless: Maps/discovery, public-site crawling, static extraction,
  optional browser escalation, MX validation, inferred email (opt-in), public registry adapters (key-gated),
  and GBP facts already present in discovery data.
- A business can already have many `people` rows and many `contacts` rows. Contact values are unique per
  business/kind/value; people are unique per business/name/title.
- Provenance was fragmented across `contacts.meta_json`, `contacts.affinity`, `people.origin`, `evidence`,
  `businesses.enrich_json`, and provider-specific profile/GBP payloads; new writes now also project
  public-site contacts and people into field-level observations, while legacy tables remain authoritative.
- Email policy already forbids SMTP probing and foreign/unlinked addresses are not treated as eligible.
  LinkedIn is excluded from social handling.

## Exact gaps

| Area | Baseline gap | Phase 0 foundation |
|---|---|---|
| Unified provenance | No field-level value/source/confidence/provider/cost/jurisdiction object | `EnrichmentObservation` + `enrichment_observations` table |
| Freshness | Crawl timestamps are business-level; no field decay or recheck queue | observation `observed_at`, `verified_at`, `fresh_until`, `recheck_at` |
| Person ownership | Contacts/evidence are only indirectly associated with a person | observation `person_id`/`person_name` |
| Outreach attribution | `outcomes` had no source/provider/contact-type columns | nullable additive attribution columns + extended `add_outcome` |
| Measurement | No reusable coverage/source-hit/cost metrics | read-only `enrich.metrics` functions |
| Phone typing | Phones were stored as generic `Contact(kind='phone')` | `switchboard|main_line|direct|mobile|unknown` classification and observation projection |
| Email waterfall | Published vs inferred was partly represented by labels/tiers | explicit contact `email_stage` plus published→role→inferred labels; licensed adapters remain opt-in |
| Coverage expansion | Team/about and structured people were not explicit | Schema.org Person, vCard, and structured Person extraction is additive; registry/GBP paths retain existing guards |
| Paid providers | No unified opt-in adapter contract | adapter protocol/registry; no built-in provider and disabled by default |
| BYOL | No conservative CSV import path | dry-run-first CSV importer with unique ID/domain/phone/exact name+locality matching |
| Compliance | Region profile was coarse (`us|uk|eu`) and provenance was not field-level | conservative US/GB/EU policy profiles plus observation jurisdiction/provenance |
| Benchmark | No hand-checked outreach benchmark workflow | CSV/TSV/XLSX importer, WESCORE workbook tab mapping, canonical dispositions, attribution and idempotent re-import |

## Measurement contract

`leadforge.enrich.metrics` is intentionally read-only and reports coverage counts, observation source/provider
hit rate, observation cost per verified record, and outreach outcome attribution. Existing rows continue to be
valid; empty observation/attribution tables report zero/empty metrics until a producer opts in.

## Next implementation order

1. Run `leadforge enrich --stage backfill` once per existing workspace, then use `--stage recheck` to inspect
   field-level freshness deadlines before a bounded site pass.
2. Extend page-source coverage incrementally (press/speaker and trade-license adapters) behind source-specific tests;
   do not add SMTP probing, LinkedIn crawling, or login-gated access.
3. Use the supplied WESCORE workbooks as a called benchmark; import with preview first, then `--apply` only after
   the strong-match report is reviewed.
4. Add paid-provider implementations only as separately tested, explicitly configured adapters.
