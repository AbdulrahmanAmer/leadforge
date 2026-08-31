---
description: Load the LeadForge ICM handoff and finish the remaining build units one by one.
---

# /finalize — finish the LeadForge build

Read these four files now, in order, before doing anything else:

1. @icm/HANDOFF.md — orientation, invariants, how to work in this repo
2. @icm/STATE.md — the live worklist (done vs remaining); keep it updated as you go
3. @docs/05-icm-build-plan.md — staged plan, unit specs, acceptance criteria, and the one-shot finalize protocol
4. @AGENTS.md — coding rules and red lines

Then:

- Confirm the baseline is green before writing code: `pip install -e .[dev]`, `pytest -q`, `ruff check src tests`.
  Expected: **64 passed, 2 xfailed**, ruff clean. If red, run `leadforge doctor --fix` and reinstall first.
- Work the remaining units in the order listed in `icm/STATE.md`. Each has a binding spec in its stub file's
  docstring, plus acceptance criteria in `docs/05-icm-build-plan.md` and `icm/stages/*.md`.
- After each unit: tests green, ruff clean, tick the checkbox in `icm/STATE.md`, and commit using the unit id
  (e.g. `U3.6: fallback REST discovery provider`).
- Prioritize **U8.2 (live end-to-end validation)** once the offline units are done — it is the only step that
  exercises the real scraper and will expose field drift in `GOSOM_FIELD_MAP`. Turn what you learn into a real
  fixture test.

Hard rules you may not break to make a gate pass:

- The `LF_DIGEST` contract (`docs/06-token-contract.md`).
- Politeness/compliance invariants (`docs/04` §5, `docs/07-compliance.md`).
- No anti-bot/captcha evasion, fingerprint spoofing, fake accounts, or outreach/sending features — use the
  pinned OSS engines as shipped. If a unit seems to require any of that, stop and say so.
- Country stays required on every campaign; areas stay specific. Vague locations produce garbage lead lists.

Report progress against `icm/STATE.md` as you complete each unit.
