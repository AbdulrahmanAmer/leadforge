"""U4.6 registry cross-check tests — canned JSON fixtures, no network."""
import pytest

from leadforge.config import load_config
from leadforge.providers import registry as regmod
from leadforge.providers.registry import CompaniesHouseRegistry, get_registries


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


BIZ = {"id": "b1", "name": "Acme Widgets Ltd", "address_city": "Leeds", "address_postal": "LS1 4AB",
       "address_country": "GB", "address_region": None}

SEARCH = {"items": [{"company_number": "01234567", "title": "ACME WIDGETS LTD",
                     "address": {"locality": "Leeds", "postal_code": "LS1 4AB"}}]}
OFFICERS = {"items": [
    {"name": "SMITH, Jane", "officer_role": "director", "appointed_on": "2019-03-01"},
    {"name": "GONE, Bob", "officer_role": "director", "resigned_on": "2021-01-01"},
]}


def _cfg_with_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.registry.companies_house_key = "test-key"
    return cfg


def test_companies_house_maps_active_officers(tmp_path, monkeypatch):
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    reg = CompaniesHouseRegistry(cfg)
    calls = iter([_Resp(SEARCH), _Resp(OFFICERS)])
    monkeypatch.setattr("httpx.get", lambda *a, **k: next(calls))
    out = reg.lookup(BIZ)
    assert len(out) == 1  # resigned officer excluded
    person, ev = out[0]
    assert person.name == "Smith, Jane"
    assert person.title == "Director"
    assert person.labeled_by == "registry" and person.is_dm == 0
    assert ev.fact == "registry_officer" and "01234567" in ev.url


def test_companies_house_locality_mismatch_returns_empty(tmp_path, monkeypatch):
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    reg = CompaniesHouseRegistry(cfg)
    far = {"items": [{"company_number": "999", "address": {"locality": "Glasgow", "postal_code": "G1 1AA"}}]}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(far))
    assert reg.lookup(BIZ) == []


def test_companies_house_429_disables_without_raising(tmp_path, monkeypatch):
    cfg = _cfg_with_key(tmp_path, monkeypatch)
    reg = CompaniesHouseRegistry(cfg)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp({}, status=429))
    monkeypatch.setattr(regmod.time, "sleep", lambda s: None)
    assert reg.lookup(BIZ) == []
    assert reg.disabled is True
    # once disabled, no further network calls
    monkeypatch.setattr("httpx.get", lambda *a, **k: pytest.fail("network call after disable"))
    assert reg.lookup(BIZ) == []


def test_no_keys_is_silent_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    assert get_registries(cfg) == []
