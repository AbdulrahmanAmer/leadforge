"""`leadforge dashboard` (v0.3.1) — a local, read-only status page for a running campaign.

Why: `leadforge watch` is one bar for one stage. A human walking away from a multi-day run needs the whole
picture in one place, split the way the work actually splits:

  MACHINE (unattended, internet-bound, runs until the plan is exhausted): discover -> enrich -> registry
      -> gbp -> validate. Each gets its own measured pace and ETA; the sum is the walk-away timeline.
  HUMAN / AGENT (waits for someone): decision-maker labeling, then score + export (seconds), then outreach.

Everything here is READ-ONLY: the database is opened with `mode=ro`, the feed is tailed, nothing is written
and nothing is signalled to the run. Stdlib `http.server` only — no new dependency. The page polls
`/api/status` every 5 s; the JSON is also what an agent or a script can read.

Pace sources, in order of honesty: the feed's own timestamps (runs started with v0.3.1+); the run's
`started_at` from the DB for older feeds (cumulative average); for stages that have not started yet, the
pace measured on the previous run in this workspace when one exists (`runs.stats_json` durations), else the
documented defaults — and the page SAYS which one it is using.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from leadforge.util import _fmt_secs, progress_estimate

# documented fallbacks when no measurement exists yet (docs/09; measured 2026-08-31 / 2026-09-03)
DEFAULT_PACE_S = {
    "discover": 180.0,     # per Maps query (measured mean 3.0 min incl. stalls; median 2.0)
    "enrich": 2.5,         # per site with 12 workers (v0.3 default; 26 sites/min measured on 60 real sites, 2026-09-03)
    "registry": 1.65,      # per business (Companies House 600 req / 5 min at ~2.5 calls each, measured 2026-09-03)
    "gbp": 0.02,           # per business, local
    "validate": 0.3,       # per email (DNS MX, cached per domain)
}
MACHINE_STAGES = ["discover", "enrich", "registry", "gbp", "validate"]
HUMAN_STAGES = ["dm_pending", "scoring", "export", "outreach"]


def _ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _feed_history(feed: Path, run_started: float | None) -> dict[str, list[tuple[float, int, int]]]:
    """Per-stage [(ts, done, total)] from the feed. Lines without a timestamp (a process older than
    v0.3.1) are pinned to the run start, except the last which is pinned to the feed's mtime — so the
    estimate becomes the cumulative average, never a guess from this process's uptime."""
    hist: dict[str, list[tuple[float, int, int]]] = {}
    if not feed.is_file():
        return hist
    lines = feed.read_text(encoding="utf-8", errors="replace").splitlines()
    mtime = feed.stat().st_mtime
    last_stage, last_had_ts = None, True
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        ts = d.get("ts")
        last_had_ts = ts is not None
        if ts is None:
            ts = run_started or mtime
        last_stage = d.get("stage", "?")
        h = hist.setdefault(last_stage, [])
        done, total = int(d.get("done") or 0), int(d.get("total") or 0)
        if not h or h[-1][1] != done or h[-1][2] != total:
            h.append((float(ts), done, total))
    if last_stage and not last_had_ts and hist.get(last_stage):
        # pre-v0.3.1 feed: its latest count is "as of the file's last write", whatever line carried it
        # (the last line usually repeats the count with the next query's text, so it never appended above)
        t, done, total = hist[last_stage][-1]
        hist[last_stage][-1] = (max(t, mtime), done, total)
    return hist


