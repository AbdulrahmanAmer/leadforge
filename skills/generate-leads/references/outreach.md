# Outreach protocol (agent-facing, v0.3 ADR-011/012)

Sending is a **bounded, audited extension** of the spreadsheet, not a separate product. It is
dry-run by default everywhere; nothing leaves the workspace without an armed config, a named human
approver, and a content hash that matches what that human actually approved. Never widen these
guardrails to satisfy a request — see `icm/SCOPE.md` boundary 4 and `docs/07-compliance.md`.

## The sequence

```
outreach identity add   (once per sender)      -> a from-name, from-email, postal address, privacy URL, opt-out
outreach mailbox add    (once per identity)     -> the inbox that actually sends, transport + env-var-named creds
outreach plan            --campaign --tier --identity   -> enrol scored leads as outreach_targets
draft export / apply     (unit F)                        -> agent writes drafts, mechanical gate stores messages
outreach approve          --tier | --ids | --all-drafted --approver NAME
outreach send  --dry-run  (default)                       -> renders every approved message to .eml, sends NOTHING
  a human reads the outbox
outreach doctor --identity LABEL                          -> SPF/DKIM/DMARC/MX/warm-up, must be all-ok
outreach send  --live --i-am NAME                          -> only after outreach.armed: true in leadforge.yaml
outreach sync                                              -> ingest bounces/complaints/unsubscribes/replies
outreach status                                            -> counts by state, caps, breaker
outreach outcome add     --business ID --channel --result
```

**Never skip straight to `--live`.** The dry-run digest's `outbox_dir` is where the human reads
what would have gone out; `doctor` must show every check `ok` before arming; `outreach.armed: true`
is a config change only the workspace owner makes (`leadforge config set outreach.armed true`), never
something an agent flips on its own initiative.

## Commands and digest counts to watch

| Command | Purpose | Key flags | Digest counts |
|---|---|---|---|
| `outreach identity add` | register a sender | `--label`, `--from-email` (required), `--from-name`, `--postal-address`, `--privacy-url`, `--unsubscribe-mailto/-url` | `live_complete` (0/1) |
| `outreach identity list` | list senders | — | `identities` |
| `outreach mailbox add` | register a sending inbox | `--identity`, `--address`, `--transport file\|smtp`, `--config key=ENV_VAR_NAME` (repeatable) | `id` |
| `outreach mailbox list` | list inboxes | `--identity` | `mailboxes` |
| `outreach plan` | enrol scored leads | `--campaign`, `--icp` (default `icp.yaml`), `--run`, `--tier A,B`, `--identity`, `--limit`, `--client` | `enrolled`, `suppressed`, `no_sendable_email`, `entity_gate`, `chain_duplicate`, `site_dead`, `already_enrolled` |
| `outreach approve` | bind approval to exact content | `--campaign`, `--approver` + exactly one of `--tier` / `--ids` / `--all-drafted` | `approved`, `candidates` |
| `outreach send` | dry-run render or live send | `--campaign`, `--dry-run` (default) / `--live`, `--i-am NAME`, `--mailbox ADDR`, `--max N` | `would_send` (dry) or `sent`/`unknown`/`skipped_*` (live) |
| `outreach doctor` | sender health, fails closed | `--identity LABEL` | `checks`, `failed` |
| `outreach sync` | ingest bounces/complaints/unsubs/replies | — | `bounce_hard`, `bounce_soft`, `complaint`, `unsubscribe`, `reply`, `duplicate` |
| `outreach status` | lifecycle + mailbox snapshot | `--campaign` | `target_states`, `message_states`, `unknown_sends`, `mailboxes` |
| `outreach outcome add` | record a phone/email result | `--business`, `--channel phone\|email`, `--result`, `--notes` | `id` |

Every command ends with exactly one `LF_DIGEST` line (docs/06). `--json` on any command (root-level
flag, works after the subcommand too) suppresses the human block.

## What `plan`'s exclusion reasons mean

- `suppressed` — the best candidate email (or the domain) is on the suppression list.
- `no_sendable_email` — no `valid`/`role` tier email survives ranking (a `risky`, `inferred`,
  `unknown` or `invalid` tier email is never sendable, no exceptions).
- `entity_gate` — the compliance basis needs a human to confirm entity type first (an unmatched or
  dissolved-company registry result under a strict region policy). The lead is NOT excluded from the
  spreadsheet — only from automatic email eligibility; it still shows up as a call-first lead.
- `chain_duplicate` — another location of the same chain (shared non-freemail domain or phone) already
  claimed the one target this chain gets; the highest-scoring location wins.
- `site_dead` — the site returned an HTTP error or a crawl error other than a robots refusal.
- `already_enrolled` — this business already has a target under this campaign name.

## Reading a `send --dry-run` digest

```
LF_DIGEST {"ok":true,"cmd":"outreach send","counts":{"would_send":12,"candidates":12,"skipped_no_recipient":0},"artifacts":["leadforge_data/outbox/ab12cd34ef.eml", ...]}
```

Nothing was sent. Read a few `.eml` files from the listed `outbox_dir` before ever arming. Each one
carries the full header set (From, Reply-To, To, Subject, Date, Message-ID, List-Unsubscribe,
List-Unsubscribe-Post when applicable) plus a footer with the sender's postal address and a
first-contact privacy line — read that footer too; it is what the recipient sees explaining why they
were contacted.

## Live-send guardrails (never work around these)

- `outreach.armed: true` must already be set in `leadforge.yaml` — an agent proposes this to the
  workspace owner, never sets it itself.
- `--i-am NAME` must equal the `approved_by` on every message being sent; a mismatch skips that
  message (`skipped_not_approver`), it does not error the whole batch.
- A message edited after approval (its `draft_hash` no longer equals `approved_hash`) reverts to
  `drafted` and is skipped (`skipped_hash_mismatch`) — re-approve it explicitly.
- Suppression, eligibility, mailbox status/cap, and the send window are all re-checked at send time,
  not trusted from plan/approve time.
- A crashed send lands the message in `unknown`, never auto-requeued — `outreach status` surfaces
  `unknown_sends`; a human investigates and resolves it manually (there is no "retry" command by
  design — see `outreach.states.transition`).
- A mailbox's hard-bounce or complaint rate over its last 100 live sends pausing it
  (`mailboxes[].status == "paused"`) means STOP using that mailbox — do not switch to a different one
  to route around the pause; tell the user.

## `outreach sync` — never paste message content to the user

`sync`'s digest is counts only (`bounce_hard`, `complaint`, `unsubscribe`, `reply`, ...). Raw bodies
stay on disk under `inbox_dir`; never `cat` them, never quote reply text back to the user beyond a
classification label. This mirrors the token-contract rule for the rest of the pipeline
(`docs/06-token-contract.md`) — outreach content is exactly as unbounded as a crawled web page.
