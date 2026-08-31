#!/usr/bin/env python3
"""User-scope skill bridge + install helper (U7.5).

Installs the repo's skill into the per-user skill directories that agent harnesses read, so LeadForge works
even without going through a plugin marketplace. Cross-platform (uses copies on Windows, symlinks elsewhere).

    python install.py            # install skill for all detected harnesses + pip install the CLI
    python install.py --check    # report what's installed / what's missing, change nothing
    python install.py --skill-only    # skip pip install
    python install.py --targets claude,codex

Harness install matrix (preferred routes) is printed at the end and lives in README.md.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# Windows consoles default to cp1252; this script prints arrows/checkmarks.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent
SKILLS_SRC = REPO / "skills"

# where each harness looks for user-scope skills (verified 2026-08-31; see docs/01-research.md §1)
TARGETS = {
    "claude": Path.home() / ".claude" / "skills",
    "codex": Path.home() / ".agents" / "skills",   # cross-tool Agent Skills convention Codex reads
    "cursor": Path.home() / ".cursor" / "skills",
}


def link_or_copy(src: Path, dst: Path) -> str:
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() or (dst.is_dir() and (dst / "SKILL.md").exists()):
            return "already installed"
        return "skipped (path exists and is not ours)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        try:  # directory junction needs no admin rights
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                           check=True, capture_output=True, encoding="utf-8", errors="replace")
            return "linked (junction)"
        except (subprocess.CalledProcessError, OSError):
            shutil.copytree(src, dst)
            return "copied"
    try:
        os.symlink(src, dst, target_is_directory=True)
        return "linked"
    except OSError:
        shutil.copytree(src, dst)
        return "copied"


def main() -> int:
    ap = argparse.ArgumentParser(description="Install the LeadForge skill + CLI for local agent harnesses.")
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--skill-only", action="store_true", help="do not pip install the CLI")
    ap.add_argument("--targets", default="claude,codex", help="comma list: claude,codex,cursor (default: claude,codex)")
    args = ap.parse_args()

    skills = sorted(p for p in SKILLS_SRC.iterdir() if (p / "SKILL.md").exists()) if SKILLS_SRC.is_dir() else []
    if not skills:
        print(f"! no skills found under {SKILLS_SRC}")
        return 1
    print(f"LeadForge repo: {REPO}\nskills: {', '.join(s.name for s in skills)}\n")

    wanted = [t.strip() for t in args.targets.split(",") if t.strip()]
    for target in wanted:
        base = TARGETS.get(target)
        if base is None:
            print(f"  {target}: unknown target (known: {', '.join(TARGETS)})")
            continue
        for skill in skills:
            dst = base / skill.name
            if args.check:
                print(f"  {target}: {dst} — {'present' if dst.exists() else 'MISSING'}")
            else:
                print(f"  {target}: {dst} — {link_or_copy(skill, dst)}")

    if not args.skill_only and not args.check:
        print("\ninstalling the leadforge CLI (editable)...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(REPO)],
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print("! pip install failed — install manually: pip install -e .")
        else:
            print("ok. next: leadforge doctor --fix")

    print(
        "\nPreferred install routes (no bridge needed):\n"
        "  Claude Code : /plugin marketplace add AbdulrahmanAmer/leadforge  →  /plugin install leadforge@leadforge\n"
        "  Codex       : codex plugin marketplace add AbdulrahmanAmer/leadforge  →  install from /plugins\n"
        "  Any harness : npx skills add AbdulrahmanAmer/leadforge\n"
        "Then run: leadforge doctor --fix"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
