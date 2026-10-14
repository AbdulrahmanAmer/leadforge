---
name: generate-leads
description: Generate scored B2B lead lists from open-source scraping. Use when the user wants to find leads, prospects, potential clients or customers, build a lead list, find businesses to pitch (e.g. "find auto repair shops in Houston for my web agency", "B2B leads for my SaaS", "who should my real-estate services target"), or asks to run LeadForge. Interviews the user about their ideal customer profile, launches the self-bootstrapping leadforge CLI pipeline in the background (Google Maps scraping, website deep-crawl, decision-maker discovery, ICP scoring), polls it to completion, and delivers a scored XLSX/CSV lead sheet.
license: MIT
compatibility: Requires shell access, Python 3.11+, and network. Windows, macOS, Linux.
metadata:
  version: "0.6.0"
  author: leadforge
---

# Generate B2B Leads

You orchestrate a Python CLI that does ALL scraping, crawling, parsing, storage, scoring and exporting. Your job is
judgment, not labor: interview → plan → launch (detached) → poll → adjudicate decision makers → deliver. Runs take
minutes to hours — **never wait on `run` in the foreground**; it outlives a single tool call. For email outreach
after the sheet exists, hand off to the separate `send-outreach` skill — it is not part of this one.

## Iron rules (token contract — full version: `references/cli.md`)

1. Every `leadforge` command ends with one `LF_DIGEST {json}` line. **That line is the result.** Long stages also
   stream bounded `LF_PROGRESS {json}` lines — read them for progress, never as the result. Always pass `--json`.
2. **Never** read files under `leadforge_data/` (db/cache/exports/logs) — unbounded. The ONE exception: the small
   NDJSON batch produced by `leadforge dm export`.
3. Never scrape/crawl/search the web yourself for lead data. If the pipeline can't get something, it says so in the digest.
4. On `ok:false`: follow the digest `warnings` + `next`, check `references/troubleshooting.md`, retry once, then report.

## Step 0 — Locate, bootstrap, resume-check

Run from the workspace directory the user chose (any folder works; state lives in `./leadforge_data/`). The `leadforge`
command is `${CLAUDE_PLUGIN_ROOT}/bin/leadforge` when installed as a plugin (it self-installs into an isolated venv on
first use — no repo checkout needed); a plain pip/editable install just has `leadforge` on PATH.

```bash
leadforge status --json          # is there already a run for this workspace? (see "Resuming a run" below)
leadforge doctor --json          # light check: python/deps/scraper binary presence — no downloads
```

`doctor` is **light by default**. Only run `doctor --fix --json` (installs missing core deps + the scraper binary) when
`doctor` reports a problem, and only run `doctor --fix --full --json` (adds NER + browser extras, several hundred MB,
slow on a fresh machine) **after telling the user the approximate download size and getting a yes** — see
`references/troubleshooting.md` for what each extra buys and costs.

## Step 1 — Interview the user (before any scraping)

Ask conversationally (batch related questions; skip what the user already stated). Full question bank + field
semantics: `references/icp-guide.md`. You MUST end up knowing:

1. **Offer** — what they sell + the one-line value prop.
2. **Target category(ies)** — business types to find.
3. **Geography — city AND country (never skip the country).** ISO2 country (US, GB, EG, AE…); a bare city name is
   ambiguous worldwide. In federal countries (US/CA/AU/BR/IN/MX) also get the state/region.
4. **Size band**, **hard disqualifiers**, **soft qualifications** (these become scoring signals).
5. **Decision-maker titles** they want to reach. **Volume cap** (default 200).
6. **Profile** — default scoring, or `scoring: { profile: account_fit }` for a prospecting-engine brief.

**UK campaigns only.** If country is GB and `leadforge config get registry.companies_house_key` is empty, ask ONCE
about a free Companies House API key (`references/icp-guide.md`); proceed without it if declined.

Write `answers.yaml`, then: `leadforge intake --answers answers.yaml --json` (fix field errors by asking the user).

## Step 2 — Plan, state the estimate, get consent above the threshold

```bash
leadforge plan --icp icp.yaml --json   # mandatory — never skip straight to run
```

The digest's `counts` carry both estimates: `est_runtime_min` (discovery) and `est_enrich_min` (crawl/enrich), summed
in `est_total_min`, plus `est_max_results` / `est_enrich_sites`. State the total to the user in plain terms ("roughly
N businesses, about H hours end to end"). **Get an explicit yes before `run` when `est_total_min > 30` or the plan has
more than 80 queries** — otherwise proceed straight to Step 3.

## Step 3 — Launch detached, then poll — never block

```bash
leadforge run --icp icp.yaml --detach --json   # returns immediately: run id, pid, log path
```

Poll, spacing calls out (the digest gives an ETA once the current stage has a measured pace):
`leadforge status --run <run_id> --json`. Report progress between polls using `progress_stage` /
`progress_done` / `progress_total` / `eta_seconds` when present.
**Autopilot handles decision-maker labeling and drafting itself** (through your own
headless Claude Code where available, deterministic fallbacks otherwise) — a normal run ends `stage=exported` in
`status`; go straight to Step 5. Only do Step 4 when `status` shows `dm_unlabeled > 0`, or the workspace has
`--no-autopilot` set (`status` shows `stage=dm_pending`).

### Resuming a run found by `status`

If Step 0's `status --json` shows a run that is not `exported` and not `alive` (the detached process died or the
previous session ended), resume it (detached, then poll as in Step 3) instead of starting a new one:

```bash
leadforge run --icp icp.yaml --resume --detach --json                       # continue from wherever it stopped
leadforge run --icp icp.yaml --resume --from-stage <stage> --detach --json  # force a stage first, if status says why
```

Never re-run `leadforge intake` or start a fresh `run` for a workspace with a live or resumable run — check `status` first.

## Step 4 — Decision-maker labeling (only if `dm_unlabeled > 0`, or autopilot is off)

```bash
leadforge dm export --max 60 --json     # digest contains the batch file path
```

Read the batch file. Each line: one business, its ICP-priority titles, and candidate `{name,title,snippet}` entries.
Label **from the snippets only** — never browse websites. Protocol + examples: `references/dm-labeling.md`. Write
`dm_labels.ndjson`: `{"biz":"<id>","pick":<candidate index or -1>,"confidence":0.0-1.0,"title_override":null|"..."}`.

```bash
leadforge dm apply --in dm_labels.ndjson --json
leadforge run --icp icp.yaml --resume --detach --json   # then poll status as in Step 3
```

Repeat `dm export` if the digest says more batches remain.

## Step 5 — Deliver

The final digest lists artifact paths (XLSX, CSV, report.json). Report to the user, briefly:

- counts + tier split (A/B/C), e.g. "142 leads — 38 A, 71 B, 33 C"; top 2-3 "likely need" hooks with counts;
- the sheet path (Summary/About tabs explain scores + compliance reminders);
- any degradations worth knowing (e.g. "9 sites need a browser pass — see troubleshooting").

Do NOT paste sheet contents (≤ 5 rows only if the user explicitly asks for a preview). If the user wants to email
these leads, hand off explicitly to the `send-outreach` skill — do not improvise outreach steps here.

## Repeat campaigns

Same workspace = same SQLite: re-runs dedupe automatically. New campaign = Step 1 with a new `answers.yaml`.
`leadforge status --json` shows the latest run any time. Opt-out: `leadforge suppress add <email-or-domain>`.
