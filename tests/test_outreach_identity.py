"""U9E#2 — sending identities + mailboxes. Values in mailbox --config are env var NAMES, never secrets."""

from __future__ import annotations

import pytest

from leadforge.outreach import identity as im
from leadforge.util import LeadForgeError


def test_identity_add_requires_from_email(conn):
    with pytest.raises(LeadForgeError):
        im.add_identity(conn, label="x", from_email="")


def test_identity_live_complete_requires_all_fields(conn):
    im.add_identity(conn, label="bare", from_email="a@b.com")
    row = im.get_identity(conn, "bare")
    assert not im.is_identity_complete(row)

    im.add_identity(conn, label="full", from_email="a@b.com", from_name="A B", postal_address="1 St",
                    privacy_url="https://x.com/privacy", unsubscribe_mailto="unsub@b.com")
    row2 = im.get_identity(conn, "full")
    assert im.is_identity_complete(row2)


def test_identity_duplicate_label_rejected(conn):
    im.add_identity(conn, label="dup", from_email="a@b.com")
    with pytest.raises(LeadForgeError):
        im.add_identity(conn, label="dup", from_email="c@d.com")


def test_mailbox_config_values_are_env_var_names_never_secrets(conn):
    im.add_identity(conn, label="ident1", from_email="a@b.com")
    mb_id = im.add_mailbox(conn, identity_label="ident1", address="a@b.com",
                           config={"host_env": "SMTP_HOST", "password_env": "SMTP_PASSWORD"})
    row = im.get_mailbox(conn, "a@b.com")
    stored = row["config_json"]
    # the literal secret must never appear — only the env var NAME does
    assert "SMTP_HOST" in stored and "SMTP_PASSWORD" in stored
    assert "hunter2" not in stored  # sanity: nothing that looks like a secret value snuck in
    assert mb_id


def test_mailbox_config_rejects_unknown_key(conn):
    im.add_identity(conn, label="ident2", from_email="a@b.com")
    with pytest.raises(LeadForgeError):
        im.add_mailbox(conn, identity_label="ident2", address="b@b.com", config={"literal_password": "hunter2"})


def test_mailbox_requires_existing_identity(conn):
    with pytest.raises(LeadForgeError):
        im.add_mailbox(conn, identity_label="ghost", address="x@x.com")


def test_list_identities_and_mailboxes(conn):
    im.add_identity(conn, label="i1", from_email="a@b.com")
    im.add_identity(conn, label="i2", from_email="c@d.com")
    im.add_mailbox(conn, identity_label="i1", address="a@b.com")
    im.add_mailbox(conn, identity_label="i1", address="a2@b.com")
    assert {r["label"] for r in im.list_identities(conn)} == {"i1", "i2"}
    assert len(im.list_mailboxes(conn, "i1")) == 2
    assert len(im.list_mailboxes(conn)) == 2


# ---------------------------------------------------------------------------------------- watched-fail
#   test_identity_live_complete_requires_all_fields: is_identity_complete() temporarily changed to
#     `return True` unconditionally -> the "bare" assertion (`not im.is_identity_complete(row)`) failed
#     -> red for the right reason. Restored.
#   test_mailbox_config_values_are_env_var_names_never_secrets: add_mailbox() temporarily changed to
#     store `config["literal_password"] = "hunter2"` directly (simulating an accidental secret write)
#     -> the "hunter2" absence assertion failed -> red for the right reason. Restored; the MAILBOX_CONFIG_KEYS
#     allowlist in identity.py has no key shaped like a literal secret, only *_env names.
