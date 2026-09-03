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


class DvsaCfg(BaseModel):
    """DVSA 'Active MOT test stations' register (gov.uk, OGL, refreshed quarterly) - v0.3 provider `dvsa`."""

    url: str = "https://assets.publishing.service.gov.uk/media/69a0638bc497bac082bc7741/active-mot-stations.csv"
    refresh_days: int = 90


class CompaniesHouseDiscoveryCfg(BaseModel):
    """Companies House advanced search as a discovery provider (company mode, v0.3)."""

    page_size: int = 100
    max_hits_per_shard: int = 9000  # the API stops paging at 10k; a bigger shard must be split
    exclude_sic: list[str] = Field(default_factory=lambda: ["82200"])  # call centres = competitors (owner decision 7)


class MapsListCfg(BaseModel):
    """Native list-first Google Maps provider (`maps_list`, speed unit) — docs/09, ADR-014.

    Measured 2026-09-02 (probe_list2.py, plain Playwright, no stealth): one persistent browser,
    ~120 cards per search in 25-28s once the browser is up (13s page load + ~15s scrolling);
    phone on 119-120/120 cards, website on 83-91/120. Defaults below are set from that probe.
    """

    max_cards: int = 130            # Google Maps list caps out ~120/search server-side regardless
    search_delay_s: float = 4.0     # politeness gap between searches in one browser; +/-30% jitter applied
    max_searches_per_hour: int = 120  # token-bucket cap per provider instance
    visit_details: bool = False     # opt-in place-page visit for full address/hours/plus-code on NEW cards only
    detail_tabs: int = 3            # pages of the same context cycled (round-robin) for detail visits
    delay_s: float = 1.5            # politeness gap between detail-page navigations; +/-30% jitter applied


class DiscoveryCfg(BaseModel):
    providers: list[str] = Field(default_factory=lambda: ["gosom"])  # + "dvsa" | "companies_house" per campaign
    grid_mode: str = "off"  # off | auto — keep off until gosom grid flags are live-verified (U8.2)
    grid_cell_km: float = 3.0
    depth: int = 10
    concurrency: int = 2
    lang: str = "en"
    timeout_min: int = 30
    stall_s: float = 180.0  # kill gosom when results stop growing this long (v1.17.4 hangs after finishing)
    proxies: list[str] = Field(default_factory=list)
    email_crawl: bool = False
    fallback_rest: FallbackRestCfg = Field(default_factory=FallbackRestCfg)
    # v0.3 coverage: explicit [minLng, minLat, maxLng, maxLat] per area label bypasses the geocoder
    area_bbox: dict[str, list[float]] = Field(default_factory=dict)
    subdivide_at: int = 100        # a tiled query returning >= this many listings is split into 4 child tiles
    max_subdivisions: int = 2
    est_min_per_query: float = 2.0         # measured 2026-08-31: 1.7 min untiled
    est_min_per_tiled_query: float = 4.0   # measured 2026-08-31: 3.5-4.7 min tiled
    dvsa: DvsaCfg = Field(default_factory=DvsaCfg)
    companies_house: CompaniesHouseDiscoveryCfg = Field(default_factory=CompaniesHouseDiscoveryCfg)
    maps_list: MapsListCfg = Field(default_factory=MapsListCfg)
    # speed unit: fan discover queries across N provider-chain instances (each its own browser/subprocess
    # pool); ==1 keeps the original strictly-serial loop. See pipeline.run_discover.
    parallel_queries: int = 1


