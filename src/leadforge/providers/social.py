"""Social & video presence signals — ICM unit U4.8 (implemented; youtube via yt-dlp, other networks report 'unknown' until v0.2). On by default, degrades silently.

WHY THIS EXISTS
A business's own website already tells us which social/video profiles it wants customers to find — the
enrichment crawler extracts those links in `enrich/extract.py::extract_socials`. This unit takes only those
self-published, public business profiles and asks: does it exist, is it active, when was the last post?
That converts into strong, honest need signals for outreach:

    stale_social        last post older than `social.stale_months` (default 6)
    no_social_presence  the site links no social profile at all
    no_video_presence   no YouTube/video channel linked (pitch angle for video/marketing offers)
    active_social       posting recently (useful as a NEGATIVE signal for "they need social help")

BACKEND
Agent-Reach (https://github.com/Panniantong/Agent-Reach, `pip install agent-reach`) routes to free OSS
backends for YouTube, Facebook, Instagram, X, Reddit, RSS, GitHub and web search. We shell out to it and read
JSON, exactly like the gosom provider — so nothing heavy enters the agent's context.

=== HARD BOUNDARIES FOR THIS UNIT (icm/SCOPE.md) — do not cross ===
1. LINKEDIN IS EXCLUDED. Agent-Reach can reach it; LeadForge must not. Filter `linkedin` out of every input
   list before calling, and assert this in a test.
2. PUBLIC / LOGGED-OUT ONLY. Agent-Reach supports cookie and browser-session auth for some platforms
   (Twitter, Reddit, Facebook, Instagram, ...). Never configure, pass, or rely on those. If a profile is not
   readable logged-out, record `unknown` and move on.
3. BUSINESS / BRAND ACCOUNTS ONLY — and only ones the business itself links from its own website. Never
   search for or profile a private individual.
4. METADATA, NOT CONTENT. Store existence, last-activity date, follower/subscriber count, and the profile
   URL. Do not archive posts, comments, images, or transcripts into the database.
5. NEVER BLOCKS THE RUN. Missing binary, timeout, rate limit, or any exception -> log and return {} .

=== SPEC (implement exactly; acceptance criteria in icm/stages/stage-4-enrichment.md) ===

def is_available(cfg) -> tuple[bool, str]:
    '''Enabled only when cfg.social.enabled is true AND the CLI answers.
    Run: `agent-reach doctor --json`, timeout 20s, shell=False. Parse JSON; return (True, backend summary)
    when it reports a working backend, else (False, actionable reason).'''

def presence(social_links: dict[str, str], cfg) -> dict:
    '''social_links: {"youtube": url, "facebook": url, ...} straight from extract_socials().
    Returns {network: {"url": str, "exists": bool, "last_post_at": iso|None,
                       "followers": int|None, "status": "ok|unknown|error"}}.
    - Drop "linkedin" first (boundary 1). Cap at cfg.social.max_networks (default 3) per business.
    - One subprocess call per network, timeout cfg.social.timeout_s (default 30), shell=False,
      encoding="utf-8". Reuse the pattern in providers/gosom.py.
    - Respect a module-level rate limiter: no more than cfg.social.max_calls_per_min (default 20).'''

def to_signals(presence_map: dict, cfg) -> list[str]:
    '''Map the presence map onto the signal names listed at the top of this file.
    stale_social when the newest last_post_at across networks is older than cfg.social.stale_months.'''

WIRING
- `enrich/runner.py::_persist`: after socials are extracted, if `social.is_available(cfg)`, call
  `presence(...)` then `to_signals(...)` and merge the result into the business's `enrich_json` under
  `"social_presence"` and add the signal names into `enrich_json["signals"]`.
- `score.py::_need_hits`: the new signals are already honored generically — add them to SOFT_QUALIFIERS in
  models.py and to the `hooks:` table in leadforge/data/scoring.default.yaml (both already have entries prepared).
- Evidence: one `Evidence(fact="social_presence", url=<profile url>, snippet="<network>: last post <date>")`
  per network checked, so the sheet's provenance stays honest.

CONFIG (config.py + leadforge.example.yaml)
  social:
    enabled: true           # on by default; skipped silently when tooling absent
    networks: [youtube]     # only youtube has a real logged-out backend today; linkedin rejected even if listed
    max_networks: 3
    stale_months: 6
    timeout_s: 30
    max_calls_per_min: 20
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

from leadforge.util import LOG

EXCLUDED_NETWORKS = frozenset({"linkedin"})  # boundary 1 — enforced, not advisory


class _RateLimiter:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self._stamps: list[float] = []
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._stamps = [t for t in self._stamps if now - t < 60.0]
            if len(self._stamps) >= self.per_min:
                time.sleep(max(0.0, 60.0 - (now - self._stamps[0])))
            self._stamps.append(time.monotonic())


_LIMITER: _RateLimiter | None = None


def _limiter(cfg) -> _RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = _RateLimiter(cfg.social.max_calls_per_min)
    return _LIMITER


_PROBE_CACHE: tuple[bool, str] | None = None


def is_available(cfg) -> tuple[bool, str]:
    """Enabled only when cfg.social.enabled AND the agent-reach CLI answers its doctor probe.
    The doctor probe is a real subprocess (up to 20s) — cached for the process lifetime."""
    if not cfg.social.enabled:
        return False, "social presence disabled (social.enabled: false)"
    global _PROBE_CACHE
    if _PROBE_CACHE is not None:
        return _PROBE_CACHE
    _PROBE_CACHE = _probe()
    return _PROBE_CACHE


def _probe() -> tuple[bool, str]:
    exe = shutil.which("agent-reach")
    if not exe:
        return False, "agent-reach CLI not installed (pip install agent-reach)"
    try:
        proc = subprocess.run([exe, "doctor", "--json"], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=20, shell=False)
        report = json.loads(proc.stdout or "{}")
        ok = [k for k, v in report.items() if isinstance(v, dict) and v.get("status") == "ok"]
        if ok:
            return True, f"agent-reach backends ok: {', '.join(sorted(ok))}"
        return False, "agent-reach installed but no backend reports ok (run: agent-reach doctor)"
    except Exception as e:  # noqa: BLE001 — availability probe must never raise
        return False, f"agent-reach doctor failed: {type(e).__name__}"


def _youtube_presence(url: str, timeout: float) -> dict:
    """Channel metadata via yt-dlp (agent-reach's youtube backend). Public, logged-out, metadata only."""
    exe = shutil.which("yt-dlp")
    if not exe:
        return {"url": url, "exists": True, "last_post_at": None, "followers": None, "status": "unknown"}
    proc = subprocess.run(
        [exe, "--dump-single-json", "--flat-playlist", "--playlist-items", "1", "--no-warnings", url],
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"url": url, "exists": False, "last_post_at": None, "followers": None, "status": "error"}
    data = json.loads(proc.stdout)
    followers = data.get("channel_follower_count")
    last = None
    entries = data.get("entries") or []
    if entries:
        ts = entries[0].get("timestamp") or entries[0].get("release_timestamp")
        if ts:
            last = datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
        elif entries[0].get("upload_date"):
            d = entries[0]["upload_date"]
            last = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return {"url": url, "exists": True, "last_post_at": last, "followers": followers, "status": "ok"}


def presence(social_links: dict[str, str], cfg) -> dict:
    """{network: {url, exists, last_post_at, followers, status}} for the business's own linked profiles.

    Logged-out public metadata only. Networks without a logged-out backend are recorded 'unknown' —
    the link's existence is still a fact (the site publishes it).
    """
    out: dict[str, dict] = {}
    links = filter_networks(social_links)
    wanted = [n for n in cfg.social.networks if n in links][: cfg.social.max_networks]
    for net in wanted:
        url = links[net]
        try:
            _limiter(cfg).wait()
            if net == "youtube":
                out[net] = _youtube_presence(url, cfg.social.timeout_s)
            else:
                out[net] = {"url": url, "exists": True, "last_post_at": None,
                            "followers": None, "status": "unknown"}
        except Exception as e:  # noqa: BLE001 — never blocks the run
            LOG.warning("social presence failed for %s: %s", net, type(e).__name__)
            out[net] = {"url": url, "exists": False, "last_post_at": None, "followers": None, "status": "error"}
    return out


def to_signals(presence_map: dict, cfg) -> list[str]:
    signals: list[str] = []
    if not presence_map:
        return ["no_social_presence"]
    if "youtube" not in presence_map:
        signals.append("no_video_presence")
    dates = [p["last_post_at"] for p in presence_map.values() if p.get("last_post_at")]
    if dates:
        newest = max(datetime.fromisoformat(d).replace(tzinfo=UTC) for d in dates)
        stale_cutoff = datetime.now(tz=UTC) - timedelta(days=30 * cfg.social.stale_months)
        signals.append("stale_social" if newest < stale_cutoff else "active_social")
    return signals


def filter_networks(social_links: dict[str, str]) -> dict[str, str]:
    """Drop networks LeadForge will never touch. Used by presence(); tested independently."""
    return {k: v for k, v in social_links.items() if k.lower() not in EXCLUDED_NETWORKS}
