---
name: generate-leads
description: Generate scored B2B lead lists from open-source scraping. Use when the user wants to find leads, prospects, potential clients or customers, build a lead list, find businesses to pitch (e.g. "find auto repair shops in Houston for my web agency", "B2B leads for my SaaS", "who should my real-estate services target"), or asks to run LeadForge. Interviews the user about their ideal customer profile, runs the self-bootstrapping leadforge CLI pipeline (Google Maps scraping, website deep-crawl, decision-maker discovery, ICP scoring), and delivers a scored XLSX/CSV lead sheet.
license: MIT
compatibility: Requires shell access, Python 3.11+, and network. Windows, macOS, Linux.
metadata:
  version: "0.3.0"
  author: leadforge
---

# Generate B2B Leads

You orchestrate a Python CLI that does ALL scraping, crawling, parsing, storage, scoring and exporting. Your job is judgment, not labor:
interview → launch → adjudicate decision makers → deliver.

## Iron rules (token contract — full version: `references/cli.md`)

1. Every `leadforge` command ends with one `LF_DIGEST {json}` line. **That line is the result.** Long stages also stream bounded `LF_PROGRESS {json}` lines — use them to report progress to the user ("12/30 queries done") or ignore them; never treat them as the result. Prefer `--json` flags.
2. **Never** read files under `leadforge_data/` (db/cache/exports/logs) — unbounded. The ONE exception: the small NDJSON batch produced by
   `leadforge dm export`.
3. Never scrape/crawl/search the web yourself for lead data. If the pipeline can't get something, it says so in the digest.
4. On `ok:false`: follow the digest `warnings` + `next`, check `references/troubleshooting.md`, retry once, then report to the user.

## Step 0 — Locate & bootstrap

Run from the repo/workspace directory the user chose (any folder works; state lives in `./leadforge_data/`).

```bash
leadforge doctor --fix --full --json  # first-time setup: deps, scraper binary, quality extras (NER, browser, yt-dlp); safe to re-run
```

If `leadforge` isn't on PATH: `pip install -e <repo-root>` first (repo-root = directory containing `pyproject.toml`; on install failure
see troubleshooting).

## Step 1 — Interview the user (before any scraping)

Ask conversationally (batch related questions; skip what the user already stated). Full question bank + field semantics:
`references/icp-guide.md`. You MUST end up knowing:

1. **Offer** — what they sell + the one-line value prop (drives "likely need" hooks).
2. **Target category(ies)** — business types to find (e.g. auto repair shops, boutique realtors, dental clinics).
3. **Geography — city AND country (never skip the country).** Ask for the country explicitly (ISO2: US, GB,
   EG, AE…) and push for a specific area. A bare city name is ambiguous worldwide — there is a Houston in the
   US and in the UK, dozens of Springfields — and guessing wrong silently fills the whole sheet with garbage.
   In federal countries (US/CA/AU/BR/IN/MX) also get the state/region: "Houston, TX", not "Houston". If the
   user says something vague ("the Gulf area", "downtown"), ask which city/cities before proceeding.
4. **Size band** — e.g. min reviews, solo vs multi-location, employees if known.
5. **Hard disqualifiers** — franchises? no-phone? competitors to exclude?
6. **Soft qualifications** — what makes a lead GOOD for them (these become scoring signals; e.g. "no website yet" is GOLD for a web
   agency).
7. **Decision-maker titles** — who they want to reach (Owner, GM, Marketing Manager, …).
8. **Volume cap** — how many leads they want (default 200).
9. **Profile** — ordinary lead-gen uses the default scoring; a prospecting-engine brief (account rubric,
   grades, manual-review statuses) uses `scoring: { profile: account_fit }` — see `references/icp-guide.md`.

