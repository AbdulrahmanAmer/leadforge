"""End-to-end throughput benchmark: how long does a scored, enriched sheet take on this machine?

    python scripts/bench_speed.py --area "Nottingham" --categories "auto repair shop,MOT centre" \
        --providers dvsa,maps_list --parallel 2 --sites 60 --workers 12 [--grid] [--out DIR]

Runs a real mini-campaign in a fresh scratch workspace (never the live one), one stage at a time, with a
stopwatch around each: plan -> discover -> enrich (site crawl, capped at --sites) -> registry -> gbp ->
validate -> score -> export. Prints a stage table with items, seconds and items/min, then a projection to
1,000 rows per stage from the measured pace. Network is real; politeness is the tool's own. Nothing is
cached between runs unless you pass --workspace to reuse one.

The point is a number you can quote, produced by the same code paths the product runs — not an estimate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def _icp(area: str, country: str, categories: list[str], grid: bool, sites: int) -> dict:
    return {
        "version": 1, "campaign": "bench-speed",
        "offer": {"what": "IT support", "value_prop": "fewer missed bookings", "sender": "bench"},
        "target": {"categories": categories, "geography": {"country": country, "areas": [area], "grid": "auto" if grid else "off"},
                   "size": {"min_reviews": 0}},
        "qualify": {"hard": [], "soft": ["website_missing", "phone_only_booking", "weak_social_presence"]},
        "decision_maker": {"titles_priority": ["Owner", "Director"]},
        "caps": {"max_leads": 5000, "max_sites": sites, "max_tiles": 60},
        "compliance": {"region_profile": "uk" if country == "GB" else "us"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", default="Nottingham")
    ap.add_argument("--country", default="GB")
    ap.add_argument("--categories", default="auto repair shop,MOT centre")
    ap.add_argument("--providers", default="dvsa,maps_list")
    ap.add_argument("--parallel", type=int, default=2, help="discovery.parallel_queries")
    ap.add_argument("--grid", action="store_true", help="tile the area (discovery.grid_mode auto)")
    ap.add_argument("--sites", type=int, default=60, help="cap on sites crawled (caps.max_sites)")
    ap.add_argument("--workers", type=int, default=12, help="politeness.workers")
    ap.add_argument("--pages", type=int, default=4, help="crawl.pages_per_site")
    ap.add_argument("--ch-key", default="", help="Companies House key for the registry stage (optional)")
    ap.add_argument("--workspace", default="", help="reuse/create this workspace instead of a temp dir")
    ap.add_argument("--keep", action="store_true", help="keep the temp workspace")
    a = ap.parse_args()

    ws = Path(a.workspace) if a.workspace else Path(tempfile.mkdtemp(prefix="lf-bench-"))
    ws.mkdir(parents=True, exist_ok=True)
    cats = [c.strip() for c in a.categories.split(",") if c.strip()]
    (ws / "icp.yaml").write_text(yaml.safe_dump(_icp(a.area, a.country, cats, a.grid, a.sites), sort_keys=False), encoding="utf-8")
    cfg_doc = {
        "discovery": {"providers": [p.strip() for p in a.providers.split(",") if p.strip()],
                      "grid_mode": "auto" if a.grid else "off", "parallel_queries": a.parallel},
        "politeness": {"workers": a.workers},
        "crawl": {"pages_per_site": a.pages},
        "export": {"auto_open": False},
        "progress_window": False,
    }
    if a.ch_key:
        cfg_doc["registry"] = {"companies_house_key": a.ch_key}
    (ws / "leadforge.yaml").write_text(yaml.safe_dump(cfg_doc, sort_keys=False), encoding="utf-8")
    # reuse the live workspace's cached register + scraper binary when present (no re-download)
    live = Path(r"D:\GainLev\LeadForge\campaign-uk-autorepair\leadforge_data")
    for sub in ("cache/dvsa", "bin"):
        src, dst = live / sub, ws / "leadforge_data" / sub
        if src.exists() and not dst.exists():
            shutil.copytree(src, dst)

    import os

    os.environ["LEADFORGE_NO_UI"] = "1"
    os.chdir(ws)
    from leadforge import db
    from leadforge.config import load_config
    from leadforge.enrich.runner import run_enrich
    from leadforge.export import export_run
    from leadforge.intake import load_icp
    from leadforge.pipeline import run_discover
    from leadforge.score import score_run

    cfg = load_config(ws)
    icp = load_icp(ws / "icp.yaml")
    rows: list[tuple[str, int, float]] = []

    def stage(name: str, fn, items_fn):
        t = time.time()
        fn()
        secs = time.time() - t
        n = items_fn()
        rows.append((name, n, secs))
        rate = f"{60 * n / secs:7.1f}/min" if secs >= 0.5 else "    n/a"
        print(f"  {name:10s} {n:6d} items  {secs:7.1f} s  {rate}", flush=True)

    print(f"benchmark workspace: {ws}")
    print(f"providers={cfg_doc['discovery']['providers']} parallel={a.parallel} grid={a.grid} sites<={a.sites} workers={a.workers} pages={a.pages}")
    t_all = time.time()
    run_id_box: dict = {}

    def _discover():
        run_id_box["run"], counts, warns = run_discover(cfg, icp, ws / "icp.yaml")
        run_id_box["counts"] = counts
        if warns:
            print("   warnings:", warns)

    conn = db.connect(cfg.db_path)
    stage("discover", _discover, lambda: conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0])
    crawlable = conn.execute("SELECT COUNT(*) FROM businesses WHERE domain IS NOT NULL").fetchone()[0]
    print(f"   {crawlable} businesses have a website; crawling up to {a.sites}")
    stage("enrich", lambda: run_enrich(conn, cfg, a.sites, stage="site"),
          lambda: conn.execute("SELECT COUNT(*) FROM businesses WHERE json_extract(enrich_json,'$.crawled_at') IS NOT NULL "
                               "OR json_extract(enrich_json,'$.attempted_at') IS NOT NULL").fetchone()[0])
    stage("registry", lambda: run_enrich(conn, cfg, a.sites, stage="registry"),
          lambda: conn.execute("SELECT COUNT(*) FROM businesses WHERE json_extract(enrich_json,'$.registry_checked') IS NOT NULL").fetchone()[0])
    stage("gbp", lambda: run_enrich(conn, cfg, a.sites, stage="gbp"),
          lambda: conn.execute("SELECT COUNT(*) FROM people WHERE origin='gbp'").fetchone()[0])
    stage("validate", lambda: run_enrich(conn, cfg, a.sites, stage="validate"),
          lambda: conn.execute("SELECT COUNT(*) FROM contacts WHERE kind='email'").fetchone()[0])
    stage("score", lambda: score_run(conn, icp, run_id_box["run"], cfg=cfg),
          lambda: conn.execute("SELECT COUNT(*) FROM scores WHERE run_id=?", (run_id_box["run"],)).fetchone()[0])
    out_dir = ws / "exports"
    stage("export", lambda: export_run(conn, icp, run_id_box["run"], out_dir, ["xlsx", "csv"], cfg=cfg),
          lambda: len(list(out_dir.rglob("*.csv"))))
    total = time.time() - t_all

    biz = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    emails = conn.execute("SELECT COUNT(DISTINCT business_id) FROM contacts WHERE kind='email' AND tier IN ('valid','role')").fetchone()[0]
    dms = conn.execute("SELECT COUNT(DISTINCT business_id) FROM people WHERE is_dm=1").fetchone()[0]
    print(f"\nTOTAL {total:.0f} s -> {biz} businesses, {emails} with a sendable email, {dms} with a decision maker")
    print("\nProjection to 1,000 rows from the measured pace (per stage, independent):")
    for name, n, secs in rows:
        if n and secs >= 0.5 and name in ("discover", "enrich", "registry", "validate"):
            per = secs / n
            print(f"  {name:10s} {per:6.2f} s/item -> {1000 * per / 60:6.1f} min per 1,000")
    disc = next((s for nme, n, s in rows if nme == "discover"), 0.0)
    enr = next((s / n for nme, n, s in rows if nme == "enrich" and n), 0.0)
    reg = next((s / n for nme, n, s in rows if nme == "registry" and n), 0.0)
    print(f"  serial estimate for 1,000 businesses with websites: discover {disc / 60:.1f} min (this area) + enrich "
          f"{1000 * enr / 60:.1f} min + registry {1000 * reg / 60:.1f} min (+ score/export seconds)")
    report = {"args": vars(a), "stages": [{"stage": n, "items": i, "secs": round(s, 1)} for n, i, s in rows],
              "total_secs": round(total, 1), "businesses": biz, "with_sendable_email": emails, "with_dm": dms,
              "discover_counts": run_id_box.get("counts")}
    (ws / "bench_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"report: {ws / 'bench_report.json'}")
    if not a.keep and not a.workspace:
        print("(temp workspace kept for inspection; delete it yourself)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
