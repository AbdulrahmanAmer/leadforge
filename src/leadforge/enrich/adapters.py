"""Opt-in boundary for licensed enrichment providers.

The default registry is empty and disabled. Adapters must return the same observation
model used by OSS enrichment and cannot bypass the existing compliance gates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from leadforge.config import Config
from leadforge.models import EnrichmentObservation


class PaidEnrichmentAdapter(Protocol):
    name: str
    cost_per_lookup: float

    def enrich(self, business: dict) -> Iterable[EnrichmentObservation]: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, PaidEnrichmentAdapter] = {}

    def register(self, adapter: PaidEnrichmentAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def enabled(self, cfg: Config) -> dict[str, PaidEnrichmentAdapter]:
        if not cfg.paid_enrichment.enabled:
            return {}
        selected = set(cfg.paid_enrichment.providers)
        return {name: adapter for name, adapter in self._adapters.items() if name in selected}


def register_builtin_adapters(registry: AdapterRegistry, loader: Callable[[str], PaidEnrichmentAdapter] | None = None) -> None:
    """Extension hook; no provider is bundled or implicitly loaded."""
    if loader is None:
        return
    # Kept explicit so configuration cannot cause arbitrary imports or network calls.
    return
