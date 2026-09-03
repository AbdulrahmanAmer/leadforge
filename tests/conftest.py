import sqlite3

import pytest

from leadforge import db
from leadforge.config import load_config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return load_config(tmp_path)


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    return db.connect(cfg.db_path)


@pytest.fixture
def sample_icp():
    from leadforge.models import ICP

    return ICP.model_validate({
        "campaign": "test-campaign",
        "offer": {"what": "Website redesign", "value_prop": "more jobs"},
        "target": {"categories": ["auto repair shop"],
                   "geography": {"areas": ["Houston, TX"], "country": "US"},
                   "size": {"min_reviews": 10}},
        "qualify": {"hard": ["no_phone"], "soft": ["website_missing", "stale_site"]},
        "decision_maker": {"titles_priority": ["Owner", "General Manager"]},
    })


@pytest.fixture(autouse=True)
def _no_ui(monkeypatch):
    """Tests must never pop Excel or console windows (auto-open features, v0.1.3)."""
    monkeypatch.setenv("LEADFORGE_NO_UI", "1")

@pytest.fixture(autouse=True)
def _never_autodetect_claude(monkeypatch):
    """v0.4: `agent_runner.resolve_command` auto-detects `claude` on PATH. A test that reaches the
    autopilot labeling/drafting stages without patching the runner would otherwise shell out to the
    operator's real Claude Code (it happened in the 2026-09-03 full-suite run). Explicit
    `agent.command` lists (the fake scripts in test_agent_runner.py) still work."""
    import leadforge.agent_runner as _ar
    monkeypatch.setattr(_ar.shutil, "which", lambda *a, **k: None)
