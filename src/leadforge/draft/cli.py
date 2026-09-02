"""`leadforge draft` sub-app (v0.3 unit F, docs/09 Wave 2 F, ADR-012). Registered by leadforge.cli.

export -> agent writes subject+observation -> apply (gated, never trusts the model) -> render for
human review; check runs the gate standalone. No LLM API key anywhere in this package: the CLI hands
the agent (already in-harness) a compact evidence packet and reads back its two written slots.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from leadforge import db
from leadforge.config import Config
from leadforge.draft.gate import check_draft
from leadforge.draft.packet import build_packet, tokens_est
from leadforge.draft.skeletons import PURPOSES, deterministic_slots, load_skeleton, render_body, render_txt
from leadforge.enrich.validate import rank_email_contacts
from leadforge.score import fill_email_affinity
from leadforge.util import LeadForgeError, emit_digest, now_iso

draft_app = typer.Typer(help="Agent drafting loop: export packets -> agent writes -> apply (gated) -> render.")


def _cfg(ctx: typer.Context) -> Config:
    cfg = ctx.obj["cfg"]
    if cfg is None:  # mirrors leadforge.cli._cfg: a broken leadforge.yaml must not brick this sub-app
        emit_digest(False, "config-load", warnings=[ctx.obj.get("cfg_error", "leadforge.yaml invalid")],
                    next_="leadforge config set <key> <value> to repair, or fix/delete leadforge.yaml")
        raise typer.Exit(2)
    return cfg


def _say(ctx: typer.Context, *lines: str) -> None:
    if not ctx.obj.get("json_only"):
        for ln in lines:
            typer.echo(ln)


def _identity_for(conn, identity_id: int | None, default_name: str) -> dict:
    """A sending identity for the packet's `sender` fact and the message footer. Falls back to the
    campaign's only identity (or its name), then a bare default — outreach `identity add` may not
    have run yet (docs/09 Wave 2 F is testable standalone, before unit E creates any)."""
    if identity_id:
        row = conn.execute("SELECT * FROM sending_identities WHERE id=?", (identity_id,)).fetchone()
        if row:
            return dict(row)
    row = conn.execute("SELECT * FROM sending_identities ORDER BY id LIMIT 1").fetchone()
    if row:
        return dict(row)
    return {"from_name": default_name, "label": "default", "postal_address": "",
            "privacy_url": "", "unsubscribe_mailto": "", "unsubscribe_url": ""}


def _best_contact(conn, business_row, cfg: Config):
    """The best MAILABLE contact, not just the best-ranked one: export.py's ranking still surfaces an
    ineligible address for the sheet's Email column to display (a human decides from there), but this
    module is about to compose an actual outbound message — a row correctly classified
    `freemail_unlinked` or `foreign` (a stranger's or a template-credit's freemail box; C1's
    classify_email_affinity is what produces that classification on a fresh crawl) must never become
    the packet's contact or a message's To: address, even though it can still legitimately outrank
    other candidates in rank_email_contacts' display order. Mirrors compliance.lawful_basis_email's
    affinity gate for cfg.validation.freemail_policy. NOTE this does not, and cannot, catch a
    pre-v0.3 row whose `affinity` column is still blank: fill_email_affinity backfills those through
    the coarser fallback_email_affinity (score.py), which reads ANY freemail domain as
    `freemail_linked` — the SAME leniency export.py/compliance.py apply everywhere else, kept
    consistent on purpose rather than making this module quietly stricter than the rest of the app."""
    contacts = db.contacts_for(conn, business_row["id"])
    filled = fill_email_affinity(contacts, business_row["domain"])
    policy = cfg.validation.freemail_policy
    for c in rank_email_contacts(filled):
        if c["tier"] not in ("valid", "role"):
            continue
        affinity = c.get("affinity") or ""
        if affinity == "own_domain":
            return c
        if affinity == "freemail_linked" and policy in ("linked", "any"):
            return c
    return None


def _ensure_targets(conn, run_id: str, tiers: set[str], campaign: str) -> list[int]:
    """Standalone mode (docs/09 Wave 2 F): create minimal outreach_targets rows (state enrolled) for
    scored businesses in `tiers` so `draft export --run` works before unit E's `outreach plan` has."""
    rows = db.scores_for_run(conn, run_id)
    ids: list[int] = []
    now = now_iso()
    for s in rows:
        if s["tier"] not in tiers:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO outreach_targets(business_id,campaign,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?)", (s["business_id"], campaign, "enrolled", now, now),
        )
        row = conn.execute("SELECT id FROM outreach_targets WHERE business_id=? AND campaign=?",
                           (s["business_id"], campaign)).fetchone()
        if row:
            ids.append(row["id"])
    conn.commit()
    return ids


