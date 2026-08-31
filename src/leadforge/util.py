"""Cross-cutting helpers (U0.4): logging, digest protocol, politeness throttle, small utilities.

The digest protocol is the agent-facing contract (docs/06): every command ends with exactly one
`LF_DIGEST {compact json}` line on stdout. Keep stdout otherwise terse; detail goes to the logfile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
import unicodedata
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

LOG = logging.getLogger("leadforge")


# --- errors (docs/04 §4) --------------------------------------------------------------------
class LeadForgeError(Exception):
    exit_code = 1


class EnvError(LeadForgeError):
    exit_code = 3


class InputError(LeadForgeError):
    exit_code = 4


class ProviderFailed(LeadForgeError):
    exit_code = 5


class ProviderDegraded(Exception):
    """Non-fatal provider trouble: record + continue."""


# --- logging --------------------------------------------------------------------------------
def setup_logging(logs_dir: Path, verbose: bool = False) -> None:
    if LOG.handlers:
        return
    LOG.setLevel(logging.DEBUG)
    fh = RotatingFileHandler(logs_dir / "leadforge.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.addHandler(fh)


# --- digest ---------------------------------------------------------------------------------
def emit_digest(
    ok: bool,
    cmd: str,
    run: str | None = None,
    counts: dict | None = None,
    warnings: list[str] | None = None,
    artifacts: list[str] | None = None,
    next_: str | None = None,
) -> None:
    payload = {
        "ok": ok,
        "cmd": cmd,
        "run": run,
        "counts": counts or {},
        "warnings": (warnings or [])[:5],
        "artifacts": artifacts or [],
        "next": next_,
    }
    print("LF_DIGEST " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)


# --- politeness -----------------------------------------------------------------------------
class HostThrottle:
    """Per-host pacing: >= delay_s (+/- jitter) between requests to the same host, thread-safe."""

    def __init__(self, delay_s: float, jitter: float = 0.3):
        self.delay_s = delay_s
        self.jitter = jitter
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                ready = self._next.get(host, 0.0)
                if now >= ready:
                    delay = self.delay_s * (1 + random.uniform(-self.jitter, self.jitter))
                    self._next[host] = now + max(0.2, delay)
                    return
                sleep_for = ready - now
            time.sleep(min(sleep_for, 1.0))


# --- small utilities ------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha1_hex(text: str, n: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "x"


# Minimal multi-label public suffixes we care about for apex-domain extraction without a PSL dep.
_CC_SLD = {
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz",
    "com.br", "com.mx", "co.za", "com.sg", "com.hk", "co.jp", "co.in", "com.eg",
}


def apex_domain(url_or_host: str) -> str | None:
    """Best-effort apex domain ('www.foo.co.uk/x' -> 'foo.co.uk'). Returns None for IPs/empty."""
    host = url_or_host.strip().lower()
    if "//" in host:
        host = urlsplit(host).netloc or host
    host = host.split("@")[-1].split(":")[0].strip(".")
    if not host or re.fullmatch(r"[\d.]+", host):
        return None
    labels = host.split(".")
    if len(labels) < 2:
        return None
    tail2 = ".".join(labels[-2:])
    if tail2 in _CC_SLD and len(labels) >= 3:
        return ".".join(labels[-3:])
    return tail2


SOCIAL_HOSTS = {
    "facebook.com": "facebook", "m.facebook.com": "facebook", "instagram.com": "instagram",
    "linkedin.com": "linkedin", "x.com": "x", "twitter.com": "x", "youtube.com": "youtube",
    "tiktok.com": "tiktok", "pinterest.com": "pinterest", "yelp.com": "yelp", "wa.me": "whatsapp",
}


def social_network(url: str) -> str | None:
    apex = apex_domain(url)
    if apex is None:
        return None
    host = urlsplit(url if "//" in url else "https://" + url).netloc.lower().removeprefix("www.")
    return SOCIAL_HOSTS.get(host) or SOCIAL_HOSTS.get(apex)
