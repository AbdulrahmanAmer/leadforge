"""v0.3 unit E follow-up: SmtpTransport against a fake smtplib (no sockets) — SSL on 465, STARTTLS
otherwise, credentials only via env var NAMES, and a login failure surfaces as an exception the send
loop turns into 'unknown' (never a silent success)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from leadforge.outreach.transport import smtp as smtp_mod
from leadforge.outreach.transport.base import RenderedMessage


def _mailbox(config: dict) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE m(id, address, transport, config_json)")
    conn.execute("INSERT INTO m VALUES(1, 'me@sender.example', 'smtp', ?)", (json.dumps(config),))
    return conn.execute("SELECT * FROM m").fetchone()


class _FakeServer:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.tls = self.quit_called = False
        self.login_args = None
        self.sent = None
        type(self).instances.append(self)

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)
        if password == "wrong":
            raise smtp_mod.smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, msg):
        self.sent = msg
        return {}

    def quit(self):
        self.quit_called = True


class _FakeSSL(_FakeServer):
    pass


def _rendered() -> RenderedMessage:
    return RenderedMessage(
        message_id_header="<lf-abc@sender.example>", from_name="Me", from_email="me@sender.example",
        reply_to="", to_email="info@garage.example", subject="Hello", body_text="Hi,\n\nshort note.\n",
        headers=[("From", "Me <me@sender.example>"), ("To", "info@garage.example"), ("Subject", "Hello"),
                 ("Message-ID", "<lf-abc@sender.example>")],
    )


@pytest.fixture(autouse=True)
def _fake_smtplib(monkeypatch):
    _FakeServer.instances.clear()
    _FakeSSL.instances.clear()
    monkeypatch.setattr(smtp_mod.smtplib, "SMTP", _FakeServer)
    monkeypatch.setattr(smtp_mod.smtplib, "SMTP_SSL", _FakeSSL)


CONFIG = {"host_env": "T_HOST", "port_env": "T_PORT", "user_env": "T_USER", "password_env": "T_PASS"}


def _env(monkeypatch, port: str, password: str = "s3cret") -> None:
    monkeypatch.setenv("T_HOST", "smtp.example")
    monkeypatch.setenv("T_PORT", port)
    monkeypatch.setenv("T_USER", "me")
    monkeypatch.setenv("T_PASS", password)


def test_available_names_the_missing_env_var(monkeypatch):
    for k in ("T_HOST", "T_PORT", "T_USER", "T_PASS"):
        monkeypatch.delenv(k, raising=False)
    ok, reason = smtp_mod.SmtpTransport().available(_mailbox(CONFIG))
    assert not ok and "T_HOST" in reason


def test_starttls_path_on_587_sends_and_quits(monkeypatch):
    _env(monkeypatch, "587")
    mid, _resp = smtp_mod.SmtpTransport().send(_rendered(), _mailbox(CONFIG))
    srv = _FakeServer.instances[-1]
    assert isinstance(srv, _FakeServer) and not isinstance(srv, _FakeSSL)
    assert srv.tls and srv.login_args == ("me", "s3cret") and srv.quit_called
    assert srv.sent["Subject"] == "Hello" and srv.sent["To"] == "info@garage.example"
    assert mid == "<lf-abc@sender.example>"


def test_implicit_ssl_on_465(monkeypatch):
    _env(monkeypatch, "465")
    smtp_mod.SmtpTransport().send(_rendered(), _mailbox(CONFIG))
    srv = _FakeSSL.instances[-1]
    assert isinstance(srv, _FakeSSL) and not srv.tls and srv.quit_called


def test_login_failure_raises_and_still_quits(monkeypatch):
    _env(monkeypatch, "587", password="wrong")
    with pytest.raises(smtp_mod.smtplib.SMTPAuthenticationError):
        smtp_mod.SmtpTransport().send(_rendered(), _mailbox(CONFIG))
    assert _FakeServer.instances[-1].quit_called
