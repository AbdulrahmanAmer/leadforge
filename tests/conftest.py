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
