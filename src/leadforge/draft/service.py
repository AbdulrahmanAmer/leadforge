"""Draft service (v0.4 "autopilot" unit B, ADR-015): the logic behind `leadforge draft export` and
`leadforge draft apply`, moved out of `draft/cli.py` so it can be driven headlessly by
`draft.service.auto_draft` from `pipeline.py` (autopilot) as well as by the CLI (manual/in-harness
mode, unchanged behaviour). `draft/cli.py`'s commands are now thin wrappers over this module.

`auto_draft` is the v0.4 addition: it resolves targets for a run, builds packets, and gets each
batch drafted either by an injected agent-runner callable (the operator's own Claude Code in
print mode — see `leadforge.agent_runner`, owned by unit A) or, when no runner is available (or a
batch's runner call fails) and `cfg.draft.template_fallback` is on, by the deterministic
`draft.template` drafter. Nothing here ever sends anything; drafts land in `messages` state
'drafted' or 'rejected', same as an interactively-drafted campaign.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from leadforge.config import Config
from leadforge.draft.gate import check_draft
from leadforge.draft.packet import DISTINCTIVE_KEYS, build_packet, tokens_est
from leadforge.draft.skeletons import deterministic_slots, load_skeleton, render_body
from leadforge.draft.template import template_drafts
from leadforge.models import ICP
from leadforge.util import now_iso


def identity_for(conn, identity_id: int | None, default_name: str) -> dict:
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


def best_contact(conn, business_row, cfg: Config):
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
    from leadforge import db
    from leadforge.enrich.validate import rank_email_contacts
    from leadforge.score import fill_email_affinity

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


def ensure_targets(conn, run_id: str, tiers: set[str], campaign: str) -> list[int]:
    """Standalone mode (docs/09 Wave 2 F): create minimal outreach_targets rows (state enrolled) for
    scored businesses in `tiers` so `draft export --run` works before unit E's `outreach plan` has.
    Also used headlessly by `auto_draft` (v0.4) to enrol autopilot's own draft targets."""
    from leadforge import db

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


def load_packets(path: Path) -> dict[int, dict]:
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