**UK campaigns only — one optional extra question.** If the country is GB and `leadforge config get
registry.companies_house_key` is empty, ask ONCE: "Want registry-verified company directors on the sheet?
It needs a free Companies House API key (2-minute signup at
https://developer.company-information.service.gov.uk — create an application, copy the key)." If they paste
one: `leadforge config set registry.companies_house_key <KEY>`. If they decline or hesitate, proceed
without it — never block the campaign on this.

Write the answers to `answers.yaml` (schema in `references/icp-guide.md`), then:

```bash
leadforge intake --answers answers.yaml --json     # → writes icp.yaml, validates hard
```

Fix any field errors it reports by asking the user, not by guessing.

## Step 2 — Run the pipeline

```bash
leadforge plan --icp icp.yaml --json    # optional sanity: tiles/queries/estimates — confirm with user if queries > 80
leadforge run  --icp icp.yaml --json    # discover → enrich → validate; pauses at stage=dm_pending
```

Long runs are normal (minutes to an hour). The command streams nothing noisy; wait for the digest. If it ends `stage=dm_pending`,
continue to Step 3. If `stage=exported` (no DM candidates found), skip to Step 4.

## Step 3 — Decision-maker labeling (your judgment call)

```bash
leadforge dm export --max 60 --json     # digest contains the batch file path
```

Read the batch file. Each line: one business, its ICP-priority titles, and candidate `{name,title,snippet}` entries. Label **from the
snippets only** — do not browse websites. Protocol + examples: `references/dm-labeling.md`. Write `dm_labels.ndjson` with one line per
business: `{"biz":"<id>","pick":<candidate index or -1>,"confidence":0.0-1.0,"title_override":null|"..."}`.

```bash
leadforge dm apply --in dm_labels.ndjson --json
leadforge run --icp icp.yaml --resume --json      # scores + exports
```

Repeat `dm export` if the digest says more batches remain.

## Step 4 — Deliver

The final digest lists artifact paths (XLSX, CSV, report.json). Report to the user, briefly:

- counts + tier split (A/B/C), e.g. "142 leads — 38 A, 71 B, 33 C";
- top 2–3 "likely need" hooks with counts (from the digest, e.g. "61 have no online booking");
- the sheet path, and that Summary/About tabs explain scores + compliance reminders;
- any degradations worth knowing (e.g. "9 sites need a browser pass — install extras to cover them: `pip install -e .[browser]`").

Do NOT paste sheet contents (≤ 5 rows only if the user explicitly asks for a preview).

## Step 5 — Outreach (optional, v0.3; only when the user asks to contact leads)

The sheet's **Next Action** column is phone-first by owner decision: call rows with a validated phone,
email is the second touch. Email goes out only through `leadforge outreach`, which is **dry-run by default
and cannot send until the workspace owner arms it**. Protocol + digest fields: `references/outreach.md`
(sending) and `references/drafting.md` (how you write the two model slots of each message).

```bash
leadforge outreach identity add --label gainlev --from-email you@sending-domain --from-name "Name" \
    --postal-address "..." --privacy-url https://... --unsubscribe-mailto stop@sending-domain --json
leadforge outreach mailbox add --identity gainlev --address you@sending-domain --transport smtp \
    --config host_env=LF_SMTP_HOST --config port_env=LF_SMTP_PORT --config user_env=LF_SMTP_USER \
    --config password_env=LF_SMTP_PASS --json            # values are env var NAMES, never secrets
leadforge outreach plan --campaign <campaign> --tier A,B --identity gainlev --json   # enrol eligible leads
leadforge draft export --campaign <campaign> --purpose client_campaign --max 40 --json
#   read the packet file; write drafts.ndjson: {"target", "subject", "observation", "used_fact"} or {"target","abstain":true}
leadforge draft apply --in drafts.ndjson --json          # mechanical no-fabrication gate; rejected drafts are counted
leadforge draft render --campaign <campaign> --out drafts/ --json   # files for the human to read
leadforge outreach approve --campaign <campaign> --all-drafted --approver "<human name>" --json
leadforge outreach send --campaign <campaign> --json     # DRY RUN: writes .eml files to the outbox, sends nothing
leadforge outreach doctor --identity gainlev --json      # SPF / DKIM / DMARC / MX / warm-up must all be ok
```

Rules you must not bend: never run `--live` yourself — the owner sets `outreach.armed: true` and passes
`--i-am <approver>`; never edit a message after approval (it reverts to drafted); never invent a fact in a
draft (the gate rejects it, and the packet is the only source of truth); abstain on grade-C packets when the
purpose needs personalisation. After a calling or mailing sprint, record results with
`leadforge outreach outcome add --business <id> --channel phone|email --result <..>` so the next scoring pass
learns from real outcomes.

## Repeat campaigns

Same workspace = same SQLite: re-runs dedupe automatically and re-verify stale rows. For a new campaign, just run Step 1 with a new
`answers.yaml` (new icp/campaign name). `leadforge status --json` shows the latest run any time. To honor an opt-out immediately:
`leadforge suppress add <email-or-domain>`.
