"""FileTransport (v0.3 unit E, docs/09 Wave 2 E #5) — the dry-run default.

Writes one RFC 5322 .eml per message under `cfg.data_path / cfg.outreach.outbox_dir`. Every header
render.py assembled is written verbatim and in order, so a human (or `leadforge outreach doctor`'s
sibling reviewer) can read exactly what would have gone out. No network I/O.
"""

from __future__ import annotations

from pathlib import Path

from leadforge.outreach.transport.base import RenderedMessage, Transport
from leadforge.util import sha1_hex


class FileTransport(Transport):
    name = "file"

    def __init__(self, outbox_dir: Path):
        self.outbox_dir = Path(outbox_dir)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def available(self, mailbox_row) -> tuple[bool, str]:
        return True, ""

    def _eml_text(self, rendered: RenderedMessage) -> str:
        lines = [f"{name}: {value}" for name, value in rendered.headers]
        lines.append("Content-Type: text/plain; charset=utf-8")
        lines.append("")
        lines.append(rendered.body_text)
        return "\r\n".join(lines) + "\r\n"

    def send(self, rendered: RenderedMessage, mailbox_row) -> tuple[str, str]:
        token = sha1_hex(rendered.message_id_header + rendered.to_email, 10)
        path = self.outbox_dir / f"{token}.eml"
        path.write_text(self._eml_text(rendered), encoding="utf-8", newline="")
        return f"file:{path.name}", str(path)
