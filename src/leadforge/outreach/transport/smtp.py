"""SmtpTransport (v0.3 unit E, docs/09 Wave 2 E #5) — stdlib `smtplib`, no vendor SDK.

Credentials come from the ENVIRONMENT VARIABLE NAMES stored in the mailbox's `config_json`
(`host_env`, `port_env`, `user_env`, `password_env`) — never a literal secret in SQLite. Port 465
connects over implicit SSL; any other port (587 by convention) starts in plaintext and upgrades with
STARTTLS. A missing env var makes `available()` report False, naming exactly which one.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from leadforge.outreach.identity import env_config
from leadforge.outreach.transport.base import RenderedMessage, Transport


class SmtpTransport(Transport):
    name = "smtp"

    def _resolve(self, mailbox_row) -> tuple[dict[str, str], list[str]]:
        cfg = env_config(mailbox_row)
        missing: list[str] = []
        out: dict[str, str] = {}
        for field, env_key in (("host", "host_env"), ("port", "port_env"), ("user", "user_env"),
                               ("password", "password_env")):
            env_name = cfg.get(env_key)
            if not env_name:
                missing.append(env_key)
                continue
            val = os.environ.get(env_name)
            if val is None:
                missing.append(env_name)
                continue
            out[field] = val
        return out, missing

    def available(self, mailbox_row) -> tuple[bool, str]:
        _, missing = self._resolve(mailbox_row)
        if missing:
            return False, f"missing env var(s): {', '.join(missing)}"
        return True, ""

    def send(self, rendered: RenderedMessage, mailbox_row) -> tuple[str, str]:
        creds, missing = self._resolve(mailbox_row)
        if missing:
            raise RuntimeError(f"SmtpTransport: missing env var(s) {missing}")
        host, port, user, password = creds["host"], int(creds["port"]), creds["user"], creds["password"]

        msg = EmailMessage()
        for name, value in rendered.headers:
            msg[name] = value
        msg.set_content(rendered.body_text)

        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        try:
            server.login(user, password)
            resp = server.send_message(msg)
        finally:
            server.quit()
        provider_message_id = rendered.message_id_header
        return provider_message_id, repr(resp)
