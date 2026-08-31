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


def register(cls: type[DiscoveryProvider]) -> type[DiscoveryProvider]:
    PROVIDERS[cls.name] = cls
    return cls


def get_chain(cfg: Config, only: str | None = None) -> list[DiscoveryProvider]:
    # imports here so registration happens on demand without import cycles
    from leadforge.providers import fallback_rest, gosom  # noqa: F401

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
