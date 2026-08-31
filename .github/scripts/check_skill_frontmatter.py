#!/usr/bin/env python3
"""CI guard: SKILL.md frontmatter must stay portable across harnesses.

The open Agent Skills spec allows exactly these keys. Claude-only extras (argument-hint, context, model, …)
break claude.ai packaging and are ignored by Codex/Cursor/others — so this repo keeps to the spec six.
Ref: docs/01-research.md §1.1.
"""

from __future__ import annotations

import sys
from pathlib import Path

ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
REQUIRED = {"name", "description"}
REPO = Path(__file__).resolve().parents[2]

failures: list[str] = []
skills = sorted(REPO.glob("skills/*/SKILL.md"))
if not skills:
    print("no SKILL.md files found under skills/", file=sys.stderr)
    raise SystemExit(1)

for skill in skills:
    text = skill.read_text(encoding="utf-8")
    if not text.startswith("---"):
        failures.append(f"{skill}: missing YAML frontmatter")
        continue
    end = text.find("\n---", 3)
    if end == -1:
        failures.append(f"{skill}: unterminated frontmatter")
        continue
    block = text[3:end]
    keys = {
        line.split(":", 1)[0].strip()
        for line in block.splitlines()
        if line.strip() and not line.startswith((" ", "\t", "#")) and ":" in line
    }
    extra = keys - ALLOWED
    missing = REQUIRED - keys
    if extra:
        failures.append(f"{skill}: non-portable frontmatter key(s): {sorted(extra)}")
    if missing:
        failures.append(f"{skill}: missing required key(s): {sorted(missing)}")
    # directory name must equal `name` (spec + Codex requirement)
    for line in block.splitlines():
        if line.startswith("name:"):
            declared = line.split(":", 1)[1].strip()
            if declared != skill.parent.name:
                failures.append(f"{skill}: name '{declared}' != directory '{skill.parent.name}'")

if failures:
    print("SKILL.md frontmatter problems:", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    raise SystemExit(1)

print(f"ok — {len(skills)} skill(s) have portable frontmatter")