def _load_packets(path: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            row = json.loads(ln)
            if "target" in row and "packet" in row:
                out[int(row["target"])] = row["packet"]
    return out


def _prev_days(conn, target_id: int, cfg: Config) -> int:
    """Real elapsed days since the target's last message when one exists; otherwise the configured
    default cadence (cfg.outreach.follow_up_days) — never a fabricated number the gate would have to
    catch (this is a deterministic slot, never model-written, so the gate never sees it either way)."""
    row = conn.execute("SELECT created_at FROM messages WHERE target_id=? ORDER BY id DESC LIMIT 1",
                       (target_id,)).fetchone()
    if row and row["created_at"]:
        try:
            prev = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            days = (datetime.now(UTC) - prev).days
            if days > 0:
                return days
        except (ValueError, TypeError):
            pass
    return cfg.outreach.follow_up_days


@draft_app.command("export")
def export(
    ctx: typer.Context,
    campaign: str = typer.Option(..., "--campaign"),
    purpose: str = typer.Option(..., "--purpose"),
    icp: str = typer.Option("icp.yaml", "--icp"),
    run: str | None = typer.Option(None, "--run", help="score businesses without outreach_targets rows yet"),
    tier: str | None = typer.Option(None, "--tier", help="with --run: comma-separated tiers, default A,B"),
    max_: int = typer.Option(40, "--max"),
    out: str | None = typer.Option(None, "--out"),
    redo: bool = typer.Option(False, "--redo", help="also re-export targets already in state 'drafted'"),
) -> None:
    """Write one evidence packet per enrolled target (<= cfg.draft.packet_max_tokens each) for the
    agent to draft a subject + observation from."""
    from leadforge.intake import load_icp

    cfg = _cfg(ctx)
    if purpose not in PURPOSES:
        emit_digest(False, "draft export", warnings=[f"unknown --purpose '{purpose}' (one of {', '.join(PURPOSES)})"])
        raise typer.Exit(2)
    try:
        icp_obj = load_icp(Path(icp))
    except LeadForgeError as e:
        emit_digest(False, "draft export", warnings=[str(e)[:120]])
        raise typer.Exit(e.exit_code) from e

    conn = db.connect(cfg.db_path)
    if run:
        tiers = {t.strip().upper() for t in (tier or "A,B").split(",") if t.strip()}
        target_ids = _ensure_targets(conn, run, tiers, campaign)
    else:
        states = ("enrolled", "drafted") if redo else ("enrolled",)
        q = ",".join("?" * len(states))
        rows = conn.execute(
            f"SELECT id FROM outreach_targets WHERE campaign=? AND state IN ({q}) ORDER BY id",
            (campaign, *states),
        ).fetchall()
        target_ids = [r["id"] for r in rows]
    if max_:
        target_ids = target_ids[:max_]

    default_identity_name = icp_obj.offer.sender or icp_obj.campaign
    out_path = Path(out) if out else cfg.workspace / "drafts_packets.ndjson"
    header_identity = _identity_for(conn, None, default_identity_name)
    lines: list[dict] = [{
        "purpose": purpose,
        "offer": {"what": icp_obj.offer.what, "value_prop": icp_obj.offer.value_prop},
        "sender": {"from_name": header_identity.get("from_name") or header_identity.get("label") or "",
                  "label": header_identity.get("label", "")},
        "constraints": {"max_observation_words": cfg.draft.max_observation_words,
                        "max_subject_chars": cfg.draft.max_subject_chars},
        "instructions": "Write ONLY 'subject' and 'observation'. Cite exactly one packet fact by its "
                        "'k' as 'used_fact'. Never invent a number, name, email, URL or claim that is "
                        "not in this packet. If grade is 'C' and the purpose needs personalisation, "
                        "set 'abstain': true instead of padding with a generic line.",
    }]
    counts = {"targets": 0, "grade_a": 0, "grade_b": 0, "grade_c": 0, "insufficient_evidence": 0}
    for tid in target_ids:
        t = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (tid,)).fetchone()
        if t is None:
            continue
        b = conn.execute("SELECT * FROM businesses WHERE id=?", (t["business_id"],)).fetchone()
        if b is None:
            continue
        contact_row = None
        if t["contact_id"]:
            contact_row = conn.execute("SELECT * FROM contacts WHERE id=?", (t["contact_id"],)).fetchone()
        if contact_row is None:
            contact_row = _best_contact(conn, b, cfg)
        identity = _identity_for(conn, t["identity_id"], default_identity_name)
        packet = build_packet(conn, cfg, icp_obj, b, contact_row, purpose, identity)
        est = tokens_est(packet)
        counts["targets"] += 1
        counts[f"grade_{packet['grade'].lower()}"] += 1
        if not packet["facts"]:
            counts["insufficient_evidence"] += 1
        lines.append({"target": tid, "grade": packet["grade"], "tokens_est": est, "packet": packet})

    with open(out_path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False) + "\n")

    tok_vals = [ln["tokens_est"] for ln in lines[1:]]
    mean_tok = round(sum(tok_vals) / len(tok_vals), 1) if tok_vals else 0
    _say(ctx, f"packets={counts['targets']} A={counts['grade_a']} B={counts['grade_b']} "
              f"C={counts['grade_c']} insufficient_evidence={counts['insufficient_evidence']} "
              f"mean_tokens={mean_tok} -> {out_path}")
    emit_digest(True, "draft export",
                counts={**counts, "mean_tokens_est": mean_tok, "max_tokens_est": max(tok_vals, default=0)},
                artifacts=[str(out_path.resolve())],
                next_="agent writes subject+observation per line into drafts.ndjson, then: "
                      "leadforge draft apply --in drafts.ndjson")


