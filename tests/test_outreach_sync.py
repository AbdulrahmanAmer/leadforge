"""U9E#9 — `outreach sync`: webhook spool + IMAP ingestion, dedupe, suppression, state advance."""

from __future__ import annotations

import json

from leadforge import db
from leadforge.outreach import sync as sync_mod
from tests.outreach_helpers import make_identity, make_mailbox, make_message, make_run, make_scored_business, make_target


def _sent_message(conn, cfg, *, campaign="sync1"):
    run_id = make_run(conn)
    bid = make_scored_business(conn, run_id, id_="syncbiz", tier="A", email="owner@syncbiz.co.uk")
    make_identity(conn, label="ident_sync")
    make_mailbox(conn, identity_label="ident_sync", address="sales@acme-agency.com")
    ident_id = conn.execute("SELECT id FROM sending_identities WHERE label='ident_sync'").fetchone()["id"]
    tid = make_target(conn, business_id=bid, campaign=campaign, identity_id=ident_id, state="approved",
                      eligibility={"_tier": "A", "_region_profile": "us"})
    mid = make_message(conn, target_id=tid, draft_hash="h1", state="approved", approved_hash="h1", approved_by="alice")
    from leadforge.outreach.states import transition
    transition(conn, "message", mid, "queued")
    transition(conn, "message", mid, "sent")
    transition(conn, "target", tid, "queued")
    transition(conn, "target", tid, "sent")
    mb = conn.execute("SELECT id FROM mailboxes WHERE address='sales@acme-agency.com'").fetchone()
    conn.execute("UPDATE messages SET message_id_header=?, sent_at=?, mailbox_id=? WHERE id=?",
                ("<lf-abc123@acme-agency.com>", "2026-09-02T10:00:00Z", mb["id"], mid))
    conn.commit()
    return bid, tid, mid


def _write_spool(cfg, items):
    inbox = cfg.data_path / cfg.outreach.inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / "batch1.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def test_hard_bounce_suppresses_and_advances_target(cfg, conn):
    bid, tid, mid = _sent_message(conn, cfg)
    _write_spool(cfg, [{"kind": "bounce_hard", "email": "owner@syncbiz.co.uk", "message_id": "<lf-abc123@acme-agency.com>",
                        "occurred_at": "2026-09-02T11:00:00Z", "body": "550 no such user"}])
    counts = sync_mod.sync_inbox(conn, cfg)
    assert counts.get("bounce_hard") == 1
    assert db.is_suppressed(conn, "owner@syncbiz.co.uk")
    assert conn.execute("SELECT state FROM outreach_targets WHERE id=?", (tid,)).fetchone()["state"] == "bounced"
    row = conn.execute("SELECT source FROM suppression WHERE value='owner@syncbiz.co.uk'").fetchone()
    assert row["source"] == "bounce_hard"


def test_unsubscribe_suppresses_and_opts_out_target(cfg, conn):
    bid, tid, mid = _sent_message(conn, cfg, campaign="sync2")
    _write_spool(cfg, [{"kind": "unsubscribe", "email": "owner@syncbiz.co.uk", "message_id": "<lf-abc123@acme-agency.com>",
                        "occurred_at": "2026-09-02T11:00:00Z", "body": "please unsubscribe"}])
    counts = sync_mod.sync_inbox(conn, cfg)
    assert counts.get("unsubscribe") == 1
    assert db.is_suppressed(conn, "owner@syncbiz.co.uk")
    assert conn.execute("SELECT state FROM outreach_targets WHERE id=?", (tid,)).fetchone()["state"] == "opted_out"


def test_dedupe_on_rerun(cfg, conn):
    bid, tid, mid = _sent_message(conn, cfg, campaign="sync3")
    _write_spool(cfg, [{"kind": "bounce_hard", "email": "owner@syncbiz.co.uk", "message_id": "<lf-abc123@acme-agency.com>",
                        "occurred_at": "2026-09-02T11:00:00Z", "body": "550"}])
    counts1 = sync_mod.sync_inbox(conn, cfg)
    assert counts1.get("bounce_hard") == 1
    counts2 = sync_mod.sync_inbox(conn, cfg)  # same spool file, same content -> dedupe
    assert counts2.get("bounce_hard", 0) == 0
    assert counts2.get("duplicate") == 1
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1


def test_reply_classified_by_keyword(cfg, conn):
    bid, tid, mid = _sent_message(conn, cfg, campaign="sync4")
    _write_spool(cfg, [{"kind": "reply", "email": "owner@syncbiz.co.uk", "message_id": "<lf-abc123@acme-agency.com>",
                        "occurred_at": "2026-09-02T11:00:00Z", "body": "Yes please, sounds good, tell me more!"}])
    sync_mod.sync_inbox(conn, cfg)
    ev = conn.execute("SELECT classification FROM events WHERE kind='reply'").fetchone()
    assert ev["classification"] == "interested"
    assert conn.execute("SELECT state FROM outreach_targets WHERE id=?", (tid,)).fetchone()["state"] == "replied"


