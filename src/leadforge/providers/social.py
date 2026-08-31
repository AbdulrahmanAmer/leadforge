"""Social & video presence signals via Agent-Reach — ICM unit U4.8 (STUB with binding spec). Opt-in.

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
  models.py and to the `hooks:` table in config/scoring.default.yaml (both already have entries prepared).
- Evidence: one `Evidence(fact="social_presence", url=<profile url>, snippet="<network>: last post <date>")`
  per network checked, so the sheet's provenance stays honest.

CONFIG (add to config.py + leadforge.example.yaml, all defaults OFF/conservative)
  social:
    enabled: false          # master switch; nothing runs unless true
    networks: [youtube, facebook, instagram]   # linkedin is rejected even if listed
    max_networks: 3
    stale_months: 6
    timeout_s: 30
    max_calls_per_min: 20
"""

from __future__ import annotations

EXCLUDED_NETWORKS = frozenset({"linkedin"})  # boundary 1 — enforced, not advisory


def is_available(cfg) -> tuple[bool, str]:
    return False, "U4.8 not implemented yet — see module docstring spec"


def presence(social_links: dict[str, str], cfg) -> dict:
    raise NotImplementedError("U4.8: implement per module docstring spec")


def to_signals(presence_map: dict, cfg) -> list[str]:
    raise NotImplementedError("U4.8: implement per module docstring spec")


def filter_networks(social_links: dict[str, str]) -> dict[str, str]:
    """Drop networks LeadForge will never touch. Used by presence(); tested independently."""
    return {k: v for k, v in social_links.items() if k.lower() not in EXCLUDED_NETWORKS}
