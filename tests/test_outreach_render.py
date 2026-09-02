"""U9E#6 — message rendering: headers, Message-ID token, footer. U9E#5's golden .eml lives here too
(FileTransport just writes what render.py already assembled)."""

from __future__ import annotations

from leadforge.outreach import render
from leadforge.outreach.transport.file import FileTransport
from tests.outreach_helpers import make_identity


def test_message_id_token_is_deterministic_and_secret_backed(conn):
    t1 = render.message_id_token(conn, 42)
    t2 = render.message_id_token(conn, 42)
    t3 = render.message_id_token(conn, 43)
    assert t1 == t2  # same target, same secret -> same token
    assert t1 != t3  # different target -> different token
    secret_row = conn.execute("SELECT value FROM meta WHERE key='outreach_secret'").fetchone()
    assert secret_row and len(secret_row["value"]) >= 32


def test_render_message_headers_and_footer(conn):
    make_identity(conn, label="ident1", from_email="sales@acme-agency.com", from_name="Acme Sales",
                 reply_to="replies@acme-agency.com", postal_address="1 Main St, Leeds",
                 privacy_url="https://acme-agency.com/privacy", unsubscribe_mailto="unsub@acme-agency.com",
                 unsubscribe_url="https://acme-agency.com/unsub")
    identity_row = conn.execute("SELECT * FROM sending_identities WHERE label='ident1'").fetchone()

    rendered = render.render_message(conn, target_id=7, identity=identity_row, to_email="owner@abbeyauto.co.uk",
                                     subject="Quick question about Abbey Auto", body_text="Hi there, noticed X.",
                                     business_name="Abbey Auto Repair")

    headers = dict(rendered.headers)
    assert headers["From"] == "Acme Sales <sales@acme-agency.com>"
    assert headers["Reply-To"] == "replies@acme-agency.com"
    assert headers["To"] == "owner@abbeyauto.co.uk"
    assert headers["Subject"] == "Quick question about Abbey Auto"
    assert "Date" in headers
    assert headers["Message-ID"].startswith("<lf-") and headers["Message-ID"].endswith("@acme-agency.com>")
    assert "<mailto:unsub@acme-agency.com>" in headers["List-Unsubscribe"]
    assert "<https://acme-agency.com/unsub>" in headers["List-Unsubscribe"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    assert "Hi there, noticed X." in rendered.body_text
    assert "Acme Sales" in rendered.body_text
    assert "1 Main St, Leeds" in rendered.body_text
    assert "We found Abbey Auto Repair in public business listings" in rendered.body_text
    assert "https://acme-agency.com/privacy" in rendered.body_text
    assert "opt out" in rendered.body_text.lower()
    # plain text only — no HTML, no tracking pixel, no wrapped/redirected links
    assert "<html" not in rendered.body_text.lower()
    assert "<img" not in rendered.body_text.lower()


def test_list_unsubscribe_post_absent_without_https_url(conn):
    make_identity(conn, label="mailto-only", unsubscribe_url="", unsubscribe_mailto="unsub@acme-agency.com")
    identity_row = conn.execute("SELECT * FROM sending_identities WHERE label='mailto-only'").fetchone()
    rendered = render.render_message(conn, target_id=1, identity=identity_row, to_email="x@y.com", subject="s",
                                     body_text="b", business_name="Y Ltd")
    headers = dict(rendered.headers)
    assert "List-Unsubscribe-Post" not in headers
    assert "List-Unsubscribe" in headers


def test_golden_eml_from_file_transport(conn, tmp_path):
    make_identity(conn, label="golden", from_email="sales@acme-agency.com", from_name="Acme Sales",
                 postal_address="1 Main St, Leeds", privacy_url="https://acme-agency.com/privacy",
                 unsubscribe_mailto="unsub@acme-agency.com", unsubscribe_url="https://acme-agency.com/unsub")
    identity_row = conn.execute("SELECT * FROM sending_identities WHERE label='golden'").fetchone()
    rendered = render.render_message(conn, target_id=99, identity=identity_row, to_email="owner@abbeyauto.co.uk",
                                     subject="Hello Abbey Auto Repair", body_text="Body line one.",
                                     business_name="Abbey Auto Repair")

    transport = FileTransport(tmp_path / "outbox")
    provider_id, path = transport.send(rendered, None)
    eml_files = list((tmp_path / "outbox").glob("*.eml"))
    assert len(eml_files) == 1
    text = eml_files[0].read_text(encoding="utf-8")

    for header_name in ("From:", "To:", "Subject:", "Date:", "Message-ID:", "List-Unsubscribe:",
                        "List-Unsubscribe-Post:"):
        assert header_name in text
    assert "Body line one." in text
    assert "Acme Sales" in text
    assert provider_id.startswith("file:")


# ---------------------------------------------------------------------------------------- watched-fail
#   test_render_message_headers_and_footer: build_footer() temporarily changed to drop the privacy_url
#     interpolation -> the "https://acme-agency.com/privacy" body assertion failed -> red for the right
#     reason. Restored.
#   test_list_unsubscribe_post_absent_without_https_url: the `if identity["unsubscribe_url"]:` guard on
#     List-Unsubscribe-Post temporarily removed (always appended) -> the `not in headers` assertion
#     failed -> red for the right reason. Restored.
#   test_golden_eml_from_file_transport: FileTransport._eml_text temporarily skipped writing the
#     List-Unsubscribe-Post line -> the header-presence loop assertion failed -> red for the right
#     reason. Restored.
