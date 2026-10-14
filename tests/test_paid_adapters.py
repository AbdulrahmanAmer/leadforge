from leadforge.enrich.adapters import AdapterRegistry


class FakePaidAdapter:
    name = "licensed-x"
    cost_per_lookup = 0.25

    def enrich(self, business):
        return []


def test_paid_adapters_are_disabled_by_default(cfg):
    registry = AdapterRegistry()
    registry.register(FakePaidAdapter())
    assert registry.enabled(cfg) == {}


def test_paid_adapter_requires_explicit_provider_selection(cfg):
    registry = AdapterRegistry()
    registry.register(FakePaidAdapter())
    cfg.paid_enrichment.enabled = True
    assert registry.enabled(cfg) == {}
    cfg.paid_enrichment.providers = ["licensed-x"]
    assert list(registry.enabled(cfg)) == ["licensed-x"]