@draft_app.command("apply")
def apply(
    ctx: typer.Context,
    in_: str = typer.Option("drafts.ndjson", "--in"),
    campaign: str | None = typer.Option(None, "--campaign", help="unused filter placeholder; drafts.ndjson already names targets"),
    packets: str | None = typer.Option(None, "--packets"),
) -> None:
    """Ingest the agent's drafts; every draft passes the mechanical no-fabrication gate or is rejected."""
    cfg = _cfg(ctx)
    in_path = Path(in_)
    packets_path = Path(packets) if packets else cfg.workspace / "drafts_packets.ndjson"
    if not in_path.is_file():
        emit_digest(False, "draft apply", warnings=[f"{in_path} not found"], next_="leadforge draft export ...")
        raise typer.Exit(2)
    if not packets_path.is_file():
        emit_digest(False, "draft apply", warnings=[f"{packets_path} not found"],
                    next_=f"leadforge draft export --out {packets_path}")
        raise typer.Exit(2)

    packet_by_target = _load_packets(packets_path)
    conn = db.connect(cfg.db_path)
    counts = {"applied": 0, "rejected": 0, "insufficient_evidence": 0, "skipped": 0}
    now = now_iso()
    for ln in Path(in_path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        draft = json.loads(ln)
        tid = draft.get("target")
        packet = packet_by_target.get(tid)
        t = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (tid,)).fetchone() if tid is not None else None
        if packet is None or t is None or (campaign and t["campaign"] != campaign):
            counts["skipped"] += 1
            continue
        if draft.get("abstain"):
            counts["insufficient_evidence"] += 1
            continue

        result = check_draft(packet, draft)
        step = conn.execute("SELECT COUNT(*) c FROM messages WHERE target_id=?", (tid,)).fetchone()["c"] + 1
        purpose = packet.get("purpose", "")
        subject = str(draft.get("subject", ""))
        used_fact = str(draft.get("used_fact", ""))
        if result["ok"]:
            skeleton = load_skeleton(purpose)
            identity = _identity_for(conn, t["identity_id"], (packet.get("sender") or {}).get("from_name", "The Team"))
            prev_days = _prev_days(conn, tid, cfg) if purpose == "follow_up" else None
            slots = deterministic_slots(skeleton, packet, identity, prev_days=prev_days)
            body = render_body(skeleton, slots, str(draft.get("observation", "")))
            draft_hash = hashlib.sha256((subject + "\n" + body).encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO messages(target_id,step,purpose,subject,body_text,draft_hash,state,gate_json,"
                "grade,used_fact,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, step, purpose, subject, body, draft_hash, "drafted", json.dumps(result),
                 packet.get("grade", ""), used_fact, now, now),
            )
            conn.execute("UPDATE outreach_targets SET state='drafted', updated_at=? WHERE id=?", (now, tid))
            counts["applied"] += 1
        else:
            conn.execute(
                "INSERT INTO messages(target_id,step,purpose,subject,body_text,draft_hash,state,gate_json,"
                "grade,used_fact,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, step, purpose, subject, "", "", "rejected", json.dumps(result),
                 packet.get("grade", ""), used_fact, now, now),
            )
            counts["rejected"] += 1
    conn.commit()
    _say(ctx, f"applied={counts['applied']} rejected={counts['rejected']} "
              f"insufficient_evidence={counts['insufficient_evidence']} skipped={counts['skipped']}")
    emit_digest(True, "draft apply", counts=counts, next_="leadforge draft render --campaign <c> --out drafts/")


