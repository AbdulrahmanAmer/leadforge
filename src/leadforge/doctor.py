"""Environment doctor & self-bootstrap (U0.5).

Checks the machine BEFORE any pipeline work and (with --fix) installs what is missing:
- Python >= 3.11, core imports, writable data dirs, disk space
- pinned gosom google-maps-scraper binary (auto-download per-OS release asset -> leadforge_data/bin/)
- network + DNS reachability
- optional extras present (reported, never required)

A 24h ok-stamp keeps repeat checks cheap; pipeline commands call ensure_ready().
"""

from __future__ import annotations

import json
import platform
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from leadforge import GOSOM_VERSION
from leadforge.config import Config
from leadforge.util import LOG, EnvError, now_iso

GOSOM_REPO = "gosom/google-maps-scraper"
STAMP_NAME = ".doctor_ok.json"
CORE_IMPORTS = [
    "typer", "pydantic", "yaml", "httpx", "selectolax", "trafilatura",
    "phonenumbers", "email_validator", "dns", "pyap", "tenacity", "openpyxl",
]
OPTIONAL_EXTRAS = {"crawl4ai": "browser", "gliner": "ner", "usaddress": "addressing"}


@dataclass
class CheckResult:
    name: str
    ok: bool
    fixed: bool = False
    msg: str = ""
    hint: str = ""


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def counts(self) -> dict:
        return {
            "checks": len(self.results),
            "fixed": sum(r.fixed for r in self.results),
            "failed": sum(not r.ok for r in self.results),
        }

    def lines(self) -> list[str]:
        out = []
        for r in self.results:
            tag = "fixed" if r.fixed else ("ok" if r.ok else "FAIL")
            line = f"[{tag}] {r.name}" + (f" — {r.msg}" if r.msg else "")
            if not r.ok and r.hint:
                line += f" | fix: {r.hint}"
            out.append(line)
        return out


# ------------------------------------------------------------------ gosom binary management
def gosom_asset_name() -> str:
    osname = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(platform.system(), "linux")
    arch = platform.machine().lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(arch, "amd64")
    ext = ".exe" if osname == "windows" else ""
    return f"google_maps_scraper-{GOSOM_VERSION}-{osname}-{arch}{ext}"


def gosom_path(cfg: Config) -> Path:
    return cfg.bin_dir / gosom_asset_name()


