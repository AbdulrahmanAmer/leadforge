"""Transport ABC (v0.3 unit E, docs/09 Wave 2 E #5, ADR-012).

A transport turns one rendered message into an attempt to deliver it. It never decides WHETHER to
send (eligibility/suppression/caps/window/circuit-breaker all live in outreach/send.py) — it only
knows how to hand a rendered message to a wire. No vendor SDKs: `file` (dry-run default) and `smtp`
(stdlib `smtplib`) are the only two built in; ADR-012 leaves room for a future adapter to register
itself the same way providers do.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RenderedMessage:
    """What render.py hands a transport: headers already resolved, body already assembled."""

    message_id_header: str
    from_name: str
    from_email: str
    reply_to: str
    to_email: str
    subject: str
    body_text: str
    headers: list[tuple[str, str]]  # ordered extra headers (List-Unsubscribe, List-Unsubscribe-Post, ...)


class Transport(ABC):
    name: str = "base"

    @abstractmethod
    def available(self, mailbox_row) -> tuple[bool, str]:
        """Cheap capability probe for this mailbox's config. Never raises."""

    @abstractmethod
    def send(self, rendered: RenderedMessage, mailbox_row) -> tuple[str, str]:
        """-> (provider_message_id, response_str). Raises on hard delivery failure."""
