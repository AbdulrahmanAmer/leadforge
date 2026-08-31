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


_LEGAL_SUFFIX_RE = re.compile(r"^(inc|llc|ltd|llp|plc|co|corp|corporation|gmbh|sa|bv)\.?$", re.IGNORECASE)


def natural_name(raw: str) -> str:
    """'SURNAME, Given Names' (how corporate registries list officers) -> 'Given Names Surname'.
    Anything not person-shaped passes through unchanged: no comma, two commas, or a legal
    suffix after the comma ('Acme Widgets, Inc'). Casing is left to the caller."""
    raw = (raw or "").strip()
    if raw.count(",") == 1:
        last, _, given = raw.partition(",")
        last, given = last.strip(), given.strip()
        if last and given and not _LEGAL_SUFFIX_RE.match(given):
            return f"{given} {last}"
    return raw


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


# --- progress heartbeat (v0.1.3) -------------------------------------------------------------
# Two mutually exclusive channels, chosen by where output is going:
#  - agent/pipe (stdout not a TTY): bounded `LF_PROGRESS {json}` lines on stdout (docs/06)
#  - human terminal (stderr a TTY): ONE in-place animated bar with %, ETA and live status;
#    each finished stage collapses to a permanent one-line summary above the bar (the history).
_PROG = {"stage": None, "start": 0.0, "spin": 0}
_SPINNER = "|/-\\"
_CLEAR = "\r\x1b[2K"
_PROGRESS_FILE: str | None = None


def set_progress_file(path) -> None:
    """Mirror progress events to a JSONL file so `leadforge watch` (and the auto-opened progress
    window) can render a live bar for runs launched headless by an agent."""
    global _PROGRESS_FILE
    _PROGRESS_FILE = str(path)
    try:
        from pathlib import Path as _P
        _P(path).write_text("", encoding="utf-8")  # fresh run, fresh feed
    except OSError:
        pass


def _gui_ok() -> bool:
    import os as _os
    return not (_os.environ.get("CI") or _os.environ.get("LEADFORGE_NO_UI"))


def open_progress_window(workspace) -> None:
    """Pop a console running `leadforge watch` so a human can see the live bar for a run an
    agent launched headless. Windows-only; no-op elsewhere, in CI, or when LEADFORGE_NO_UI is set."""
    import os as _os
    import subprocess as _sp
    import sys as _sys
    if not _gui_ok() or _os.name != "nt" or _os.environ.get("LEADFORGE_WATCH_CHILD"):
        return
    try:
        env = dict(_os.environ, LEADFORGE_WATCH_CHILD="1")
        _sp.Popen([_sys.executable, "-m", "leadforge", "watch"], cwd=str(workspace), env=env,
                  creationflags=_sp.CREATE_NEW_CONSOLE)
    except Exception as e:  # noqa: BLE001 — a UI nicety must never break the pipeline
        LOG.debug("progress window failed: %s", type(e).__name__)


def open_artifact(path) -> None:
    """Open a finished export with the OS default app. No-op in CI / with LEADFORGE_NO_UI."""
    import os as _os
    import subprocess as _sp
    import sys as _sys
    if not _gui_ok():
        return
    try:
        if _os.name == "nt":
            _os.startfile(str(path))  # noqa: S606
        elif _sys.platform == "darwin":
            _sp.Popen(["open", str(path)])
        else:
            _sp.Popen(["xdg-open", str(path)])
    except Exception as e:  # noqa: BLE001
        LOG.debug("auto-open failed: %s", type(e).__name__)


def _fmt_secs(sec: float) -> str:
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


def emit_progress(stage: str, done: int, total: int | None, msg: str = "") -> None:
    import json as _json
    import sys as _sys

    try:
        human = _sys.stderr.isatty()
    except Exception:  # noqa: BLE001
        human = False

    payload = {"stage": stage, "done": done, "total": total, "msg": msg[:120]}
    if _PROGRESS_FILE:
        try:
            with open(_PROGRESS_FILE, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    if not human:
        print("LF_PROGRESS " + _json.dumps(payload, ensure_ascii=False), flush=True)
        return
    render_progress_line(stage, done, total, msg)


def render_progress_line(stage: str, done: int, total: int | None, msg: str = "") -> None:
    """The human-facing in-place bar (used by emit_progress on a TTY and by `leadforge watch`)."""
    import sys as _sys
    import time as _time

    try:
        now = _time.monotonic()
        if _PROG["stage"] != stage:
            if _PROG["stage"] is not None:  # previous stage becomes a permanent history line
                _sys.stderr.write(_CLEAR + f"\x1b[32m[done]\x1b[0m {_PROG['stage']} "
                                  f"({_fmt_secs(now - _PROG['start'])})\n")
            _PROG.update(stage=stage, start=now)
        _PROG["spin"] = (_PROG["spin"] + 1) % len(_SPINNER)
        spin = _SPINNER[_PROG["spin"]]
        elapsed = now - _PROG["start"]
        if total:
            pct = min(100, round(100 * done / max(1, total)))
            width = 22
            filled = min(width, round(width * done / max(1, total)))
            bar = "\x1b[36m" + "█" * filled + "\x1b[90m" + "░" * (width - filled) + "\x1b[0m"
            eta = f" ~{_fmt_secs(elapsed / done * (total - done))} left" if 0 < done < total else ""
            line = (f"{spin} \x1b[1m{stage}\x1b[0m {bar} {pct:3d}% ({done}/{total})"
                    f"  {msg[:46]}  {_fmt_secs(elapsed)}{eta}")
        else:
            line = f"{spin} \x1b[1m{stage}\x1b[0m  {done} done  {msg[:56]}  {_fmt_secs(elapsed)}"
        _sys.stderr.write(_CLEAR + line)
        if total and done >= total:
            _sys.stderr.write(_CLEAR + f"\x1b[32m[done]\x1b[0m {stage} {total}/{total} "
                              f"({_fmt_secs(elapsed)})\n")
            _PROG["stage"] = None
        _sys.stderr.flush()
    except Exception:  # noqa: BLE001 — a broken terminal must never break the pipeline
        pass


