"""gosom/google-maps-scraper adapter (U3.3) — the PRIMARY discovery engine (ADR-001).

Invocation mode: one subprocess per query batch, JSON output file, conservative flags.
Field reference (36 fields) + flags verified 2026-08-31 against the v1.17.4 README (docs/01 §2).

FIELD_MAP drift is the expected maintenance surface: when Google or gosom changes output,
fix the map + refresh tests/fixtures/gosom_sample.ndjson from a real run (ICM U8.2).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from leadforge import GOSOM_VERSION
from leadforge.doctor import gosom_path, gosom_runs
from leadforge.grid import PlannedQuery
from leadforge.models import RawListing
from leadforge.providers.base import DiscoveryProvider, register
from leadforge.util import LOG, ProviderDegraded, now_iso, sha1_hex

# stderr fragments that mean "back off", not "broken"
_CAPTCHA_MARKERS = ("sorry/index", "captcha", "unusual traffic", "consent")
_COOLDOWN_S = 600


@register
class GosomProvider(DiscoveryProvider):
    name = "gosom"
    supports_tiles = True  # native -grid-bbox/-grid-cell

    def available(self) -> tuple[bool, str]:
        path = gosom_path(self.cfg)
        if not path.exists():
            return False, f"binary missing ({path.name}) — run: leadforge doctor --fix"
        if not gosom_runs(path):
            return False, "binary present but not runnable"
        return True, f"v{GOSOM_VERSION}"

    def fetch(self, query: PlannedQuery, limit: int | None = None) -> list[RawListing]:
        d = self.cfg.discovery
        out_path = self.cfg.cache_dir / f"gosom_{sha1_hex(query.text + str(query.tile), 8)}.json"
        qfile = out_path.with_suffix(".queries.txt")
        qfile.write_text(query.text + "\n", encoding="utf-8")

        # A small --limit is a smoke test: one results page (~20 listings) is enough; full depth
        # can run 30+ minutes because gosom visits every place page.
        depth = min(d.depth, 1) if (limit and limit <= 20) else d.depth
        args = [
            str(gosom_path(self.cfg)),
            "-input", str(qfile),
            "-results", str(out_path),
            "-json",
            "-depth", str(depth),
            "-c", str(d.concurrency),
            "-lang", d.lang,
            "-exit-on-inactivity", "3m",
        ]
        if d.email_crawl:
            args.append("-email")
        if d.proxies:
            args += ["-proxies", ",".join(d.proxies)]
        if query.tile is not None and d.grid_mode == "auto":
            # Tile.bbox is GeoJSON order (minLng,minLat,maxLng,maxLat); gosom -grid-bbox wants
            # 'minLat,minLon,maxLat,maxLon' (verified against v1.17.4 -h) — swap or we scrape
            # a box on the wrong continent.
            b = query.tile.bbox
            args += ["-grid-bbox", f"{b[1]},{b[0]},{b[3]},{b[2]}", "-grid-cell", str(query.tile.cell_km)]

        LOG.info("gosom fetch: %s", query.text)
        proc, timed_out = self._run_with_watchdog(args, out_path, d.timeout_min * 60,
                                                  stall_s=d.stall_s)
        # Captcha classification first — it applies whether gosom exited or was killed for stalling
        # (a consent wall is a common cause of an empty stall).
        stderr_tail = (proc["stderr"] or "")[-2000:].lower()
        captcha = any(m in stderr_tail for m in _CAPTCHA_MARKERS)
        if timed_out:
            # Salvage whatever gosom already wrote — a stall after N minutes usually means
            # dozens of complete listings are sitting in the NDJSON file.
            salvaged = list(self._parse(out_path))
            if salvaged:
                LOG.warning("gosom stalled on '%s' — salvaged %d listings from written output",
                            query.text, len(salvaged))
                return salvaged[:limit] if limit else salvaged
            if captcha:
                LOG.warning("gosom captcha/consent stall; cooling down")
                time.sleep(min(_COOLDOWN_S, 30 if limit else _COOLDOWN_S))
                raise ProviderDegraded(f"captcha/cooldown on '{query.text}'")
            raise ProviderDegraded(f"gosom produced nothing before stalling on '{query.text}'")

        if captcha:
            LOG.warning("gosom captcha/consent signals; cooling down %ss", _COOLDOWN_S)
            time.sleep(min(_COOLDOWN_S, 30 if limit else _COOLDOWN_S))  # short cooldown under --limit smoke tests
            raise ProviderDegraded(f"captcha/cooldown on '{query.text}'")
        if proc["returncode"] != 0 and not out_path.exists():
            raise ProviderDegraded(f"gosom exit {proc['returncode']} on '{query.text}': {stderr_tail[:200]}")

        listings = list(self._parse(out_path))
        if limit:
            listings = listings[:limit]
        LOG.info("gosom got %d listings for '%s'", len(listings), query.text)
        return listings

    @staticmethod
    def _run_with_watchdog(args: list[str], out_path: Path, hard_timeout_s: float,
                           stall_s: float = 180.0, poll_s: float = 5.0) -> tuple[dict, bool]:
        """Run gosom, but don't trust it to exit: v1.17.4 reliably hangs after writing all results
        (observed live; -exit-on-inactivity never fires). When the results file has content and has
        stopped growing for stall_s, terminate and let the caller salvage. Returns ({returncode,
        stderr}, timed_out) — timed_out True when we had to kill it.

        stderr goes to a temp file, not a pipe: a chatty child would fill the OS pipe buffer and
        block, which this loop would misread as a stall — and the tail must survive a kill so the
        captcha classifier still sees it."""
        err_path = out_path.with_suffix(".stderr.log")
        with open(err_path, "w", encoding="utf-8", errors="replace") as err_fh:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=err_fh, shell=False)
            start = time.monotonic()
            last_size = -1
            last_growth = start
            killed = False
            while True:
                try:
                    proc.wait(timeout=poll_s)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                size = out_path.stat().st_size if out_path.exists() else 0
                if size != last_size:
                    last_size, last_growth = size, now
                stalled_with_output = size > 0 and (now - last_growth) >= stall_s
                if stalled_with_output or (now - start) >= hard_timeout_s:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)
                    killed = True
                    break
        try:
            stderr_tail = err_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            err_path.unlink()
        except OSError:
            stderr_tail = ""
        return {"returncode": -1 if killed else proc.returncode, "stderr": stderr_tail}, killed

    def _parse(self, out_path: Path):
        if not out_path.exists():
            return
        text = out_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return
        rows: list[dict] = []
        if text.startswith("["):
            try:
                rows = json.loads(text)
            except json.JSONDecodeError:
                rows = []
        else:  # NDJSON
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    LOG.debug("skipping unparseable gosom line: %.80s", line)
        for row in rows:
            if isinstance(row, dict):
                yield RawListing(provider=self.name, fetched_at=now_iso(), data=row)


# Documented gosom -> canonical key hints, used by normalize.py (single source: keep here).
GOSOM_FIELD_MAP = {
    "name": ["title"],
    "category": ["category"],
    "categories": ["categories"],
    "address": ["address"],
    "complete_address": ["complete_address"],  # dict: street/borough/city/postal_code/state/country
    "phone": ["phone"],
    "website": ["web_site", "website"],
    "rating": ["review_rating", "rating"],
    "review_count": ["review_count", "reviews"],
    "lat": ["latitude"],
    "lng": ["longitude"],
    "place_id": ["place_id"],
    "cid": ["cid"],
    "hours": ["open_hours"],
    "maps_url": ["link", "url"],
}
GosomProvider.FIELD_MAP = GOSOM_FIELD_MAP  # v0.3: registered for normalize's per-provider dispatch

from leadforge.providers.base import register_field_map as _register_field_map  # noqa: E402

_register_field_map(GosomProvider.name, GOSOM_FIELD_MAP)
