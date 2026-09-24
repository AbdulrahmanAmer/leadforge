# Spec — `generate-leads` skill v2

> **NOTE — implement LAST.** Status: draft spec (2026-09-24). Rewriting the skill is the final layer of the
> upgrade, because v2 describes CLI behaviour that does not exist yet (background runs, `status --watch`,
> isolated agent runner). Writing the skill first would document commands that are not there. Update this
> spec as those layers land; build the skill only when every "Depends on" item below is shipped.

## Why v1 is not good enough for other users

Reviewed `skills/generate-leads/SKILL.md` (v0.4.1, 151 lines) + `references/`. What works: the trigger
description, the digest contract (`LF_DIGEST` is the result), the geography interview rules, the outreach
safety rules. What breaks for someone who is not the author:

| # | Problem | Evidence |
|---|---|---|
| 1 | **Long runs cannot finish inside the agent.** Step 2 says "wait for the digest"; real runs take hours (the parked 2026-09-02 run projected 10-11 h of enrichment). A shell tool call times out at 10 min and the CLI has no background/detach mode. | `SKILL.md:78`; `grep -rE "detach\|background" src/leadforge/cli*` → no hits |
| 2 | **Bootstrap assumes a repo checkout.** "`pip install -e <repo-root>`" — a marketplace install puts the plugin in Claude's plugin cache; the user has no repo root and the skill never uses `${CLAUDE_PLUGIN_ROOT}`. | `SKILL.md:32`; no `PLUGIN_ROOT` reference anywhere |
| 3 | **Autopilot spawns full `claude -p` sessions** that load the user's every MCP server, hook and plugin per call — slow, and the same pattern that took the machine down in the 2026-09-22 war-room crash. | `src/leadforge/agent_runner.py:64` |
| 4 | **Heavy installs without consent.** `doctor --fix --full` pulls torch/GLiNER/browser (GBs) on first contact. | `SKILL.md:29` |
| 5 | **Owner-specific values leak into the generic skill** (`--label gainlev`, phone-first "by owner decision"). | `SKILL.md:114,124-129` |
| 6 | **One skill does two jobs.** Lead generation and email outreach (identity, mailbox, DKIM, arming) share one 151-line body; every lead-gen call loads the outreach protocol. | `SKILL.md:112-145` |
| 7 | **No resume/park story.** Nothing tells the agent how to find, resume, or report on a run that outlived the previous session. | whole file; resume recipe lives only in `.claude/POSITION.md` |
| 8 | **No cost/time estimate before launch.** `plan` is "optional"; the user is never told "≈ N hours, ≈ M sites" before committing. | `SKILL.md:74` |
| 9 | Thin troubleshooting (19 lines) for a tool with a scraper binary, torch, a browser and SMTP. | `references/troubleshooting.md` |

## v2 requirements

1. **Two skills.** `generate-leads` (interview → plan → launch → monitor → deliver) and `send-outreach`
   (identity, mailbox, draft review, approve, dry-run, doctor). Each body ≤ 120 lines; detail in `references/`.
2. **Launch-and-monitor, never block.** `leadforge run … --detach --json` returns a run id + log path
   immediately; the agent polls `leadforge status --run <id> --json` (bounded digest) and reports progress.
   A run that outlives the session is picked up by `leadforge status --json` in the next session.
3. **Plan is mandatory** and its digest carries time + volume estimates; the agent states them and gets a
   yes before `run` when the estimate is over a threshold (e.g. > 30 min or > 80 queries).
4. **Bootstrap from the plugin.** Per the Claude Code docs (skills.md "String Substitutions";
   plugins.md "bin/"): `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` are substituted in SKILL.md text,
   and a plugin's `bin/` directory is on the Bash tool's PATH while the plugin is enabled. Design: ship
   `bin/leadforge` (a small shim) that on first use creates a venv in `${CLAUDE_PLUGIN_DATA}`, installs the
   package from `${CLAUDE_PLUGIN_ROOT}`, then execs `python -m leadforge "$@"` — no repo-root assumption,
   survives plugin updates by re-installing when the plugin version changes. (Install path stability across
   updates is not documented — the shim must key on the version, not the path.) `doctor --json` (light) by default; `--full` extras only after the
   user agrees, with the download size stated.
5. **Isolated autopilot.** The agent runner calls `claude -p` with `--strict-mcp-config` (no MCP servers) and
   no hooks (verify the mechanism, e.g. `--settings` with `disableAllHooks`), bounded concurrency, and a
   per-call timeout. Documented so users know autopilot uses their Claude subscription.
6. **Neutral defaults.** No owner names, labels, or channel policy in the skill; those live in the user's
   workspace config.
7. **Troubleshooting** covers: scraper binary, torch/NER, browser extra, rate limiting / blocked sites,
   locked SQLite, resuming a parked run, outreach DNS checks.

## Depends on (build these first)

- [x] `run --detach` + `status --run <id>` (Layer 1, done 2026-09-24)
- [ ] `plan` digest: discovery estimate exists (`est_runtime_min`); add an enrichment estimate from measured rates (~23 sites/min on GPU)
- [x] isolated, bounded agent runner (Layer 2, done 2026-09-24)
- [ ] plugin-root bootstrap path (packaging layer)

## Acceptance (the skill is done when)

- A fresh machine with only the marketplace plugin installed completes interview → exported sheet on a
  small campaign (≤ 50 leads) without the agent hitting a tool timeout.
- A long campaign launched in one session is found and resumed by `status` in a new session.
- `claude plugin validate .` passes; SKILL.md version == plugin.json version == pyproject version.
- `send-outreach` never sends live mail; dry-run `.eml` files are produced.
