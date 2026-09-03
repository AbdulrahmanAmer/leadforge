"""Provider abstraction (U3.1).

Contract:
- available() -> (bool, reason): cheap capability probe; never raises.
- fetch(query, limit) -> list[RawListing]: blocking; raises ProviderDegraded for recoverable trouble
  (captcha/cooldown/empty-after-retry) and ProviderFailed only when the provider is unusable outright.
Providers NEVER write to the DB — the discover stage normalizes + upserts (separation keeps dedupe in one place).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from leadforge.config import Config
from leadforge.grid import PlannedQuery
from leadforge.models import RawListing
from leadforge.util import ProviderFailed


class DiscoveryProvider(ABC):
    name: str = "base"
    supports_tiles: bool = False  # True only when fetch() actually constrains search to query.tile

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @abstractmethod
    def available(self) -> tuple[bool, str]: ...

    @abstractmethod
    def fetch(self, query: PlannedQuery, limit: int | None = None) -> list[RawListing]: ...


PROVIDERS: dict[str, type[DiscoveryProvider]] = {}

# v0.3: every provider registers its own raw-field map so normalize.to_business can dispatch on
# RawListing.provider instead of assuming gosom's field names.
FIELD_MAPS: dict[str, dict[str, list[str]]] = {}


def register(cls: type[DiscoveryProvider]) -> type[DiscoveryProvider]:
    PROVIDERS[cls.name] = cls
    fmap = getattr(cls, "FIELD_MAP", None)
    if isinstance(fmap, dict):
        FIELD_MAPS[cls.name] = fmap
    return cls


def register_field_map(name: str, fmap: dict[str, list[str]]) -> None:
    FIELD_MAPS[name] = fmap


def get_field_map(name: str) -> dict[str, list[str]] | None:
    if name not in FIELD_MAPS:
        _import_builtins()
    return FIELD_MAPS.get(name)


def _import_builtins() -> None:
    """Import the built-in providers so their @register / field maps run (each import is optional so a
    half-built provider module never takes the others down)."""
    import importlib

    for mod in ("gosom", "fallback_rest", "dvsa", "companies_house", "maps_list"):
        try:
            importlib.import_module(f"leadforge.providers.{mod}")
        except ImportError:
            continue


def get_chain(cfg: Config, only: str | None = None) -> list[DiscoveryProvider]:
    # imports here so registration happens on demand without import cycles
    _import_builtins()

    names = [only] if only else cfg.discovery.providers
    chain: list[DiscoveryProvider] = []
    for name in names:
        cls = PROVIDERS.get(name)
        if cls is None:
            raise ProviderFailed(f"unknown provider '{name}' (known: {sorted(PROVIDERS)})")
        chain.append(cls(cfg))
    if not chain:
        raise ProviderFailed("no discovery providers configured")
    return chain