class CrawlCfg(BaseModel):
    pages_per_site: int = 6
    headed_browser: bool = False  # browser-escalation runs VISIBLY (debug aid; real-browser rendering, no stealth flags)
    stale_after_years: int = 2  # copyright year older than this many years back => stale_site signal
    # v0.3 speed unit (2026-09-02): the home page is the ONLY fetch that gates whether a site is
    # crawled at all, so it keeps the longer timeout; secondary/candidate pages (about, team, contact,
    # sitemap probe...) get the shorter page_timeout_s — a dead or slow secondary page used to cost the
    # full 15s EACH, and a 6-page site chained that into minutes. site_budget_s caps the whole crawl()
    # call's wall clock (home fetch + every secondary page) so one slow-but-alive site can never eat
    # more than its share of a batch — once exceeded, crawl() stops fetching further pages and returns
    # what it already collected (still ok=True if the home page loaded; signals["budget_exhausted"]=True).
    timeout_s: float = 10.0          # was 15.0 — home page fetch only now
    page_timeout_s: float = 6.0      # secondary/candidate page + sitemap-probe fetch timeout
    site_budget_s: float = 45.0      # per-site wall-clock budget across the whole crawl() call
    max_text_bytes: int = 400_000


class PolitenessCfg(BaseModel):
    delay_s: float = 2.0
    # v0.3 speed unit (2026-09-02): was 4. HostThrottle (util.py) serializes every request to a given
    # host to one-in-flight with >= delay_s (+-30% jitter) between them, regardless of `workers` — this
    # knob only controls how many DIFFERENT hosts run concurrently. Measured on a 20-site sample
    # (scratchpad/speed/enrich/result_A.json vs result_B.json): raising workers alone (4 -> 16) did NOT
    # improve throughput because the slow tail (403/Cloudflare blocks, dead hosts, the browser-render
    # semaphore) dominated — see crawl.site_budget_s / crawl.page_timeout_s / enrich.browser_concurrency,
    # which is what actually lets more workers help. 12 is the number those fixes are measured against
    # (scripts/bench_enrich.py) — per-host pacing is unchanged at any worker count.
    workers: int = 12
    user_agent: str = "LeadForgeBot/0.1 (internal lead research)"


class ValidationCfg(BaseModel):
    dns_timeout_s: float = 5.0
    staleness_days: int = 90
    # opt-in: propose a LIKELY address for a known decision maker when the domain's own naming
    # convention is demonstrated by an email already found there. Public evidence + MX only —
    # never SMTP/RCPT probing (icm/SCOPE.md #5). Exported in its own column, never as a found email.
    infer_emails: bool = False
    # v0.3 (owner decision 5): which freemail addresses count as the business's own -
    # linked (local part matches the business or a known person), any, none (own-domain only)
    freemail_policy: str = "linked"


class EnrichCfg(BaseModel):
    """v0.3 speed unit (2026-09-02): browser-escalation throughput, overlapped stages, DNS pooling.
    None of this touches politeness — HostThrottle's per-host delay/single-flight is unaffected; these
    knobs only change how many DIFFERENT hosts/domains/lookups run at once and how the 'all' stage
    sequence is scheduled."""

    browser_concurrency: int = 4     # was a hardcoded threading.Semaphore(2) in browser.py
    rendered_pages_per_site: int = 2  # was the hardcoded MAX_RENDERED_PAGES_PER_SITE = 3
    render_timeout_s: float = 20.0    # the rendered fetch's own timeout (was borrowing crawl.timeout_s,
                                       # which just dropped to 10s — nowhere near enough for a real render)
    overlap_stages: bool = True       # stage='all' interleaves registry(backlog)+validate with the crawl;
                                       # `enrich --stage <one>` is never affected by this (single-stage
                                       # runs always use the original serial per-stage functions)
    dns_workers: int = 8              # MX-lookup thread pool size for validate_emails_parallel


class RegistryCfg(BaseModel):
    companies_house_key: str = ""
    opencorporates_token: str = ""
    min_name_similarity: float = 0.45  # v0.3: a registry hit must also resemble the business name
    active_only: bool = True           # v0.3: dissolved/liquidated companies are never matched


