"""Enrichment throughput bench (v0.3 speed unit, 2026-09-02) — docs/09 build item 6.

    python scripts/bench_enrich.py --db <copy of db.sqlite3> --n 60 \
        [--workers 12] [--pages 4] [--budget 45] [--timeout 10] [--page-timeout 6] \
        [--browser-concurrency 4] [--dns-workers 8] [--out results.json]

Runs ONLY the 'site' stage (crawl + extract; no registry/gbp/infer/validate network) on a FRESH copy
of --db, made once per invocation — the source database named by --db is never opened for writing and
never mutated. Politeness (robots, per-host delay/single-flight, caps) is untouched; this only measures
the throughput effect of build items 1-3 (fail-fast tail, browser gate, workers/queue order).

Selection is deterministic: the first --n eligible business ids (domain IS NOT NULL, not yet crawled or
attempted, not suppressed), sorted by id ascending, straight from --db. Every OTHER eligible business in
the working copy is marked attempted (`excluded_for_bench`) before the run so businesses_for_enrich's
query returns EXACTLY this --n regardless of its own throughput-oriented ORDER BY — running this script
twice against the SAME --db with different --workers/--pages/... flags always benches the SAME hosts.

Prints one row (sites/min, ok/blocked/dead/robots counts, emails found, mean/p50/p90 seconds/site, and
the projection to 1,000 sites) and, with --out, writes the same as JSON. Compare two runs by invoking
this script twice — once with flags matching the OLD defaults (workers=4 pages=6 timeout=15
page-timeout=15 budget=999999 browser-concurrency=2), once with the NEW ones (the defaults below) — and
report both rows; this script does not itself run both, so the two invocations always share one
unambiguous set of "what changed" flags rather than a hidden hardcoded pair.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows consoles default to cp1252 — keep every character we print plain ASCII (see module docstring
# environment note); this reconfigure is a belt-and-braces fallback, not a license to print non-ASCII.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _checkpoint_and_copy(src_db: Path, dst_db: Path) -> None:
    """Flush src_db's WAL into the main file (so a plain file copy is self-contained), then copy just
    the .sqlite3 file — never opens src_db for writing, never touches -wal/-shm on the SOURCE side
    beyond a checkpoint (which SQLite itself performs safely on an existing WAL-mode db)."""
    conn = sqlite3.connect(str(src_db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        dst = Path(str(dst_db) + suffix)
        if dst.exists():
            dst.unlink()
    shutil.copy2(src_db, dst_db)
    for suffix in ("-wal", "-shm"):
        src_extra = Path(str(src_db) + suffix)
        if src_extra.exists():
            shutil.copy2(src_extra, Path(str(dst_db) + suffix))


def _select_ids(conn: sqlite3.Connection, n: int) -> list[str]:
    rows = conn.execute(
        """SELECT id FROM businesses WHERE domain IS NOT NULL
           AND json_extract(enrich_json,'$.crawled_at') IS NULL
           AND json_extract(enrich_json,'$.attempted_at') IS NULL
           AND domain NOT IN (SELECT value FROM suppression WHERE kind='domain')
           ORDER BY id ASC LIMIT ?""",
        (n,),
    ).fetchall()
    return [r[0] for r in rows]


def _exclude_everyone_else(conn: sqlite3.Connection, keep_ids: set[str]) -> None:
    """Stamp attempted_at on EVERY business not in keep_ids, unconditionally — not just the ones that
    would otherwise match businesses_for_enrich's normal eligibility filter. This matters because
    businesses_for_enrich(retry_needs_browser=True) also re-admits businesses that already have
    crawled_at set but needs_browser=1 (a real, common state on a live campaign snapshot) — an
    exclusion query that only looked at crawled_at/attempted_at IS NULL missed those, so they could
    outrank (by review_count) and displace some of our chosen --n ids from the LIMIT window, silently
    benching a DIFFERENT set of sites than the one --n selected. attempted_at IS NULL is unconditionally
    required by businesses_for_enrich regardless of retry_needs_browser, so stamping it on literally
    everyone else is the only reliable way to pin the query to EXACTLY keep_ids."""
    all_ids = [r[0] for r in conn.execute("SELECT id FROM businesses").fetchall()]
    others = [i for i in all_ids if i not in keep_ids]
    conn.executemany(
        "UPDATE businesses SET enrich_json = json_set(enrich_json, '$.attempted_at', 'excluded_for_bench') "
        "WHERE id=?",
        [(i,) for i in others],
    )
    conn.commit()


def _classify_error(error: str, needs_browser: bool) -> str:
    if not error:
        return "ok"
    if error == "robots-disallowed":
        return "robots"
    if "status:" in error:
        code = error.split("status:", 1)[1].rstrip(")")
        try:
            code_i = int(code)
        except ValueError:
            code_i = 0
        return "blocked" if code_i in (401, 403, 405, 406, 429, 503) else "dead"
    if "transport:" in error or "non-html" in error:
        return "dead"
    return "dead"


def _pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


def run_bench(db_copy_src: Path, n: int, workers: int, pages: int, budget: float, timeout: float,
             page_timeout: float, browser_concurrency: int, dns_workers: int, work_dir: Path,
             label: str) -> dict:
    from leadforge import db
    from leadforge.config import load_config
    from leadforge.enrich import runner as runner_mod

    work_dir.mkdir(parents=True, exist_ok=True)
    db_copy = work_dir / "leadforge_data" / "db.sqlite3"
    _checkpoint_and_copy(db_copy_src, db_copy)

    conn = sqlite3.connect(str(db_copy))
    conn.row_factory = sqlite3.Row
    ids = _select_ids(conn, n)
    if len(ids) < n:
        print(f"[bench:{label}] WARNING: only {len(ids)} eligible sites available (asked for {n})")
    _exclude_everyone_else(conn, set(ids))
    conn.close()

    cfg = load_config(work_dir)
    cfg.politeness.workers = workers
    cfg.crawl.pages_per_site = pages
    cfg.crawl.site_budget_s = budget
    cfg.crawl.timeout_s = timeout
    cfg.crawl.page_timeout_s = page_timeout
    cfg.enrich.browser_concurrency = browser_concurrency
    cfg.enrich.dns_workers = dns_workers
    cfg.social.enabled = False  # bench measures crawl/browser/DNS throughput, not agent-reach subprocesses

    assert str(cfg.db_path) == str(db_copy), f"db_path mismatch: {cfg.db_path} != {db_copy}"

    timings: list[tuple[str, float, bool, bool]] = []
    orig_process_one = runner_mod._process_one

    def _timed_process_one(cfg_, throttle, b):
        t0 = time.monotonic()
        out = orig_process_one(cfg_, throttle, b)
        dt = time.monotonic() - t0
        timings.append((b["id"], dt, bool(out.get("ok")), bool(out.get("needs_browser"))))
        return out

    runner_mod._process_one = _timed_process_one
    conn = db.connect(cfg.db_path)
    t0 = time.monotonic()
    try:
        counts = runner_mod.run_enrich(conn, cfg, len(ids), stage="site")
    finally:
        runner_mod._process_one = orig_process_one
        wall_s = time.monotonic() - t0

    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, enrich_json FROM businesses WHERE id IN ({placeholders})", ids
    ).fetchall()
    outcomes: dict[str, int] = {"ok": 0, "blocked": 0, "dead": 0, "robots": 0}
    for r in rows:
        ej = json.loads(r["enrich_json"])
        outcomes[_classify_error(ej.get("error", ""), bool(ej.get("needs_browser")))] += 1

    email_contacts = conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE kind='email' AND business_id IN ({placeholders})",
        ids,
    ).fetchone()[0]
    conn.close()

    secs = [t[1] for t in timings]
    n_run = len(ids)
    sites_per_min = (n_run / wall_s) * 60.0 if wall_s > 0 else 0.0
    result = {
        "label": label,
        "n": n_run,
        "workers": workers,
        "pages_per_site": pages,
        "site_budget_s": budget,
        "timeout_s": timeout,
        "page_timeout_s": page_timeout,
        "browser_concurrency": browser_concurrency,
        "dns_workers": dns_workers,
        "wall_s": round(wall_s, 2),
        "sites_per_min": round(sites_per_min, 2),
        "outcomes": outcomes,
        "emails_found": email_contacts,
        "counts": counts,
        "mean_s_per_site": round(statistics.fmean(secs), 2) if secs else 0.0,
        "p50_s_per_site": round(_pctl(secs, 0.50), 2),
        "p90_s_per_site": round(_pctl(secs, 0.90), 2),
        "projected_min_per_1000_sites": round(1000.0 / sites_per_min, 1) if sites_per_min > 0 else None,
    }
    return result


def _print_row(r: dict) -> None:
    print(
        f"[{r['label']}] n={r['n']} workers={r['workers']} pages={r['pages_per_site']} "
        f"budget={r['site_budget_s']}s timeout={r['timeout_s']}/{r['page_timeout_s']}s "
        f"browser_c={r['browser_concurrency']}"
    )
    print(f"  wall={r['wall_s']}s  sites/min={r['sites_per_min']}  "
          f"projected min/1000 sites={r['projected_min_per_1000_sites']}")
    o = r["outcomes"]
    print(f"  outcomes: ok={o['ok']} blocked={o['blocked']} dead={o['dead']} robots={o['robots']}")
    print(f"  emails_found={r['emails_found']}  counts={r['counts']}")
    print(f"  per-site seconds: mean={r['mean_s_per_site']} p50={r['p50_s_per_site']} p90={r['p90_s_per_site']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path, help="copy of db.sqlite3 to bench against (never mutated)")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--budget", type=float, default=45.0, help="crawl.site_budget_s; use a huge value to disable")
    ap.add_argument("--timeout", type=float, default=10.0, help="crawl.timeout_s (home page)")
    ap.add_argument("--page-timeout", type=float, default=6.0, help="crawl.page_timeout_s (secondary pages)")
    ap.add_argument("--browser-concurrency", type=int, default=4)
    ap.add_argument("--dns-workers", type=int, default=8)
    ap.add_argument("--label", default="run")
    ap.add_argument("--work-dir", type=Path, default=None, help="defaults to a temp dir next to --db")
    ap.add_argument("--out", type=Path, default=None, help="write the result row as JSON here too")
    args = ap.parse_args()

    if not args.db.is_file():
        print(f"ERROR: --db {args.db} does not exist")
        raise SystemExit(2)

    work_dir = args.work_dir or (args.db.parent / f"bench_{args.label}")
    result = run_bench(
        db_copy_src=args.db, n=args.n, workers=args.workers, pages=args.pages, budget=args.budget,
        timeout=args.timeout, page_timeout=args.page_timeout, browser_concurrency=args.browser_concurrency,
        dns_workers=args.dns_workers, work_dir=work_dir, label=args.label,
    )
    _print_row(result)
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
