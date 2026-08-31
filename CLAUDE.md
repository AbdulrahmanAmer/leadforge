# CLAUDE.md

## ⚡ Build trigger

**If the user says `BUILD LEADFORGE`, `/finalize`, "finish the build", or "continue the ICM": immediately read
`icm/HANDOFF.md`, then `icm/STATE.md`, then `docs/05-icm-build-plan.md`, then `AGENTS.md` — and start working
the remaining units in the order `icm/STATE.md` lists.** Do not explore the repo first and do not re-architect
anything; the design is decided and documented. Verify the baseline (`pip install -e .[dev] && pytest -q &&
ruff check src tests` → 59 passed, 1 xfailed, ruff clean) before writing code.

The same instructions exist as a pasteable prompt in `icm/PROMPT.txt` and as the `/finalize` command.

---

Read `AGENTS.md` — it is the canonical agent guide for this repo (instructions there apply to Claude Code fully).

Claude-specific notes:

- This repo is a Claude Code plugin **and** its own marketplace: `/plugin marketplace add AbdulrahmanAmer/leadforge` →
  `/plugin install leadforge@leadforge`. Local dev: `claude --plugin-dir .` and `claude plugin validate .`.
- The skill is namespaced `/leadforge:generate-leads` and auto-triggers on lead-gen requests via its description.
- When finalizing the build, follow the one-shot protocol in `docs/05-icm-build-plan.md` exactly; keep `pytest` green after every unit.
