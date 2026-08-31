"""Layered configuration (U0.3).

Order: built-in defaults -> ./leadforge.yaml (workspace) -> env LEADFORGE_SECTION__KEY.
All runtime state lives under `data_dir` (default ./leadforge_data). Nothing here performs network I/O.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class FallbackRestCfg(BaseModel):
    url: str = "http://localhost:8765"


class DiscoveryCfg(BaseModel):
    providers: list[str] = Field(default_factory=lambda: ["gosom"])
    grid_mode: str = "off"  # off | auto — keep off until gosom grid flags are live-verified (U8.2)
    grid_cell_km: float = 3.0
    depth: int = 10
    concurrency: int = 2
    lang: str = "en"
    timeout_min: int = 30
    proxies: list[str] = Field(default_factory=list)
    email_crawl: bool = False
    fallback_rest: FallbackRestCfg = Field(default_factory=FallbackRestCfg)


class CrawlCfg(BaseModel):
    pages_per_site: int = 6
    timeout_s: float = 15.0
    max_text_bytes: int = 400_000


class PolitenessCfg(BaseModel):
    delay_s: float = 2.0
    workers: int = 4
    user_agent: str = "LeadForgeBot/0.1 (internal lead research)"


class ValidationCfg(BaseModel):
    dns_timeout_s: float = 5.0
    staleness_days: int = 90


class RegistryCfg(BaseModel):
    companies_house_key: str = ""
    opencorporates_token: str = ""


class SocialCfg(BaseModel):
    """Optional social/video presence signals via Agent-Reach (U4.8, providers/social.py).

    Off by default. Public, logged-out, business-owned profiles only — never LinkedIn, never
    cookie/session auth (see icm/SCOPE.md and the provider module docstring).
    """

    enabled: bool = False
    networks: list[str] = Field(default_factory=lambda: ["youtube", "facebook", "instagram"])
    max_networks: int = 3
    stale_months: int = 6
    timeout_s: float = 30.0
    max_calls_per_min: int = 20


class ExportCfg(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["xlsx", "csv"])


class Config(BaseModel):
    data_dir: str = "leadforge_data"
    default_region: str = "US"
    discovery: DiscoveryCfg = Field(default_factory=DiscoveryCfg)
    crawl: CrawlCfg = Field(default_factory=CrawlCfg)
    politeness: PolitenessCfg = Field(default_factory=PolitenessCfg)
    validation: ValidationCfg = Field(default_factory=ValidationCfg)
    registry: RegistryCfg = Field(default_factory=RegistryCfg)
    social: SocialCfg = Field(default_factory=SocialCfg)
    export: ExportCfg = Field(default_factory=ExportCfg)

    _workspace: Path = Path(".")

    # --- path helpers (create-on-demand) -------------------------------------------------
    @property
    def workspace(self) -> Path:
        return self._workspace

    def _dir(self, *parts: str) -> Path:
        p = (self._workspace / self.data_dir).joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_path(self) -> Path:
        return self._dir()

    @property
    def bin_dir(self) -> Path:
        return self._dir("bin")

    @property
    def cache_dir(self) -> Path:
        return self._dir("cache")

    @property
    def exports_dir(self) -> Path:
        return self._dir("exports")

    @property
    def logs_dir(self) -> Path:
        return self._dir("logs")

    @property
    def db_path(self) -> Path:
        return self._dir() / "db.sqlite3"


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(prefix: str = "LEADFORGE_") -> dict[str, Any]:
    """LEADFORGE_DISCOVERY__DEPTH=5 -> {"discovery": {"depth": 5}} (values parsed as YAML scalars)."""
    out: dict[str, Any] = {}
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix):].lower().split("__")
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        try:
            node[path[-1]] = yaml.safe_load(val)
        except yaml.YAMLError:
            node[path[-1]] = val
    return out


def load_config(workspace: Path | str = ".", data_dir_override: str | None = None) -> Config:
    workspace = Path(workspace)
    merged: dict[str, Any] = {}
    cfg_file = workspace / "leadforge.yaml"
    if cfg_file.is_file():
        loaded = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{cfg_file} must contain a YAML mapping")
        merged = _deep_merge(merged, loaded)
    merged = _deep_merge(merged, _env_overrides())
    if data_dir_override:
        merged["data_dir"] = data_dir_override
    cfg = Config.model_validate(merged)
    cfg._workspace = workspace
    return cfg
