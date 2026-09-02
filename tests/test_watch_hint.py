"""`leadforge watch` outside a workspace must say so at once (a human in the wrong folder saw one line
and then 15 minutes of silence on 2026-09-03)."""

from __future__ import annotations

import json
import subprocess
import sys


def _watch(cwd) -> tuple[int, list[dict]]:
    p = subprocess.run([sys.executable, "-m", "leadforge", "watch", "--json"], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60, shell=False)
    digests = [json.loads(ln[len("LF_DIGEST "):]) for ln in (p.stdout + p.stderr).splitlines() if ln.startswith("LF_DIGEST ")]
    return p.returncode, digests


def test_watch_outside_a_workspace_fails_fast_with_a_hint(tmp_path):
    code, digests = _watch(tmp_path)
    assert code == 4 and len(digests) == 1 and digests[0]["ok"] is False
    assert "no LeadForge workspace" in digests[0]["warnings"][0]
    assert not (tmp_path / "leadforge_data" / "db.sqlite3").exists()  # watch must not create a workspace