def _counts(conn: sqlite3.Connection, run_id: str | None) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    out = {
        "businesses": q("SELECT COUNT(*) FROM businesses"),
        "with_website": q("SELECT COUNT(*) FROM businesses WHERE domain IS NOT NULL"),
        "sites_crawled": q("SELECT COUNT(*) FROM businesses WHERE json_extract(enrich_json,'$.crawled_at') IS NOT NULL"),
        "sites_attempted": q("SELECT COUNT(*) FROM businesses WHERE json_extract(enrich_json,'$.attempted_at') IS NOT NULL"),
        "registry_checked": q("SELECT COUNT(*) FROM businesses WHERE json_extract(enrich_json,'$.registry_checked') IS NOT NULL"),
        "registry_matched": q("SELECT COUNT(*) FROM businesses WHERE json_extract(enrich_json,'$.registry_profile.company_number') IS NOT NULL"),
        "emails": q("SELECT COUNT(*) FROM contacts WHERE kind='email'"),
        "emails_unvalidated": q("SELECT COUNT(*) FROM contacts WHERE kind='email' AND tier IN ('unknown','')"),
        "with_dm": q("SELECT COUNT(DISTINCT business_id) FROM people WHERE is_dm=1"),
        "dm_pending": q("SELECT COUNT(DISTINCT b.id) FROM businesses b JOIN people p ON p.business_id=b.id "
                        "WHERE NOT EXISTS (SELECT 1 FROM people x WHERE x.business_id=b.id AND x.is_dm!=0)"),
        "by_source": {r[0] or "?": r[1] for r in conn.execute("SELECT source, COUNT(*) FROM businesses GROUP BY source")},
    }
    if run_id:
        out.update({
            "new_this_run": q("SELECT COUNT(*) FROM businesses WHERE first_run_id=?", run_id),
            "queries_total": q("SELECT COUNT(*) FROM queries WHERE run_id=?", run_id),
            "queries_done": q("SELECT COUNT(*) FROM queries WHERE run_id=? AND status='done'", run_id),
            "queries_pending": q("SELECT COUNT(*) FROM queries WHERE run_id=? AND status IN ('pending','degraded')", run_id),
            "queries_skipped": q("SELECT COUNT(*) FROM queries WHERE run_id=? AND status='skipped'", run_id),
            "queries_degraded": q("SELECT COUNT(*) FROM queries WHERE run_id=? AND status IN ('degraded','failed')", run_id),
            "queries_saturated": q("SELECT COUNT(*) FROM queries WHERE run_id=? AND status='done' AND tile_json LIKE '%bbox%' "
                                   "AND result_count>=100", run_id),
            "queries_tiled_done": q("SELECT COUNT(*) FROM queries WHERE run_id=? AND status='done' AND tile_json LIKE '%bbox%'", run_id),
            "scored": q("SELECT COUNT(*) FROM scores WHERE run_id=?", run_id),
        })
    try:
        out["outreach_targets"] = q("SELECT COUNT(*) FROM outreach_targets")
        out["messages"] = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM messages GROUP BY state")}
    except sqlite3.Error:
        out["outreach_targets"] = 0
        out["messages"] = {}
    return out


PACE_FILE = "pace.json"