def build_packets(conn, cfg: Config, icp: ICP, target_ids: list[int], purpose: str) -> tuple[list[dict], dict]:
    """`lines[0]` is the header dict exactly as `draft export` writes today (purpose/offer/sender/
    constraints/instructions), then one `{"target","grade","tokens_est","packet"}` line per target;
    `counts` is the same dict the CLI has always reported (`targets`, `grade_a/b/c`,
    `insufficient_evidence`). Pure: does not write the file — the caller (CLI `export`, or
    `auto_draft`) decides whether/where to persist it."""
    default_identity_name = icp.offer.sender or icp.campaign
    header_identity = identity_for(conn, None, default_identity_name)
    lines: list[dict] = [{
        "purpose": purpose,
        "offer": {"what": icp.offer.what, "value_prop": icp.offer.value_prop},
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
            contact_row = best_contact(conn, b, cfg)
        identity = identity_for(conn, t["identity_id"], default_identity_name)
        packet = build_packet(conn, cfg, icp, b, contact_row, purpose, identity)
        est = tokens_est(packet)
        counts["targets"] += 1
        counts[f"grade_{packet['grade'].lower()}"] += 1
        if not packet["facts"]:
            counts["insufficient_evidence"] += 1
        lines.append({"target": tid, "grade": packet["grade"], "tokens_est": est, "packet": packet})
    return lines, counts


def infer_used_fact(packet: dict, draft: dict) -> str:
    """The packet fact the draft actually quotes, when the model forgot to cite one (live 2026-09-03: a
    12-packet batch came back with 11 citations, and one earlier batch with none although every line
    quoted a fact value verbatim). Inference only ever names a fact whose VALUE appears in the text —
    the gate's own USED_FACT test — so it can never launder an invented claim; distinctive facts win
    over baseline ones, and a text quoting nothing stays uncited (and is rejected as before)."""
    text = f"{draft.get('subject') or ''}\n{draft.get('observation') or ''}".casefold()
    best, best_rank = "", -1
    for f in packet.get("facts") or []:
        key, value = str(f.get("k") or ""), str(f.get("v") or "")
        if not key or not value or value.casefold() not in text:
            continue
        rank = 2 if key in DISTINCTIVE_KEYS else (0 if key in ("category", "city") else 1)
        if rank > best_rank:
            best, best_rank = key, rank
    return best


def apply_drafts(conn, cfg: Config, packet_by_target: dict[int, dict], drafts: Iterable[dict], *,
                 author: str = "agent", campaign: str | None = None) -> dict:
    """Ingest drafts (from the CLI's drafts.ndjson, or a batch of runner/template output); every
    draft passes the mechanical no-fabrication gate or is rejected. Same semantics/counts as
    `draft apply` has always had ({applied, rejected, insufficient_evidence, skipped}); every stored
    row (drafted AND rejected) now carries `messages.author = author` (schema v3)."""
    counts = {"applied": 0, "rejected": 0, "insufficient_evidence": 0, "skipped": 0}
    now = now_iso()
    for draft in drafts:
        tid = draft.get("target")
        packet = packet_by_target.get(tid)
        t = conn.execute("SELECT * FROM outreach_targets WHERE id=?", (tid,)).fetchone() if tid is not None else None
        if packet is None or t is None or (campaign and t["campaign"] != campaign):
            counts["skipped"] += 1
            continue
        if draft.get("abstain"):
            counts["insufficient_evidence"] += 1
            continue

        if not draft.get("used_fact"):
            inferred = infer_used_fact(packet, draft)
            if inferred:
                draft = {**draft, "used_fact": inferred}
        result = check_draft(packet, draft)
        step = conn.execute("SELECT COUNT(*) c FROM messages WHERE target_id=?", (tid,)).fetchone()["c"] + 1
        purpose = packet.get("purpose", "")
        subject = str(draft.get("subject", ""))
        used_fact = str(draft.get("used_fact", ""))
        if result["ok"]:
            skeleton = load_skeleton(purpose)
            identity = identity_for(conn, t["identity_id"], (packet.get("sender") or {}).get("from_name", "The Team"))
            prev_days = _prev_days(conn, tid, cfg) if purpose == "follow_up" else None
            slots = deterministic_slots(skeleton, packet, identity, prev_days=prev_days)
            body = render_body(skeleton, slots, str(draft.get("observation", "")))
            draft_hash = hashlib.sha256((subject + "\n" + body).encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO messages(target_id,step,purpose,subject,body_text,draft_hash,state,gate_json,"
                "grade,used_fact,author,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, step, purpose, subject, body, draft_hash, "drafted", json.dumps(result),
                 packet.get("grade", ""), used_fact, author, now, now),
            )
            conn.execute("UPDATE outreach_targets SET state='drafted', updated_at=? WHERE id=?", (now, tid))
            counts["applied"] += 1
        else:
            conn.execute(
                "INSERT INTO messages(target_id,step,purpose,subject,body_text,draft_hash,state,gate_json,"
                "grade,used_fact,author,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, step, purpose, subject, "", "", "rejected", json.dumps(result),
                 packet.get("grade", ""), used_fact, author, now, now),
            )
            counts["rejected"] += 1
    conn.commit()
    return counts


DRAFT_INSTRUCTIONS = (
    "You are drafting short outreach notes from evidence packets (see references/drafting.md). For "
    "each packet line you are given, write ONLY a subject and one observation sentence:\n"
    "1. Read the packet's 'facts' and nothing else. Every claim must trace to a fact's k/v pair, "
    "the business name (co), or the city -- never outside knowledge, even knowledge that happens "
    "to be true.\n"
    "2. Write ONLY 'subject' and 'observation'. Never write the greeting, offer line, CTA, "
    "signature, postal address, privacy line or opt-out line -- those are filled in separately.\n"
    "3. Cite exactly one fact as 'used_fact' (its k) and QUOTE THAT FACT'S VALUE VERBATIM in the "
    "observation (for rating '4.6 stars (120 reviews)' write exactly those words; for booking "
    "'shows an online-booking option on its site' write exactly that) -- a mechanical gate rejects "
    "any draft whose text does not contain the used fact's value or key word.\n"
    "3b. Every capitalised word or multi-word name you write must appear verbatim in the packet "
    "(the business name 'co', the city, legal_name, dm_name). Never coin a new capitalised phrase "
    "('Nottingham MOT') and never abbreviate or re-order the business name.\n"
    "4. Never invent a number, name, email, URL, or a competitor/social-proof/results claim "
    "('dozens of garages...', 'we cut their...', 'guaranteed').\n"
    "5. Negation matters: if the packet has no 'booking' fact, don't claim they take bookings "
    "online; if it has one, don't say they don't.\n"
    "6. Respect the packet's 'greeting' as given -- if it is 'Hello,' do not address anyone by "
    "name in the observation either, even if a dm_name fact is present.\n"
    "7. Abstain on grade 'C' when there is nothing worth saying -- set \"abstain\": true rather "
    "than pad with a generic line. Abstaining is a normal, counted outcome, not a failure.\n"
    "8. Keep 'observation' to one sentence, under constraints.max_observation_words; keep "
    "'subject' under constraints.max_subject_chars. Short beats clever.\n\n"
    "9. A line carrying 'rejected_attempt' is a retry: your previous draft for that target failed "
    "the gate for the listed reasons -- write a corrected draft that fixes every reason.\n\n"
    "Reply with ONLY one JSON line per packet line: "
    '{"target","subject","observation","used_fact"} or {"target","abstain":true}. '
    "Do not use any tools. No prose."
)


def _rejected_lines(conn, batch_lines: list[dict]) -> list[dict]:
    """The packet lines of `batch_lines` whose target has a rejected message and no drafted one, each
    annotated with the newest rejection (`rejected_attempt`: subject + gate reasons) for the retry."""
    out: list[dict] = []
    for ln in batch_lines:
        tid = ln["target"]
        if conn.execute("SELECT 1 FROM messages WHERE target_id=? AND state='drafted' LIMIT 1", (tid,)).fetchone():
            continue
        row = conn.execute("SELECT subject, gate_json FROM messages WHERE target_id=? AND state='rejected' "
                           "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        if row is None:
            continue
        try:
            reasons = json.loads(row["gate_json"]).get("reasons", [])
        except (TypeError, ValueError):
            reasons = []
        out.append({**{k: v for k, v in ln.items() if k != "rejected_attempt"},
                    "rejected_attempt": {"subject": row["subject"], "reasons": reasons}})
    return out


def auto_draft(conn, cfg: Config, icp: ICP, run_id: str, *, runner: Callable[[list[str]], list[dict]] | None,
               purpose: str | None = None, campaign: str | None = None) -> dict:
    """Autopilot's own drafting pass (v0.4, ADR-015): resolve this run's tier-eligible targets,
    build their evidence packets, and get each batch drafted by `runner` (the operator's own Claude
    Code in headless print mode) when available, falling back to the deterministic
    `draft.template.template_drafts` for any batch the runner can't handle — never blocking the run.

    Idempotent on resume: a target that already carries a message in state 'drafted' is skipped, so
    re-entering the 'drafting' stage after a crash never re-drafts (and never double-counts) work
    that already landed."""
    camp = campaign or icp.campaign
    purpose_ = purpose or cfg.draft.auto_purpose
    tiers = set(cfg.draft.auto_tiers)

    target_ids = ensure_targets(conn, run_id, tiers, camp)
    if target_ids:
        q = ",".join("?" * len(target_ids))
        already = {
            r["target_id"] for r in conn.execute(
                f"SELECT DISTINCT target_id FROM messages WHERE target_id IN ({q}) AND state='drafted'",
                target_ids,
            ).fetchall()
        }
        target_ids = [tid for tid in target_ids if tid not in already]
    target_ids = target_ids[: cfg.draft.auto_max]

    result = {"targets": len(target_ids), "drafted": 0, "rejected": 0, "abstained": 0,
              "skipped": 0, "author": "none", "batches": 0}
    if not target_ids:
        return result

    lines, _counts = build_packets(conn, cfg, icp, target_ids, purpose_)
    header, packet_lines = lines[0], lines[1:]
    authors_used: set[str] = set()

    def _apply(batch_lines: list[dict], drafts: list[dict], author: str) -> None:
        if not drafts:
            return
        packet_by_target = {ln["target"]: ln["packet"] for ln in batch_lines}
        counts = apply_drafts(conn, cfg, packet_by_target, drafts, author=author, campaign=camp)
        result["drafted"] += counts["applied"]
        result["rejected"] += counts["rejected"]
        result["abstained"] += counts["insufficient_evidence"]
        result["skipped"] += counts["skipped"]
        authors_used.add(author)

    if runner is not None:
        batch_size = max(1, cfg.agent.batch)
        for i in range(0, len(packet_lines), batch_size):
            batch = packet_lines[i:i + batch_size]
            result["batches"] += 1
            call_lines = [json.dumps(header, ensure_ascii=False)] + [
                json.dumps(ln, ensure_ascii=False) for ln in batch
            ]
            try:
                drafts: list[dict] | None = runner(call_lines)
            except Exception:
                # any failure from the injected runner (AgentFailed, a timeout, a bad subprocess,
                # or -- since leadforge.agent_runner may not even be importable in every worktree
                # this module ships from -- any other exception) is a failed batch, not a crashed
                # run: fall back to the deterministic drafter for just this batch.
                drafts = None
            if drafts is None:
                if cfg.draft.template_fallback:
                    _apply(batch, template_drafts(batch), "template")
                # else: template fallback disabled -> this batch simply drafts nothing
                continue
            _apply(batch, drafts, "agent")
            # gated retry (live 2026-09-03: 9 of 10 first drafts failed USED_FACT/PROPER_NOUN; the reasons
            # are exactly what the model needs to fix them) -- then the template for whatever still fails
            remaining = batch
            for _attempt in range(max(0, int(getattr(cfg.draft, "retries", 1)))):
                remaining = _rejected_lines(conn, remaining)
                if not remaining:
                    break
                result["batches"] += 1
                retry_lines = [json.dumps(header, ensure_ascii=False)] + [
                    json.dumps(ln, ensure_ascii=False) for ln in remaining
                ]
                try:
                    retry_drafts = runner(retry_lines)
                except Exception:
                    break
                _apply(remaining, retry_drafts or [], "agent")
            still = _rejected_lines(conn, remaining) if remaining else []
            if still and cfg.draft.template_fallback:
                _apply(still, template_drafts(still), "template")
    elif cfg.draft.template_fallback:
        result["batches"] = 1
        _apply(packet_lines, template_drafts(packet_lines), "template")
    # else: no runner and no template fallback -> no drafts at all (targets stay 'enrolled')

    if len(authors_used) == 1:
        result["author"] = next(iter(authors_used))
    elif len(authors_used) > 1:
        result["author"] = "mixed"
    return result