def _release_assets(client: httpx.Client) -> list[dict]:
    r = client.get(
        f"https://api.github.com/repos/{GOSOM_REPO}/releases/tags/v{GOSOM_VERSION}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("assets", [])


def download_gosom(cfg: Config) -> Path:
    """Download the pinned release asset. Direct URL first; GitHub API fuzzy-match as fallback
    (covers arm64 machines where only amd64 assets exist -> emulation/rosetta)."""
    dest = gosom_path(cfg)
    if dest.exists():
        return dest
    asset = gosom_asset_name()
    urls = [f"https://github.com/{GOSOM_REPO}/releases/download/v{GOSOM_VERSION}/{asset}"]
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        try:
            names = {a["name"]: a["browser_download_url"] for a in _release_assets(client)}
            if asset not in names:  # fuzzy: same OS, prefer amd64
                osname = asset.split("-")[-2].split(".")[0] if "windows" not in asset else "windows"
                cand = [u for n, u in names.items() if osname in n]
                urls = cand[:1] + urls
        except httpx.HTTPError as e:
            LOG.warning("GitHub API asset listing failed (%s); trying direct URL", e)
        last_err: Exception | None = None
        for url in urls:
            try:
                LOG.info("downloading gosom binary: %s", url)
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_bytes(1 << 16):
                            fh.write(chunk)
                tmp.rename(dest)
                if platform.system() != "Windows":
                    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
                return dest
            except httpx.HTTPError as e:
                last_err = e
    raise EnvError(f"binary download failed: {asset} ({last_err}). Manually place it at {dest} from "
                   f"https://github.com/{GOSOM_REPO}/releases/tag/v{GOSOM_VERSION}")


def gosom_runs(path: Path) -> bool:
    try:
        proc = subprocess.run([str(path), "-h"], capture_output=True, timeout=20, encoding="utf-8", errors="replace")
        return proc.returncode in (0, 2)  # go flag package exits 2 on -h
    except (OSError, subprocess.TimeoutExpired):
        return False


# ------------------------------------------------------------------ checks
def install_quality_extras(rep_add) -> None:
    """--fix --full: install the enrichment-quality extras ([ner] GLiNER, [browser] crawl4ai)
    so a fresh machine's first bootstrap yields full-quality sheets, not the heuristic floor."""
    root = _repo_root()
    for extra, probe_mod, post in (("ner", "gliner", None), ("browser", "crawl4ai", "crawl4ai-setup")):
        already = _importable(probe_mod)
        if not already:
            proc = subprocess.run([sys.executable, "-m", "pip", "install", "-e", f"{root}[{extra}]"],
                                  capture_output=True, encoding="utf-8", errors="replace")
            ok = proc.returncode == 0 and _importable(probe_mod)
        else:
            ok = True
        if ok and post and shutil.which(post):
            subprocess.run([shutil.which(post)], capture_output=True, encoding="utf-8",
                           errors="replace", timeout=600)
        rep_add(CheckResult(f"extra-{extra}", ok, fixed=ok and not already,
                            msg="installed" if ok else "install failed",
                            hint="" if ok else f"pip install -e {root}[{extra}]"))


def run_doctor(cfg: Config, fix: bool = False, strict: bool = False, full: bool = False) -> DoctorReport:
    rep = DoctorReport()
    add = rep.results.append
    if fix and full:
        install_quality_extras(add)

    ver = sys.version_info
    add(CheckResult("python>=3.11", ver >= (3, 11), msg=f"{ver.major}.{ver.minor}.{ver.micro}",
                    hint="install Python 3.11+ and reinstall: pip install -e ."))

    missing = [mod for mod in CORE_IMPORTS if not _importable(mod)]
    had_missing = bool(missing)
    if missing and fix:
        subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(_repo_root())],
                       capture_output=True, encoding="utf-8", errors="replace")
        missing = [m for m in missing if not _importable(m)]
    add(CheckResult("core-imports", not missing, fixed=had_missing and not missing,
                    msg="all present" if not missing else f"missing: {', '.join(missing)}",
                    hint="pip install -e <repo-root>"))

    try:
        for d in (cfg.data_path, cfg.bin_dir, cfg.cache_dir, cfg.exports_dir, cfg.logs_dir):
            probe = d / ".w"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        add(CheckResult("data-dirs-writable", True, msg=str(cfg.data_path)))
    except OSError as e:
        add(CheckResult("data-dirs-writable", False, msg=str(e), hint="check permissions / --data-dir"))

    free_gb = shutil.disk_usage(cfg.data_path).free / 1e9
    add(CheckResult("disk>=2GB", free_gb >= 2, msg=f"{free_gb:.1f} GB free", hint="free disk space"))

    bin_path = gosom_path(cfg)
    fixed = False
    if not (bin_path.exists() and gosom_runs(bin_path)):
        if fix:
            try:
                download_gosom(cfg)
                fixed = gosom_runs(bin_path)
            except EnvError as e:
                add(CheckResult("gosom-binary", False, msg=str(e),
                                hint="download the asset manually into leadforge_data/bin/"))
            else:
                add(CheckResult("gosom-binary", fixed, fixed=fixed, msg=f"v{GOSOM_VERSION} -> {bin_path.name}",
                                hint="manual download; see message"))
        else:
            add(CheckResult("gosom-binary", False, msg=f"missing: {bin_path.name}", hint="leadforge doctor --fix"))
    else:
        add(CheckResult("gosom-binary", True, msg=f"v{GOSOM_VERSION} present"))

    try:
        with httpx.Client(timeout=8) as client:
            client.head("https://github.com")
        add(CheckResult("network", True, msg="https reachable"))
    except httpx.HTTPError as e:
        add(CheckResult("network", False, msg=type(e).__name__, hint="check connectivity/proxy"))

    try:
        from leadforge.enrich.validate import get_resolver
        res = get_resolver()
        res.resolve("gmail.com", "MX", lifetime=cfg.validation.dns_timeout_s)
        add(CheckResult("dns-mx", True, msg=f"resolver ok ({', '.join(map(str, res.nameservers[:2]))})"))
    except Exception as e:  # noqa: BLE001 — any resolver failure is the same answer
        add(CheckResult("dns-mx", False, msg=type(e).__name__,
                        hint="email tiers will be 'unknown' until DNS works"))

    extras = [f"{tag}({mod})" for mod, tag in OPTIONAL_EXTRAS.items() if _importable(mod)]
    add(CheckResult("optional-extras", True, msg=", ".join(extras) if extras else
                    "none installed (fine) — [browser]/[ner]/[addressing] available"))

    if rep.ok:
        _write_stamp(cfg)
    if strict and not rep.ok:
        raise EnvError("; ".join(r.name for r in rep.results if not r.ok))
    return rep


def _importable(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def _repo_root() -> Path:
    # src/leadforge/doctor.py -> repo root
    return Path(__file__).resolve().parents[2]


def _write_stamp(cfg: Config) -> None:
    (cfg.data_path / STAMP_NAME).write_text(
        json.dumps({"at": now_iso(), "t": time.time(), "gosom": GOSOM_VERSION}), encoding="utf-8"
    )


def ensure_ready(cfg: Config, max_age_h: float = 24.0) -> None:
    """Cheap gate for pipeline commands: trust a recent ok-stamp, else run a fixing doctor."""
    stamp = cfg.data_path / STAMP_NAME
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
        if time.time() - float(data["t"]) < max_age_h * 3600 and data.get("gosom") == GOSOM_VERSION:
            return
    except (OSError, ValueError, KeyError):
        pass
    rep = run_doctor(cfg, fix=True)
    if not rep.ok:
        failed = "; ".join(f"{r.name} ({r.hint})" for r in rep.results if not r.ok)
        raise EnvError(f"environment not ready: {failed}")