def _pace_store(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / PACE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pick_pace(data_dir: Path, store: dict, stage: str, est: dict, default_label: str) -> tuple[float, str]:
    """Measured on this process's feed > measured earlier in this workspace > documented default. The feed is
    truncated on every process start (`set_progress_file`), so without the store a resumed run fell back to the
    documented default for every stage it was not currently running (seen 2026-09-03: a 26 h walk-away time
    built on a 22 s/site default while the measured pace was 2.3 s)."""
    if est.get("per_item_s"):
        store[stage] = {"per_item_s": est["per_item_s"], "measured_at": time.time()}
        try:
            (data_dir / PACE_FILE).write_text(json.dumps(store), encoding="utf-8")
        except OSError:
            pass
        return est["per_item_s"], "measured on this run"
    prior = store.get(stage, {}).get("per_item_s")
    if prior:
        return float(prior), "measured earlier in this workspace"
    return DEFAULT_PACE_S[stage], default_label


def _overlap_enabled(data_dir: Path) -> bool:
    """Whether this workspace runs enrich/registry/validate overlapped (`enrich.overlap_stages`); defaults on."""
    try:
        from leadforge.config import load_config
        return bool(load_config(data_dir.parent).enrich.overlap_stages)
    except Exception:  # noqa: BLE001
        return True


def build_status(data_dir: Path, now: float | None = None) -> dict:
    """The whole picture as JSON: run, stage, machine timeline (measured or estimated per stage), human
    stages with their item counts, and the counts behind every number."""
    now = now or time.time()
    db_path = data_dir / "db.sqlite3"
    feed = data_dir / "progress.jsonl"
    out: dict = {"generated_at": datetime.fromtimestamp(now, tz=UTC).isoformat(), "data_dir": str(data_dir)}
    if not db_path.is_file():
        out["error"] = f"no workspace at {data_dir}"
        return out
    conn = _ro(db_path)
    run = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    run_id = run["id"] if run else None
    run_started = _iso_ts(run["started_at"]) if run else None
    stats = json.loads(run["stats_json"]) if run else {}
    counts = _counts(conn, run_id)
    hist = _feed_history(feed, run_started)
    store = _pace_store(data_dir)
    out.update({"run": run_id, "stage": run["stage"] if run else None,
                "started_at": run["started_at"] if run else None,
                "elapsed_s": (now - run_started) if run_started else None, "counts": counts})

    # ---- machine timeline ------------------------------------------------------------------
    machine = []
    stage = run["stage"] if run else "planned"
    # discover
    d = progress_estimate(hist.get("discover", []), now)
    # v0.4.1: remaining = what will actually run; 'skipped' children (prune-tiles / novelty gate) are not work
    remaining_q = counts.get("queries_pending", max(0, counts.get("queries_total", 0) - counts.get("queries_done", 0)))
    pace, src = _pick_pace(data_dir, store, "discover", d, "documented default")
    machine.append({"stage": "discover", "state": "running" if stage == "discovering" else ("done" if counts.get("queries_total") and remaining_q == 0 else "pending"),
                    "done": counts.get("queries_done", 0), "total": counts.get("queries_total", 0),
                    "growth": d["growth"], "pace_s": pace, "pace_source": src,
                    "eta_s": remaining_q * pace if remaining_q else 0.0,
                    "note": (f"{counts.get('queries_saturated', 0)}/{counts.get('queries_tiled_done', 0)} tiled queries saturated so far; "
                             f"each saturated tile adds 4 children, so the total keeps growing")})
    # enrich: sites with a domain not yet crawled/attempted
    todo = max(0, counts["with_website"] - counts["sites_crawled"] - counts["sites_attempted"])
    e = progress_estimate(hist.get("enrich", []), now)
    pace, src = _pick_pace(data_dir, store, "enrich", e, "documented default (12 workers)")
    machine.append({"stage": "enrich", "state": "running" if stage == "enriching" else ("done" if todo == 0 and counts["sites_crawled"] else "pending"),
                    "done": counts["sites_crawled"] + counts["sites_attempted"], "total": counts["with_website"],
                    "growth": 0, "pace_s": pace, "pace_source": src, "eta_s": todo * pace,
                    "note": "polite crawl: robots.txt, 1 request in flight per host; sites with no website are skipped"})
    # registry
    todo_r = max(0, counts["businesses"] - counts["registry_checked"])
    r = progress_estimate(hist.get("registry", []), now)
    pace, src = _pick_pace(data_dir, store, "registry", r, "documented default (Companies House rate limit)")
    machine.append({"stage": "registry", "state": "pending" if todo_r else "done", "done": counts["registry_checked"],
                    "total": counts["businesses"], "growth": 0, "pace_s": pace, "pace_source": src, "eta_s": todo_r * pace,
                    "note": f"{counts['registry_matched']} matched to an active company so far"})
    # validate
    v = progress_estimate(hist.get("validate", []), now)
    pace, src = _pick_pace(data_dir, store, "validate", v, "documented default")
    machine.append({"stage": "validate", "state": "pending" if counts["emails_unvalidated"] else "done",
                    "done": counts["emails"] - counts["emails_unvalidated"], "total": counts["emails"], "growth": 0,
                    "pace_s": pace, "pace_source": src, "eta_s": counts["emails_unvalidated"] * pace,
                    "note": "DNS MX per domain, never SMTP probing"})
    # v0.3 runs enrich, registry and validate OVERLAPPED after discovery (enrich.overlap_stages, default on): the
    # walk-away time is discover + the longest of the three, not their sum (the sum overstated it by ~2.5 h live).
    overlapped = _overlap_enabled(data_dir)
    after = [m["eta_s"] for m in machine if m["stage"] != "discover"]
    machine_eta = machine[0]["eta_s"] + (max(after) if overlapped else sum(after))
    out["machine"] = {"stages": machine, "eta_s": machine_eta, "eta_human": _fmt_secs(machine_eta),
                      "overlapped": overlapped,
                      "caveat": ("the enrich/registry totals grow as discovery finds businesses; the walk-away time is "
                                 + ("discover + the longest of enrich/registry/validate (they run overlapped)"
                                    if overlapped else "the sum of the stages (overlap is off)") + ", if pace holds")}

    # ---- human / agent stages ---------------------------------------------------------------
    # v0.4 (ADR-015): autopilot runs labeling and drafting unattended by default (the operator's own
    # headless Claude Code, or a deterministic fallback) — these rows still show what's left for a
    # human when autopilot is off or leaves leftovers (`dm export` / `draft export`).
    drafted_count = sum(counts.get("messages", {}).values())
    human = [
        {"stage": "dm labeling", "who": "agent (or you)", "items": counts["dm_pending"],
         "note": "autopilot: agent runner (claude -p) or heuristics; `dm export` for leftovers"},
        {"stage": "score + export", "who": "machine, seconds", "items": counts["businesses"],
         "note": "leadforge run --resume after labeling"},
        {"stage": "drafting", "who": "agent runner (claude -p) or template fallback", "items": drafted_count,
         "note": "autopilot drafts during `run`; `leadforge draft export` / `draft apply` for more"},
        {"stage": "outreach", "who": "you (approve, arm) + agent (draft)", "items": counts.get("outreach_targets", 0),
         "note": "outreach plan -> draft export/apply -> approve -> send (dry-run) -> doctor -> --live"},
    ]
    out["human"] = human
    out["stats"] = stats
    conn.close()
    return out


_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>LeadForge — campaign status</title>
<style>
 :root{--bg:#f4f6f5;--ink:#1b2622;--muted:#5c6b66;--rule:#d3dbd7;--acc:#0f6e52;--warn:#a8731c;--bar:#0f6e52;--bard:#b9d6c9}
 @media(prefers-color-scheme:dark){:root{--bg:#101816;--ink:#e4ebe7;--muted:#97a69f;--rule:#2c3a35;--acc:#5bc59f;--warn:#dba94a;--bar:#5bc59f;--bard:#2a4a3e}}
 body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,sans-serif}
 .wrap{max-width:980px;margin:0 auto;padding:28px 20px}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;letter-spacing:.08em;text-transform:uppercase;color:var(--acc);margin:28px 0 8px}
 .muted{color:var(--muted)} table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--rule);vertical-align:top;font-variant-numeric:tabular-nums}
 th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)} .n{text-align:right;font-family:ui-monospace,Consolas,monospace;white-space:nowrap}
 .bar{height:8px;background:var(--bard);border-radius:2px;overflow:hidden;min-width:120px}.bar i{display:block;height:100%;background:var(--bar)}
 .big{font-size:28px;font-family:ui-monospace,Consolas,monospace} .pill{display:inline-block;padding:1px 7px;border-radius:3px;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
 .running{background:var(--bar);color:#fff}.pending{background:var(--rule);color:var(--muted)}.done{background:var(--bard);color:var(--ink)}
 .note{font-size:13px;color:var(--muted)} .err{color:var(--warn)}
</style></head><body><div class="wrap">
<h1>LeadForge campaign status</h1><div class="muted" id="hdr">loading…</div>
<h2>Machine — unattended, runs until the plan is exhausted</h2>
<div class="big" id="meta"></div><div class="note" id="mcav"></div>
<table id="machine"><thead><tr><th>stage</th><th>state</th><th>progress</th><th class="n">done / total</th><th class="n">pace</th><th class="n">eta</th><th>note</th></tr></thead><tbody></tbody></table>
<h2>Human / agent — waits for someone</h2>
<table id="human"><thead><tr><th>stage</th><th>who</th><th class="n">items</th><th>note</th></tr></thead><tbody></tbody></table>
<h2>Counts</h2><pre id="counts" class="note"></pre>
<div class="note">Read-only: this page never writes to the database or signals the run. Refreshes every 5 s from <code>/api/status</code>.</div>
</div><script>
function fmt(s){if(s==null)return'—';s=Math.round(s);if(s>=3600)return Math.floor(s/3600)+'h'+String(Math.floor(s%3600/60)).padStart(2,'0')+'m';if(s>=60)return Math.floor(s/60)+'m'+String(s%60).padStart(2,'0')+'s';return s+'s'}
async function tick(){try{const r=await fetch('/api/status');const d=await r.json();
 if(d.error){document.getElementById('hdr').innerHTML='<span class=err>'+d.error+'</span>';return}
 document.getElementById('hdr').textContent=`run ${d.run} · stage ${d.stage} · started ${d.started_at} · elapsed ${fmt(d.elapsed_s)} · ${d.data_dir}`;
 document.getElementById('meta').textContent='~'+d.machine.eta_human+' of machine time left at current pace';
 document.getElementById('mcav').textContent=d.machine.caveat;
 const mb=document.querySelector('#machine tbody');mb.innerHTML='';
 for(const m of d.machine.stages){const pct=m.total?Math.min(100,Math.round(100*m.done/m.total)):0;
  mb.insertAdjacentHTML('beforeend',`<tr><td><b>${m.stage}</b></td><td><span class="pill ${m.state}">${m.state}</span></td><td><div class=bar><i style="width:${pct}%"></i></div></td><td class=n>${m.done} / ${m.total}${m.growth>0?' (+'+m.growth+')':''}</td><td class=n>${fmt(m.pace_s)}/item<br><span class=note>${m.pace_source}</span></td><td class=n>${fmt(m.eta_s)}</td><td class=note>${m.note}</td></tr>`)}
 const hb=document.querySelector('#human tbody');hb.innerHTML='';
 for(const h of d.human){hb.insertAdjacentHTML('beforeend',`<tr><td><b>${h.stage}</b></td><td>${h.who}</td><td class=n>${h.items}</td><td class=note>${h.note}</td></tr>`)}
 document.getElementById('counts').textContent=JSON.stringify(d.counts,null,1);
}catch(e){document.getElementById('hdr').innerHTML='<span class=err>status unavailable: '+e+'</span>'}}
tick();setInterval(tick,5000);
</script></body></html>"""


def make_handler(data_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet: the console is not the UI
            pass

        def do_GET(self):  # noqa: N802 — http.server API
            if self.path.startswith("/api/status"):
                body = json.dumps(build_status(data_dir), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            else:
                body = _HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(data_dir: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False,
          block: bool = True) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(data_dir))
    if open_browser:
        import webbrowser

        webbrowser.open(f"http://{host}:{srv.server_address[1]}/")
    if block:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
    else:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
