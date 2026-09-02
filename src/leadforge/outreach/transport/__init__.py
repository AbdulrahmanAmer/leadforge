"""Transport registry (v0.3 unit E, docs/09 Wave 2 E #5). No vendor SDKs — `file` and `smtp` only,
plus room for a future adapter to `register()` itself the same way discovery providers do."""

from __future__ import annotations

from pathlib import Path

from leadforge.outreach.transport.base import RenderedMessage, Transport
from leadforge.outreach.transport.file import FileTransport
from leadforge.outreach.transport.smtp import SmtpTransport

__all__ = ["RenderedMessage", "Transport", "FileTransport", "SmtpTransport", "get_transport", "register"]

_REGISTRY: dict[str, type[Transport]] = {"file": FileTransport, "smtp": SmtpTransport}


def register(name: str, cls: type[Transport]) -> None:
    _REGISTRY[name] = cls


def get_transport(name: str, *, outbox_dir: Path | None = None) -> Transport:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"unknown transport '{name}' (known: {sorted(_REGISTRY)})")
    if cls is FileTransport:
        if outbox_dir is None:
            raise ValueError("FileTransport requires outbox_dir")
        return FileTransport(outbox_dir)
    return cls()
