# LeadForge upgrade and customization-merge prompt

Copy the prompt below into Claude Code, OpenAI Codex, or another coding agent while working inside an older or customized LeadForge copy.

```text
You are upgrading an existing LeadForge installation to the latest published LeadForge release.

Goal
====
Bring this current customized LeadForge workspace up to the latest upstream release while preserving
intentional local customizations, local configuration, local data, and customer-specific behavior wherever
those customizations do not conflict with the upstream design or safety rules.

Upstream source
==============
Repository: https://github.com/AbdulrahmanAmer/leadforge
Target release: v0.6.0
Target version: 0.6.0

Required workflow
=================
1. Do not ask for permission for normal repository inspection, comparison, editing, testing, committing, or
   pushing within the requested workspace. Continue until the upgrade is complete or a real external blocker
   requires the operator.

2. Read the current workspace's AGENTS.md and all applicable project instructions first. Then inspect the
   current repository end to end before changing anything. Use the repository's code-knowledge graph tools
   first when available; use file search only for literals, configs, scripts, and non-code files.

3. Establish a safety point before edits:
   - record the current branch, commit, remotes, dirty files, and version;
   - do not delete or overwrite the current database, lead lists, exports, caches, or configuration;
   - create a recoverable branch or patch reference if the workspace is a Git repository;
   - never use destructive reset/checkout/clean commands against the user's work.

4. Obtain upstream without replacing the current workspace:
   - clone https://github.com/AbdulrahmanAmer/leadforge into a separate temporary directory;
   - check out tag v0.6.0 (or verify that the requested newer release is actually newer);
   - compare upstream against the current workspace using Git and targeted file inspection;
   - treat the upstream clone as read-only reference material until the merge plan is understood.

5. Read upstream AGENTS.md, README.md, CHANGELOG.md, docs/10-enrichment-phase0-baseline.md, and the relevant
   model, database, enrichment, outreach, and test files. Summarize:
   - upstream additions;
   - local customizations;
   - overlapping files;
   - schema/config/version changes;
   - conflicts that require a judgment call.

6. Merge selectively into the current workspace. Preserve local customizations unless they directly conflict
   with a required upstream invariant. Do not replace an entire file merely because upstream changed it. Use
   three-way comparison or focused patches for overlapping files. Keep local lead data and local configuration
   intact. If a local customization is valuable but conflicts with upstream, adapt it on top of the upstream
   interfaces and document the decision.

7. Preserve these LeadForge invariants:
   - the default path remains OSS/no-key and paid providers remain disabled unless explicitly configured;
   - robots.txt, per-host politeness, caps, suppression, and public-business-data rules remain intact;
   - never add SMTP mailbox probing or RCPT enumeration;
   - never scrape LinkedIn or login-gated sources;
   - never add anti-bot evasion, fake accounts, fingerprint spoofing, a dialer, or SMS;
   - email sending remains dry-run by default and keeps approval, content-hash, suppression, eligibility,
     cap, send-window, bounce, complaint, and mailbox safeguards;
   - strict email confirmation may use licensed-provider verification, delivery evidence, replies, or operator
     confirmation, but MX alone must not be represented as proof that a mailbox exists.

8. Handle the v0.6.0 enrichment upgrade carefully:
   - preserve the canonical field-level observation model and its provenance, verification, freshness,
     jurisdiction, cost, and person-ownership fields;
   - preserve multiple people per business and multiple phones/emails per person;
   - preserve phone types and email stages;
   - preserve conservative BYOL matching and WESCORE workbook benchmark import;
   - preserve source/provider/contact-type outreach attribution and coverage metrics;
   - preserve the optional paid-adapter boundary and default-disabled behavior;
   - do not silently migrate or destroy an older database if the current project instructions require a
     fail-closed schema policy. Report the exact schema action and require the documented migration path.

9. Update local documentation for any retained customization. Update version references consistently if this
   workspace is being released. Add a CHANGELOG entry describing the merge and any local deviations from
   upstream.

10. Test proportionally and then broadly:
    - run focused tests for every changed subsystem;
    - run `ruff check src tests` and `git diff --check`;
    - run the full test suite using the repository's documented command;
    - verify the CLI digest contract;
    - verify a fresh empty workspace can initialize;
    - verify existing local data/configuration was not modified unexpectedly;
    - if a live/network fixture is flaky, rerun it serially and report both results honestly.

11. Review the final diff for accidental data, secrets, generated caches, temporary clones, or unrelated edits.
    Do not commit `.env` files, databases, exports, logs, caches, or credentials.

12. Commit the completed merge with a clear message such as:
    `chore: merge LeadForge v0.6.0 into customized workspace`
    Push only the intended branch/remote. Do not force-push. If the operator did not request a push, stop after
    creating the commit and report the commit hash.

Final report
============
Report:
 - current workspace commit before the merge;
 - upstream release/tag and commit used;
 - files changed;
 - local customizations preserved;
 - conflicts and judgment calls;
 - schema/config/version handling;
 - tests and exact results;
 - final commit and push status;
 - any manual follow-up required.

If an unavoidable conflict cannot be resolved without changing business behavior or losing a local
customization, stop at that conflict, explain the concrete alternatives, and ask one concise decision question.
Do not ask for permission merely to inspect, compare, test, or perform ordinary in-scope merge work.
```

## Current release reference

The upstream release described by this prompt is LeadForge `v0.6.0`. Verify the tag and commit before applying
the prompt to a future workspace; if a newer release exists, substitute that release only after reading its
`CHANGELOG.md` and project instructions.