def test_reply_with_optout_phrase_suppresses_with_reply_optout_source(cfg, conn):
    bid, tid, mid = _sent_message(conn, cfg, campaign="sync5")
    _write_spool(cfg, [{"kind": "reply", "email": "owner@syncbiz.co.uk", "message_id": "<lf-abc123@acme-agency.com>",
                        "occurred_at": "2026-09-02T11:00:00Z", "body": "Please unsubscribe me from this list."}])
    sync_mod.sync_inbox(conn, cfg)
    row = conn.execute("SELECT source FROM suppression WHERE value='owner@syncbiz.co.uk'").fetchone()
    assert row["source"] == "reply_optout"
    assert conn.execute("SELECT state FROM outreach_targets WHERE id=?", (tid,)).fetchone()["state"] == "opted_out"


def test_soft_bounce_does_not_suppress(cfg, conn):
    bid, tid, mid = _sent_message(conn, cfg, campaign="sync6")
    _write_spool(cfg, [{"kind": "bounce_soft", "email": "owner@syncbiz.co.uk", "message_id": "<lf-abc123@acme-agency.com>",
                        "occurred_at": "2026-09-02T11:00:00Z", "body": "mailbox full, try later"}])
    sync_mod.sync_inbox(conn, cfg)
    assert not db.is_suppressed(conn, "owner@syncbiz.co.uk")


def test_never_stores_raw_body_in_event_payload(cfg, conn):
    bid, tid, mid = _sent_message(conn, cfg, campaign="sync7")
    secret_body = "this exact sentence must never land in the events table payload_json"
    _write_spool(cfg, [{"kind": "reply", "email": "owner@syncbiz.co.uk", "message_id": "<lf-abc123@acme-agency.com>",
                        "occurred_at": "2026-09-02T11:00:00Z", "body": secret_body}])
    sync_mod.sync_inbox(conn, cfg)
    payload = conn.execute("SELECT payload_json FROM events WHERE kind='reply'").fetchone()["payload_json"]
    assert secret_body not in payload


def test_imap_poll_classifies_raw_bounce(cfg, conn):
    bid, tid, mid = _sent_message(conn, cfg, campaign="sync8")
    conn.execute("UPDATE mailboxes SET config_json=? WHERE address='sales@acme-agency.com'",
                (json.dumps({"imap_host_env": "IMAP_HOST", "imap_user_env": "IMAP_USER",
                            "imap_password_env": "IMAP_PASS"}),))
    conn.commit()
    import os
    os.environ["IMAP_HOST"] = "imap.example.com"
    os.environ["IMAP_USER"] = "sales@acme-agency.com"
    os.environ["IMAP_PASS"] = "unused-in-test"

    raw = (b"From: owner@syncbiz.co.uk\r\n"
          b"To: sales@acme-agency.com\r\n"
          b"Subject: Undeliverable: Hello\r\n"
          b"In-Reply-To: <lf-abc123@acme-agency.com>\r\n"
          b"\r\n"
          b"This is an automatically generated Delivery Status Notification. 550 no such user.\r\n")

    class _FakeImap:
        def __init__(self, host):
            self.host = host

        def login(self, user, password):
            pass

        def select(self, mailbox):
            pass

        def search(self, charset, criterion):
            return "OK", [b"1"]

        def fetch(self, num, parts):
            return "OK", [(b"1 (RFC822 {n}}", raw)]

        def logout(self):
            pass

    try:
        counts = sync_mod.sync_inbox(conn, cfg, imap_client_cls=_FakeImap)
    finally:
        for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS"):
            os.environ.pop(k, None)

    assert counts.get("bounce_hard") == 1
    assert db.is_suppressed(conn, "owner@syncbiz.co.uk")


# ---------------------------------------------------------------------------------------- watched-fail
#   test_hard_bounce_suppresses_and_advances_target: `_SUPPRESSION_SOURCE["bounce_hard"]` temporarily
#     deleted -> `db.is_suppressed(...)` returned False -> red for the right reason. Restored.
#   test_dedupe_on_rerun: the dedupe_key existence check temporarily removed -> a second sync_inbox()
#     call inserted a second events row (COUNT == 2) -> red for the right reason. Restored.
#   test_reply_with_optout_phrase_suppresses_with_reply_optout_source: `_OPTOUT_KEYWORDS` temporarily
#     emptied -> classify_reply() returned is_optout=False -> the suppression row assertion failed
#     (no row at all) -> red for the right reason. Restored.
#   test_never_stores_raw_body_in_event_payload: `_ingest_event`'s payload_json temporarily changed to
#     `json.dumps(item)` (the whole raw item, body included) -> the secret sentence appeared in
#     payload_json -> red for the right reason. Restored.
#   test_imap_poll_classifies_raw_bounce: `classify_raw_kind`'s bounce_hard keyword list temporarily
#     emptied -> the message classified as a plain "reply" instead of "bounce_hard" -> red for the
#     right reason. Restored.