@draft_app.command("render")
def render(
    ctx: typer.Context,
    campaign: str = typer.Option(..., "--campaign"),
    out: str = typer.Option("drafts", "--out"),
) -> None:
    """Write one reviewable .txt file per drafted message (To/Subject preamble + body)."""
    cfg = _cfg(ctx)
    conn = db.connect(cfg.db_path)
    rows = conn.execute(
        "SELECT m.*, t.business_id FROM messages m JOIN outreach_targets t ON t.id=m.target_id "
        "WHERE t.campaign=? AND m.state='drafted' ORDER BY m.id", (campaign,),
    ).fetchall()
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for m in rows:
        b = conn.execute("SELECT * FROM businesses WHERE id=?", (m["business_id"],)).fetchone()
        contact_row = _best_contact(conn, b, cfg) if b is not None else None
        to = contact_row["value"] if contact_row is not None else ((b["name"] if b is not None else "") or "")
        txt = render_txt(to=to, subject=m["subject"], body=m["body_text"])
        (out_dir / f"{m['target_id']}_{m['id']}.txt").write_text(txt, encoding="utf-8")
        n += 1
    _say(ctx, f"rendered={n} -> {out_dir}")
    emit_digest(True, "draft render", counts={"rendered": n}, artifacts=[str(out_dir.resolve())], next_=None)


@draft_app.command("check")
def check(
    ctx: typer.Context,
    in_: str | None = typer.Option(None, "--in"),
    packets: str | None = typer.Option(None, "--packets"),
) -> None:
    """Run the gate on a drafts file without storing anything."""
    if not in_:
        emit_digest(False, "draft check", warnings=["--in is required (path to a drafts.ndjson file)"],
                    next_="leadforge draft check --in drafts.ndjson")
        raise typer.Exit(2)
    cfg = _cfg(ctx)
    in_path = Path(in_)
    packets_path = Path(packets) if packets else cfg.workspace / "drafts_packets.ndjson"
    if not in_path.is_file():
        emit_digest(False, "draft check", warnings=[f"{in_path} not found"])
        raise typer.Exit(2)
    if not packets_path.is_file():
        emit_digest(False, "draft check", warnings=[f"{packets_path} not found"])
        raise typer.Exit(2)

    packet_by_target = _load_packets(packets_path)
    counts = {"checked": 0, "ok": 0, "failed": 0}
    for ln in in_path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        draft = json.loads(ln)
        packet = packet_by_target.get(draft.get("target"))
        if packet is None:
            continue
        counts["checked"] += 1
        result = check_draft(packet, draft)
        counts["ok" if result["ok"] else "failed"] += 1
    _say(ctx, f"checked={counts['checked']} ok={counts['ok']} failed={counts['failed']}")
    emit_digest(counts["failed"] == 0, "draft check", counts=counts, next_=None)