class OutreachCfg(BaseModel):
    """v0.3 sending layer (ADR-011/012). Nothing sends unless `armed` is true AND `--live` AND `--i-am` agree."""

    armed: bool = False
    transport: str = "file"            # file (dry-run .eml) | smtp | <registered adapter>
    require_corporate: bool = False    # owner decision 5: plausibility-linked freemail is mailable by default
    daily_cap_default: int = 30
    send_window: str = "09:00-17:00"   # local time of `timezone`
    timezone: str = "Europe/London"
    max_touches: int = 2
    follow_up_days: int = 5
    bounce_rate_pause: float = 0.03    # hard bounces over the last 100 live sends
    complaint_rate_pause: float = 0.001
    inbox_dir: str = "inbox"           # webhook spool + IMAP dumps under data_dir; never printed
    outbox_dir: str = "outbox"


class DraftCfg(BaseModel):
    """v0.3 agent drafting (in-harness by default: the CLI emits packets, the agent writes the two slots)."""

    packet_max_tokens: int = 350
    name_policy: str = "gated"         # gated | never | always (owner decision 6)
    max_observation_words: int = 45
    max_subject_chars: int = 60
    # v0.4 autopilot (ADR-015): draft during `run` itself, no separate export/apply round-trip.
    auto: bool = True                       # draft during `run` (autopilot)
    auto_purpose: str = "gainlev_leadgen"   # one of draft.skeletons.PURPOSES
    auto_tiers: list[str] = Field(default_factory=lambda: ["A", "B"])
    auto_max: int = 500                     # targets drafted per run
    template_fallback: bool = True          # deterministic template drafts when no runner is available


class AgentCfg(BaseModel):
    """v0.4 headless agent runner (ADR-015): the operator's own Claude Code in print mode. No API key."""

    command: list[str] | None = None   # None = auto-detect `claude` on PATH; [] = disabled (deterministic fallbacks only)
    model: str = "sonnet"              # used only for the auto-detected command
    timeout_s: int = 900               # per invocation
    batch: int = 40                    # items per invocation
    max_batches: int = 50              # hard stop per stage per run


class PipelineCfg(BaseModel):
    autopilot: bool = True             # run continues through labeling -> score -> draft -> export without pausing


class SocialCfg(BaseModel):
    """Social/video presence signals (U4.8, providers/social.py).

    On by default — is_available() still gates on the tooling actually answering, so absence
    degrades silently. Public, logged-out, business-owned profiles only — never LinkedIn, never
    cookie/session auth (see icm/SCOPE.md and the provider module docstring).
    """

    enabled: bool = True  # auto: is_available() still gates on the agent-reach CLI actually answering
    networks: list[str] = Field(default_factory=lambda: ["youtube"])  # only youtube has a real logged-out backend today
    max_networks: int = 3
    stale_months: int = 6
    timeout_s: float = 30.0
    max_calls_per_min: int = 20


class ExportCfg(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["xlsx", "csv"])
    auto_open: bool = True  # open the finished XLSX with the OS default app (skipped in CI)


class Config(BaseModel):
    data_dir: str = "leadforge_data"
    default_region: str = "US"
    progress_window: bool = True  # headless runs pop a console with the live bar (Windows; skipped in CI)
    discovery: DiscoveryCfg = Field(default_factory=DiscoveryCfg)
    crawl: CrawlCfg = Field(default_factory=CrawlCfg)
    politeness: PolitenessCfg = Field(default_factory=PolitenessCfg)
    enrich: EnrichCfg = Field(default_factory=EnrichCfg)
    validation: ValidationCfg = Field(default_factory=ValidationCfg)
    registry: RegistryCfg = Field(default_factory=RegistryCfg)
    social: SocialCfg = Field(default_factory=SocialCfg)
    export: ExportCfg = Field(default_factory=ExportCfg)
    outreach: OutreachCfg = Field(default_factory=OutreachCfg)
    draft: DraftCfg = Field(default_factory=DraftCfg)
    agent: AgentCfg = Field(default_factory=AgentCfg)
    pipeline: PipelineCfg = Field(default_factory=PipelineCfg)

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
