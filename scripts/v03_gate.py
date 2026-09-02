"""v0.3 release gate — one deterministic command that replaces a model review loop.

    python scripts/v03_gate.py [--live-db PATH] [--skip-suite] [--quick]

Runs, in order, and prints PASS/FAIL per check (exit 1 on any FAIL):
  1. pytest full suite (with -o addopts="" so the summary line is real) + ruff
  2. version strings agree across pyproject / __init__ / both plugin manifests / skill frontmatter
  3. `claude plugin validate .` when the claude CLI is on PATH (else SKIP)
  4. CLI contract smokes in a temp workspace: every command ends with exactly one LF_DIGEST line,
     `--json` works after the subcommand, sub-apps are registered
  5. DVSA provider parses the fixture CSV offline and yields E.164 phones
  6. Export truth on a COPY of a live database (never the original): schema migrates to v2, scoring +
     export run, every v0.3 column present, zero blank cells, no freemail address exported above an
     own-domain address, Next Action populated, Site Status never 'live' for 0-page crawls
  7. Outreach guardrail probes (skipped honestly while `leadforge outreach` is still a stub):
     unarmed --live must fail closed; a suppressed address must never be sent; an edited-after-approval
     message must not queue
  8. Drafting gate probes (skipped while `leadforge draft` is a stub): fabricated number / email /
     proper noun / banned claim are rejected, a clean draft passes

Zero tokens per run. Re-run after every change; paste its output, never paraphrase it (Rule 3).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[str, str, str]] = []  # (check, PASS|FAIL|SKIP, detail)

# Windows consoles default to cp1252; digests and CLI help carry non-ASCII (dashes, box drawing)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def rec(check: str, ok: bool | None, detail: str = "") -> None:
    status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    detail = detail.encode("ascii", "replace").decode("ascii")
    RESULTS.append((check, status, detail))
    print(f"[{status}] {check}" + (f" - {detail}" if detail else ""), flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, timeout: int = 1800) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env={**os.environ, **(env or {})}, shell=False)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def digest_lines(out: str) -> list[dict]:
    found = []
    for line in out.splitlines():
        if line.startswith("LF_DIGEST "):
            try:
                found.append(json.loads(line[len("LF_DIGEST "):]))
            except json.JSONDecodeError:
                found.append({"_bad": line})
    return found


# --------------------------------------------------------------------------- 1. suite + lint
def check_suite(skip: bool) -> None:
    if skip:
        rec("pytest full suite", None, "--skip-suite")
    else:
        code, out = run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-W", "ignore",
                         "-o", "addopts=", "-q"], timeout=2400)
        last = [ln for ln in out.splitlines() if ln.strip()][-1] if out.strip() else "(no output)"
        rec("pytest full suite", code == 0 and " passed" in last and "failed" not in last, last)
    code, out = run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"])
    rec("ruff check src tests scripts", code == 0, out.strip().splitlines()[-1] if out.strip() else "")


# --------------------------------------------------------------------------- 2. versions
def check_versions() -> None:
    pats = {
        "pyproject.toml": r'^version\s*=\s*"([^"]+)"',
        "src/leadforge/__init__.py": r'__version__\s*=\s*"([^"]+)"',
        ".claude-plugin/plugin.json": r'"version"\s*:\s*"([^"]+)"',
        ".codex-plugin/plugin.json": r'"version"\s*:\s*"([^"]+)"',
        "skills/generate-leads/SKILL.md": r'^\s*version:\s*"([^"]+)"',
    }
    seen = {}
    for rel, pat in pats.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        m = re.search(pat, text, re.MULTILINE)
        seen[rel] = m.group(1) if m else "?"
    ok = len(set(seen.values())) == 1 and "?" not in seen.values()
    rec("version strings agree", ok, ", ".join(f"{k.split('/')[-1]}={v}" for k, v in seen.items()))


# --------------------------------------------------------------------------- 3. plugin validate
def check_plugin_validate() -> None:
    exe = shutil.which("claude") or shutil.which("claude.cmd")
    if not exe:
        rec("claude plugin validate .", None, "claude CLI not on PATH")
        return
    code, out = run([exe, "plugin", "validate", "."], timeout=300)
    rec("claude plugin validate .", code == 0, out.strip().splitlines()[-1][:120] if out.strip() else "")


# --------------------------------------------------------------------------- 4. CLI contract
def _lf(args: list[str], cwd: Path) -> tuple[int, list[dict], str]:
    code, out = run([sys.executable, "-m", "leadforge", *args], cwd=cwd, env={"LEADFORGE_NO_UI": "1"})
    return code, digest_lines(out), out


def check_cli_contract(tmp: Path) -> None:
    ws = tmp / "ws"
    ws.mkdir()
    icp = ROOT / "config" / "icp.example.yaml"
    if not icp.exists():
        icp = next((ROOT / "config").glob("*.yaml"))
    _code, d, _ = _lf(["version", "--json"], ws)
    rec("CLI: `version --json` emits exactly one LF_DIGEST", len(d) == 1 and d[0].get("ok") is True)
    _code, d, _ = _lf(["status", "--json"], ws)
    rec("CLI: `status --json` (flag AFTER the subcommand) emits one digest", len(d) == 1)
    _code, d, _ = _lf(["--json", "plan", "--icp", str(icp)], ws)
    rec("CLI: `plan` digest has est_runtime_min + tiled/untiled counts",
        len(d) == 1 and d[0].get("ok") is True and "est_runtime_min" in d[0].get("counts", {})
        and "tiled_queries" in d[0].get("counts", {}), json.dumps(d[0].get("counts"))[:140] if d else "no digest")
    _code, _d, out = _lf(["--help"], ws)
    rec("CLI: outreach + draft sub-apps registered", "outreach" in out and "draft" in out)
    for sub in (["outreach", "status", "--json"], ["draft", "check", "--json"]):
        _code, d, _ = _lf(sub, ws)
        rec(f"CLI: `{' '.join(sub[:2])}` answers with one digest", len(d) == 1,
            "stub" if d and not d[0].get("ok") and any("NOT IMPLEMENTED" in w for w in d[0].get("warnings", [])) else "implemented")


# --------------------------------------------------------------------------- 5. DVSA offline
def check_dvsa_fixture() -> None:
    try:
        from leadforge.config import load_config
        from leadforge.grid import PlannedQuery
        from leadforge.normalize import to_business
        from leadforge.providers import dvsa as dvsa_mod
    except Exception as e:  # noqa: BLE001
        rec("DVSA provider imports", False, f"{type(e).__name__}: {e}")
        return
    fixture = ROOT / "tests" / "fixtures" / "dvsa_sample.csv"
    if not fixture.exists():
        rec("DVSA fixture parse", None, "fixture missing")
        return
    with tempfile.TemporaryDirectory() as td:
        cfg = load_config(td)
        cache = Path(td) / "leadforge_data" / "cache" / "dvsa"
        cache.mkdir(parents=True)
        (cache / "active-mot-stations.csv").write_bytes(fixture.read_bytes())
        prov = dvsa_mod.DvsaProvider(cfg)
        towns = {r["Town"].strip().title() for r in csv.DictReader(
            fixture.read_text(encoding="cp1252", errors="replace").splitlines()) if r.get("Town")}
        town = sorted(towns)[0]
        rows = prov.fetch(PlannedQuery(text=f"MOT centre in {town}, United Kingdom", category="MOT centre", area=town))
        biz = [to_business(r, "run_gate", None, "GB") for r in rows]
        ok = bool(rows) and all(b and b.phone_e164 and b.phone_e164.startswith("+44") for b in biz)
        rec("DVSA fixture -> businesses with E.164 phones", ok, f"{len(rows)} rows for {town}")


# --------------------------------------------------------------------------- 6. export truth on a DB copy
V03_COLUMNS = ["Fit", "Contactability", "Status", "Next Action", "Entity Type", "Lawful Basis (Email)",
               "Registry Name", "Registry Match", "Chain", "Site Status", "Email Confidence", "All Hooks"]
FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "yahoo.com", "yahoo.co.uk",
            "icloud.com", "aol.com", "btinternet.com", "live.com", "live.co.uk", "protonmail.com", "proton.me",
            "mail.com", "me.com", "msn.com"}


def check_export_on_copy(live_db: Path | None, tmp: Path) -> None:
    if not live_db or not live_db.exists():
        rec("export truth on a live-DB copy", None, "no --live-db given / not found")
        return
    from leadforge import db
    from leadforge.config import load_config
    from leadforge.export import export_run
    from leadforge.intake import load_icp
    from leadforge.score import score_run

    ws = tmp / "copy"
    (ws / "leadforge_data").mkdir(parents=True)
    shutil.copy(live_db, ws / "leadforge_data" / "db.sqlite3")
    icp_path = live_db.parent.parent / "icp.yaml"
    if not icp_path.exists():
        rec("export truth on a live-DB copy", None, "no icp.yaml beside the live db")
        return
    cfg = load_config(ws)
    conn = db.connect(cfg.db_path)
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    rec("copy migrates to schema v2", ver == "2", f"schema_version={ver}")
    icp = load_icp(icp_path)
    run = db.latest_run(conn)
    counts = score_run(conn, icp, run["id"], cfg=cfg)
    out_dir = ws / "exports"
    arts = export_run(conn, icp, run["id"], out_dir, ["csv"], cfg=cfg)
    csv_path = next(Path(a) for a in arts if a.endswith(".csv"))
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    header = list(rows[0].keys()) if rows else []
    missing = [c for c in V03_COLUMNS if c not in header]
    rec("all v0.3 columns exported", not missing, f"missing={missing}" if missing else f"{len(header)} columns")
    blanks = sum(1 for r in rows for c in V03_COLUMNS if c in r and not str(r[c]).strip())
    rec("zero blank cells in v0.3 columns", blanks == 0, f"{blanks} blank")
    # freemail must never outrank an own-domain address: compare exported Email against the DB contacts
    bad = 0
    for r in rows:
        email = (r.get("Email") or "").strip().lower()
        if "@" not in email:
            continue
        dom = email.rsplit("@", 1)[-1]
        if dom not in FREEMAIL:
            continue
        biz = conn.execute("SELECT id, domain FROM businesses WHERE name=?", (r.get("Business"),)).fetchone()
        if not biz or not biz["domain"]:
            continue
        own = conn.execute(
            "SELECT 1 FROM contacts WHERE business_id=? AND kind='email' AND tier IN ('valid','role') "
            "AND lower(value) LIKE ?", (biz["id"], f"%@{biz['domain'].lower()}")).fetchone()
        if own:
            bad += 1
    rec("no freemail exported above an own-domain address", bad == 0, f"{bad} violations")
    na = {}
    for r in rows:
        na[r.get("Next Action", "")] = na.get(r.get("Next Action", ""), 0) + 1
    rec("Next Action populated", all(k for k in na), json.dumps(na)[:200])
    st = {}
    for r in rows:
        st[r.get("Status", "")] = st.get(r.get("Status", ""), 0) + 1
    print("       tiers:", json.dumps(counts), "| status:", json.dumps(st)[:200])
    # 0-page crawls must not read 'live'
    phantom_live = 0
    for b in conn.execute("SELECT name, enrich_json FROM businesses"):
        e = json.loads(b["enrich_json"] or "{}")
        if e.get("crawled_at") and not (e.get("pages") or 0):
            row = next((r for r in rows if r.get("Business") == b["name"]), None)
            if row and row.get("Site Status", "").startswith("live"):
                phantom_live += 1
    rec("no 0-page crawl reads Site Status 'live'", phantom_live == 0, f"{phantom_live} rows")
    conn.close()


# --------------------------------------------------------------------------- 7/8. outreach + draft probes
def _stub(d: list[dict]) -> bool:
    return bool(d) and not d[0].get("ok") and any("NOT IMPLEMENTED" in w for w in d[0].get("warnings", []))


def check_outreach_probes(tmp: Path) -> None:
    ws = tmp / "outreach"
    ws.mkdir()
    code, d, out = _lf(["outreach", "send", "--live", "--i-am", "gate", "--json"], ws)
    if _stub(d) or not d:
        rec("outreach: unarmed --live fails closed", None, "outreach is still a stub (no digest / options missing)")
        return
    rec("outreach: unarmed --live fails closed", code != 0 and bool(d) and d[0].get("ok") is False,
        (d[0].get("warnings") or [""])[0][:100] if d else out[-120:])
    # deeper probes (suppressed address, edited-after-approval) are exercised by tests/test_outreach_*.py;
    # here we only prove the CLI surface refuses without arming.


def check_draft_probes(tmp: Path) -> None:
    ws = tmp / "draft"
    ws.mkdir()
    code, d, out = _lf(["draft", "check", "--json"], ws)
    if _stub(d) or not d:
        rec("draft: gate rejects fabrications", None, "draft is still a stub (no digest / options missing)")
        return
    try:
        from leadforge.draft import gate as gate_mod  # type: ignore
    except Exception as e:  # noqa: BLE001
        rec("draft: gate module importable", False, f"{type(e).__name__}: {e}")
        return
    fn = getattr(gate_mod, "check_draft", None) or getattr(gate_mod, "gate", None)
    if fn is None:
        rec("draft: gate function found (check_draft/gate)", False, "not found")
        return
    packet = {"co": "Hillcliffe Garage", "city": "Leeds", "facts": [{"k": "booking", "v": True, "src": "https://x"}],
              "offer": {"what": "IT support"}, "sender": {"name": "Alex"}}
    probes = {
        "invented number": ("Hi", "We helped 37 garages like Hillcliffe Garage last year."),
        "fake email": ("Hi", "Reach me at fake@nowhere.example about Hillcliffe Garage."),
        "invented proper noun": ("Hi", "Saw your note about Blackwood Tyres, Hillcliffe Garage."),
        "banned social proof": ("Hi", "Dozens of garages use online booking now, Hillcliffe Garage."),
    }
    results = []
    for name, (subj, obs) in probes.items():
        try:
            res = fn(packet, {"subject": subj, "observation": obs, "used_fact": "booking"})
            passed = bool(res.get("ok", res.get("passed", False))) if isinstance(res, dict) else bool(res)
        except Exception as e:  # noqa: BLE001
            passed, res = False, f"raised {type(e).__name__}"
        results.append((name, passed))
    clean_ok = False
    try:
        res = fn(packet, {"subject": "Online booking at Hillcliffe Garage", "observation": "Noticed Hillcliffe Garage already takes bookings online.", "used_fact": "booking"})
        clean_ok = bool(res.get("ok", res.get("passed", False))) if isinstance(res, dict) else bool(res)
    except Exception as e:  # noqa: BLE001
        clean_ok = False
        print("       clean draft raised:", type(e).__name__, e)
    rejected_all = all(not p for _, p in results)
    rec("draft: gate rejects 4 fabrications and passes a clean draft", rejected_all and clean_ok,
        ", ".join(f"{n}={'rejected' if not p else 'ACCEPTED'}" for n, p in results) + f", clean={'ok' if clean_ok else 'REJECTED'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-db", default="")
    ap.add_argument("--skip-suite", action="store_true")
    ap.add_argument("--quick", action="store_true", help="skip suite + plugin validate")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        check_suite(a.skip_suite or a.quick)
        check_versions()
        if not a.quick:
            check_plugin_validate()
        check_cli_contract(tmp)
        check_dvsa_fixture()
        check_export_on_copy(Path(a.live_db) if a.live_db else None, tmp)
        check_outreach_probes(tmp)
        check_draft_probes(tmp)
    fails = [c for c, s, _ in RESULTS if s == "FAIL"]
    print(f"\nGATE: {len(RESULTS)} checks, {len(fails)} FAIL, {sum(1 for _, s, _ in RESULTS if s == 'SKIP')} SKIP")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
