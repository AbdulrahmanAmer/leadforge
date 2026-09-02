"""U9E#8 — `outreach send`: dry-run default, --live guardrails, per-message re-checks, circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from leadforge import db
from leadforge.outreach import send as send_mod
from leadforge.outreach.approve import approve_messages
from leadforge.util import LeadForgeError
from tests.outreach_helpers import make_identity, make_mailbox, make_message, make_run, make_scored_business, make_target


def _ready_message(conn, *, campaign="camp1", approver="alice", tier="A", email="owner@abbeyauto.co.uk",
                   domain="abbeyauto.co.uk", mailbox_addr="sales@acme-agency.com"):
    run_id = make_run(conn)
    bid = make_scored_business(conn, run_id, id_="biz_" + campaign, tier=tier, domain=domain, email=email)
    make_identity(conn, label="ident_" + campaign)
    make_mailbox(conn, identity_label="ident_" + campaign, address=mailbox_addr)
    tid = make_target(conn, business_id=bid, campaign=campaign, identity_id=conn.execute(
        "SELECT id FROM sending_identities WHERE label=?", ("ident_" + campaign,)).fetchone()["id"],
        state="drafted", eligibility={"_tier": tier, "_region_profile": "us"})
    mid = make_message(conn, target_id=tid, draft_hash="h1", subject="Hello", body_text="Body text here.")
    approve_messages(conn, campaign=campaign, approver=approver, all_drafted=True)
    return bid, tid, mid


def _armed_cfg(cfg):
    cfg.outreach.armed = True
    return cfg


@pytest.fixture(autouse=True)
def _pin_daytime(monkeypatch):
    """live_send()'s send-window check reads NOW_FN(); pin it to a stable UTC weekday daytime so
    these tests never flake depending on when in the day they happen to run. The one test that
    exercises the window check itself re-overrides this within its own body."""
    monkeypatch.setattr(send_mod, "NOW_FN", lambda: datetime(2026, 9, 2, 12, 0, tzinfo=UTC))


def test_dry_run_marks_nothing_sent_and_writes_eml(cfg, conn):
    bid, tid, mid = _ready_message(conn, campaign="dry1")
    result = send_mod.dry_run(conn, cfg, campaign="dry1")
    assert result["counts"]["would_send"] == 1
    assert conn.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()["state"] == "approved"
    assert conn.execute("SELECT state FROM outreach_targets WHERE id=?", (tid,)).fetchone()["state"] == "approved"
    assert len(result["artifacts"]) == 1


def test_live_unarmed_sends_nothing(cfg, conn):
    _ready_message(conn, campaign="unarmed1")
    with pytest.raises(LeadForgeError):
        send_mod.live_send(conn, cfg, campaign="unarmed1", i_am="alice")
    assert conn.execute("SELECT COUNT(*) c FROM messages WHERE state='sent'").fetchone()["c"] == 0


def test_live_wrong_approver_sends_nothing(cfg, conn):
    _armed_cfg(cfg)
    bid, tid, mid = _ready_message(conn, campaign="wrongapprover1", approver="alice")
    result = send_mod.live_send(conn, cfg, campaign="wrongapprover1", i_am="bob")
    assert result["counts"]["sent"] == 0
    assert result["counts"]["skipped_not_approver"] == 1
    assert conn.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()["state"] == "approved"


def test_live_send_success_writes_sent_state_and_eml(cfg, conn):
    _armed_cfg(cfg)
    bid, tid, mid = _ready_message(conn, campaign="livegood1")
    result = send_mod.live_send(conn, cfg, campaign="livegood1", i_am="alice")
    assert result["counts"]["sent"] == 1
    msg = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    assert msg["state"] == "sent" and msg["sent_at"] and msg["provider_message_id"].startswith("file:")
    target = conn.execute("SELECT state FROM outreach_targets WHERE id=?", (tid,)).fetchone()
    assert target["state"] == "sent"
    eml_files = list((cfg.data_path / cfg.outreach.outbox_dir).glob("*.eml"))
    assert len(eml_files) == 1


def test_suppressed_address_never_sent_even_if_approved(cfg, conn):
    _armed_cfg(cfg)
    bid, tid, mid = _ready_message(conn, campaign="supp1", email="owner@abbeyauto.co.uk")
    # suppressed AFTER approval, before send — the send-time re-check must catch it
    db.suppress(conn, "email", "owner@abbeyauto.co.uk", reason="test")
    result = send_mod.live_send(conn, cfg, campaign="supp1", i_am="alice")
    assert result["counts"]["sent"] == 0
    assert result["counts"]["skipped_suppressed"] == 1
    assert conn.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()["state"] == "approved"


def test_edited_after_approval_reverts_and_is_not_sent(cfg, conn):
    _armed_cfg(cfg)
    bid, tid, mid = _ready_message(conn, campaign="edited1")
    # simulate a re-draft after approval: content changed, hash no longer matches approved_hash
    conn.execute("UPDATE messages SET draft_hash='h_changed_after_approval' WHERE id=?", (mid,))
    conn.commit()
    result = send_mod.live_send(conn, cfg, campaign="edited1", i_am="alice")
    assert result["counts"]["sent"] == 0
    assert result["counts"]["skipped_hash_mismatch"] == 1
    msg = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    assert msg["state"] == "drafted"
    assert msg["approved_by"] == ""


def test_daily_cap_enforced_with_injected_clock(cfg, conn, monkeypatch):
    _armed_cfg(cfg)
    bid, tid, mid = _ready_message(conn, campaign="cap1")
    mailbox = conn.execute("SELECT id FROM mailboxes WHERE address='sales@acme-agency.com'").fetchone()
    conn.execute("UPDATE mailboxes SET daily_cap=0 WHERE id=?", (mailbox["id"],))
    conn.commit()
    result = send_mod.live_send(conn, cfg, campaign="cap1", i_am="alice")
    assert result["counts"]["sent"] == 0
    assert result["counts"]["skipped_cap"] == 1


def test_send_window_enforced_with_injected_clock(cfg, conn, monkeypatch):
    _armed_cfg(cfg)
    cfg.outreach.timezone = "UTC"
    cfg.outreach.send_window = "09:00-17:00"
    bid, tid, mid = _ready_message(conn, campaign="window1")
    monkeypatch.setattr(send_mod, "NOW_FN", lambda: datetime(2026, 9, 2, 3, 0, tzinfo=UTC))  # 3am, outside window
    result = send_mod.live_send(conn, cfg, campaign="window1", i_am="alice")
    assert result["counts"]["sent"] == 0
    assert result["counts"]["skipped_window"] == 1


def test_transport_exception_marks_unknown_never_requeued(cfg, conn, monkeypatch):
    _armed_cfg(cfg)
    bid, tid, mid = _ready_message(conn, campaign="crash1")

    class _BoomTransport:
        def send(self, rendered, mailbox_row):
            raise RuntimeError("smtp exploded")

    monkeypatch.setattr(send_mod, "get_transport", lambda name, outbox_dir=None: _BoomTransport())
    result = send_mod.live_send(conn, cfg, campaign="crash1", i_am="alice")
    assert result["counts"]["unknown"] == 1
    assert result["counts"]["sent"] == 0
    msg = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    assert msg["state"] == "unknown" and msg["error"]
    # a second send pass must NOT pick it up again (never auto-requeued: WHERE state='approved' excludes it)
    result2 = send_mod.live_send(conn, cfg, campaign="crash1", i_am="alice")
    assert result2["counts"]["sent"] == 0 and result2["counts"]["unknown"] == 0


def test_circuit_breaker_pauses_mailbox_after_hard_bounces(cfg, conn):
    _armed_cfg(cfg)
    cfg.outreach.bounce_rate_pause = 0.5  # low threshold so a couple of synthetic bounces trip it
    run_id = make_run(conn)
    ident = make_identity(conn, label="ident_breaker")
    mb_id = make_mailbox(conn, identity_label="ident_breaker", address="breaker@acme-agency.com")

    # send 2 messages successfully first
    sent_msg_ids = []
    for i in range(2):
        bid = make_scored_business(conn, run_id, id_=f"breaker_biz{i}", tier="A", domain=f"breaker{i}.co.uk",
                                   email=f"a@breaker{i}.co.uk")
        tid = make_target(conn, business_id=bid, campaign="breaker1", identity_id=ident, state="drafted",
                          eligibility={"_tier": "A", "_region_profile": "us"})
        mid = make_message(conn, target_id=tid, draft_hash=f"h{i}")
        sent_msg_ids.append(mid)
    approve_messages(conn, campaign="breaker1", approver="alice", all_drafted=True)
    result = send_mod.live_send(conn, cfg, campaign="breaker1", i_am="alice", mailbox_addr="breaker@acme-agency.com")
    assert result["counts"]["sent"] == 2

    # synthesize a hard-bounce event against one of the two just-sent messages
    real_mid = conn.execute("SELECT id FROM messages WHERE target_id IN "
                            "(SELECT id FROM outreach_targets WHERE campaign='breaker1') LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO events(message_id,kind,classification,dedupe_key,occurred_at,ingested_at) "
                "VALUES(?,?,?,?,?,?)", (real_mid, "bounce_hard", "bounce_hard", "dedupe1", "2026-09-02T00:00:00Z",
                                        "2026-09-02T00:00:00Z"))
    conn.commit()

    assert send_mod._check_breaker(conn, cfg, mb_id)
    mb = conn.execute("SELECT status, paused_reason FROM mailboxes WHERE id=?", (mb_id,)).fetchone()
    assert mb["status"] == "paused" and mb["paused_reason"]


# ---------------------------------------------------------------------------------------- watched-fail
#   test_suppressed_address_never_sent_even_if_approved: the `db.is_suppressed(...)` re-check
#     temporarily removed from live_send() -> the message was sent (sent==1) where 0 was expected ->
#     red for the right reason. Restored.
#   test_edited_after_approval_reverts_and_is_not_sent: the hash-equality guard temporarily replaced
#     with `if False:` -> the message was queued and "sent" instead of reverting -> red for the right
#     reason. Restored.
#   test_transport_exception_marks_unknown_never_requeued: the `WHERE m.state='approved'` clause in
#     _approved_messages temporarily widened to also match 'unknown' -> the second live_send() call
#     re-sent the crashed message (sent==1 on pass two) -> red for the right reason. Restored.
#   test_circuit_breaker_pauses_mailbox_after_hard_bounces: the `>=` comparison in _check_breaker
#     temporarily flipped to `>` -> the breaker failed to trip at the exact threshold -> red for the
#     right reason. Restored.
