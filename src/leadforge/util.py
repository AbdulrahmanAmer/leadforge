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
_PROG: dict = {"stage": None, "spin": 0, "hist": []}  # hist: [(ts, done, total)] for the current stage
_RATE_WINDOW = 12  # completions used for the moving rate (responsive to stalls, stable enough to trust)


def progress_estimate(hist: list[tuple[float, int, int]], now: float) -> dict:
    """Honest ETA math from the FEED's own timestamps, so every watcher (and the run itself) shows the
    same numbers — a window attached mid-run used to divide its own short uptime by whatever it had
    seen, and two windows disagreed by hours. `hist` = [(ts, done, total)] with one entry per change of
    `done`; `total` may grow while the stage runs (saturated tiles subdivide), which the plain
    remaining/rate formula reports instead of hiding.

    -> {elapsed_s, done, total, growth, rate_per_s|None, per_item_s|None, eta_s|None}"""
    if not hist:
        return {"elapsed_s": 0.0, "done": 0, "total": None, "growth": 0, "rate_per_s": None,
                "per_item_s": None, "eta_s": None}
    t0, d0, total0 = hist[0]
    tn, dn, totaln = hist[-1]
    elapsed = max(0.0, now - t0)
    window = hist[-_RATE_WINDOW:]
    rate = None
    if len(window) >= 2 and window[-1][0] > window[0][0] and window[-1][1] > window[0][1]:
        rate = (window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0])
    elif len(hist) >= 2 and tn > t0 and dn > d0:
        rate = (dn - d0) / (tn - t0)
    eta = None
    if rate and totaln:
        eta = max(0.0, (totaln - dn) / rate)
    return {"elapsed_s": elapsed, "done": dn, "total": totaln, "growth": (totaln or 0) - (total0 or 0),
            "rate_per_s": rate, "per_item_s": (1.0 / rate) if rate else None, "eta_s": eta}
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


_WINDOW_SPAWNED = False


def open_progress_window(workspace, data_dir=None) -> None:
    """Pop a console running `leadforge watch` so a human can see the live bar for a run an
    agent launched headless. Windows-only; no-op elsewhere, in CI, or when LEADFORGE_NO_UI is set.
    Once per process — run_pipeline and run_discover both call this on the same run."""
    global _WINDOW_SPAWNED
    import os as _os
    import subprocess as _sp
    import sys as _sys
    if _WINDOW_SPAWNED or not _gui_ok() or _os.name != "nt" or _os.environ.get("LEADFORGE_WATCH_CHILD"):
        return
    try:
        env = dict(_os.environ, LEADFORGE_WATCH_CHILD="1")
        # pass the resolved data dir through, or a --data-dir run's window tails the wrong feed
        args = [_sys.executable, "-m", "leadforge"]
        if data_dir is not None:
            args += ["--data-dir", str(data_dir)]
        _sp.Popen([*args, "watch"], cwd=str(workspace), env=env,
                  creationflags=_sp.CREATE_NEW_CONSOLE)
        _WINDOW_SPAWNED = True
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
    import time as _time

    try:
        human = _sys.stderr.isatty()
    except Exception:  # noqa: BLE001
        human = False

    ts = round(_time.time(), 1)  # v0.3.1: the feed carries its own clock so watchers agree on the ETA
    payload = {"stage": stage, "done": done, "total": total, "msg": msg[:120], "ts": ts}
    if _PROGRESS_FILE:
        try:
            with open(_PROGRESS_FILE, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    if not human:
        print("LF_PROGRESS " + _json.dumps(payload, ensure_ascii=False), flush=True)
        return
    render_progress_line(stage, done, total, msg, ts=ts)


def progress_summary(stage: str, hist: list[tuple[float, int, int]], now: float) -> str:
    """One plain line for a watcher that just attached: where the stage is, since when, at what pace."""
    import time as _time

    est = progress_estimate(hist, now)
    if not hist:
        return f"{stage}: no progress recorded yet"
    since = _time.strftime("%H:%M", _time.localtime(hist[0][0]))
    parts = [f"{stage}: {est['done']}/{est['total']} done since {since} ({_fmt_secs(est['elapsed_s'])} elapsed)"]
    if est["per_item_s"]:
        parts.append(f"avg {_fmt_secs(est['per_item_s'])} per item over the last {min(len(hist), _RATE_WINDOW)}")
    if est["eta_s"] is not None:
        parts.append(f"~{_fmt_secs(est['eta_s'])} left at this pace")
    if est["growth"]:
        parts.append(f"total grew +{est['growth']} (saturated tiles split); the ETA moves with it")
    return " · ".join(parts)


def render_progress_line(stage: str, done: int, total: int | None, msg: str = "", ts: float | None = None) -> None:
    """The human-facing in-place bar (used by emit_progress on a TTY and by `leadforge watch`).

    `ts` is the feed's own timestamp for this event (seconds since the epoch). Elapsed, pace and ETA are
    computed from the feed's timestamps (progress_estimate), never from this process's uptime, so a
    watcher attached at any moment shows the same figures as the run itself."""
    import sys as _sys
    import time as _time

    try:
        now = float(ts) if ts is not None else _time.time()
        if _PROG["stage"] != stage:
            if _PROG["stage"] is not None and _PROG["hist"]:  # previous stage becomes a permanent history line
                prev = progress_estimate(_PROG["hist"], now)
                _sys.stderr.write(_CLEAR + f"\x1b[32m[done]\x1b[0m {_PROG['stage']} "
                                  f"({_fmt_secs(prev['elapsed_s'])})\n")
            _PROG.update(stage=stage, hist=[])
        hist: list = _PROG["hist"]
        if not hist or hist[-1][1] != done or hist[-1][2] != total:
            hist.append((now, done, total or 0))
            del hist[:-500]
        _PROG["spin"] = (_PROG["spin"] + 1) % len(_SPINNER)
        spin = _SPINNER[_PROG["spin"]]
        est = progress_estimate(hist, now)
        elapsed = est["elapsed_s"]
        if total:
            pct = min(100, round(100 * done / max(1, total)))
            width = 22
            filled = min(width, round(width * done / max(1, total)))
            bar = "\x1b[36m" + "█" * filled + "\x1b[90m" + "░" * (width - filled) + "\x1b[0m"
            growth = f" +{est['growth']}" if est["growth"] > 0 else ""
            pace = f" · {_fmt_secs(est['per_item_s'])}/item" if est["per_item_s"] else ""
            eta = f" · ~{_fmt_secs(est['eta_s'])} left" if est["eta_s"] is not None and done < total else ""
            line = (f"{spin} \x1b[1m{stage}\x1b[0m {bar} {pct:3d}% ({done}/{total}{growth})"
                    f"  {msg[:40]}  {_fmt_secs(elapsed)}{pace}{eta}")
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


