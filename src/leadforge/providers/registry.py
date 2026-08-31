"""Public-registry cross-check providers — ICM unit U4.6 (STUB with binding spec). Opt-in, free keys (ADR-007).

=== SPEC (implement exactly; acceptance criteria in docs/05 U4.6) ===

class RegistryProvider(Protocol):
    def lookup(self, business_row) -> list[Person]   # labeled_by="registry", dm_confidence=0.0, is_dm=0

1. CompaniesHouseRegistry (UK; cfg.registry.companies_house_key non-empty enables):
   - base https://api.company-information.service.gov.uk, HTTP Basic auth (key as username, blank password)
   - GET /search/companies?q=<business name>&items_per_page=3 -> match by locality/postcode overlap with
     business address; on match GET /company/{number}/officers (active only) ->
     Person(name=officer name title-cased, title=officer_role humanized: "director" -> "Director").
   - Throttle: <= 600 req / 5 min (shared token bucket in the module); on 429 back off 60s once, then skip.
   - Evidence per person: fact="registry_officer", url=company profile URL, snippet=officer role + appointed_on.
2. OpenCorporatesRegistry (cfg.registry.opencorporates_token enables):
   - GET https://api.opencorporates.com/v0.4/companies/search?q=<name>&api_token=..&jurisdiction_code=<us_xx|gb>
   - Same matching + Person/Evidence emission; respect free-tier limits (unpublished — treat 403/429 as disable-for-run).
3. Gate: only called from enrich when cfg has a key AND business country matches the registry's jurisdiction.
   With no keys configured, enrich never imports this module's network paths (silent no-op).
4. Never blocks the run: any exception -> log + return [].
5. tests/test_registry.py: canned JSON fixtures for both APIs, no network; assert Person mapping + evidence facts.

Effect on scoring: presence of labeled_by="registry" person rows feeds the data_confidence factor
("registry_corroborated") — see score.py _f_data_confidence.
"""

from __future__ import annotations


def enabled_registries(cfg) -> list[str]:
    out = []
    if cfg.registry.companies_house_key:
        out.append("companies_house")
    if cfg.registry.opencorporates_token:
        out.append("opencorporates")
    return out
