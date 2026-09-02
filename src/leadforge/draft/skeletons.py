"""Packaged drafting skeletons (v0.3 unit F, docs/09 Wave 2 F, `data/skeletons/*.yaml`).

Every skeleton is fixed text with exactly two model-written slots (`{{subject}}` handled separately
as the message header, `{{observation}}` inline) — everything else is a DETERMINISTIC slot the CLI
fills from the packet/identity, never the model: `{{greeting}}`, `{{offer_line}}`, `{{cta}}`,
`{{signature}}`, `{{postal_address}}`, `{{privacy_line}}`, `{{optout_line}}`, and (follow_up only)
`{{prev_days}}`. This keeps identity, the postal address, opt-out and the Article-14 sourcing line
outside the model's reach entirely.
"""

from __future__ import annotations

import re
from importlib.resources import files as _pkg_files

import yaml

PURPOSES = ("gainlev_leadgen", "client_campaign", "follow_up", "re_engagement", "referral")


def load_skeleton(purpose: str) -> dict:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose '{purpose}' (one of {', '.join(PURPOSES)})")
    text = (_pkg_files("leadforge") / "data" / "skeletons" / f"{purpose}.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text) or {}
    doc.setdefault("literals", [])
    doc.setdefault("template_numbers", [])
    return doc


def offer_line(icp) -> str:
    parts = [icp.offer.what.strip()]
    if icp.offer.value_prop.strip():
        parts.append(icp.offer.value_prop.strip())
    return " — ".join(p for p in parts if p)


def signature_line(identity: dict) -> str:
    return identity.get("from_name") or identity.get("label") or "The Team"


def postal_address_line(identity: dict) -> str:
    return identity.get("postal_address") or ""


def privacy_line(identity: dict) -> str:
    if identity.get("privacy_url"):
        return f"Privacy notice: {identity['privacy_url']}"
    return "We use only publicly available business information to send this one-time introduction (UK GDPR Art. 14)."


def optout_line(identity: dict) -> str:
    if identity.get("unsubscribe_url"):
        return f"Don't want these emails? Unsubscribe: {identity['unsubscribe_url']}"
    if identity.get("unsubscribe_mailto"):
        return f"Don't want these emails? Reply 'unsubscribe' or email {identity['unsubscribe_mailto']}."
    return "Reply 'unsubscribe' at any time and we will stop."


def deterministic_slots(skeleton: dict, packet: dict, identity: dict, prev_days: int | None = None) -> dict[str, str]:
    """Every slot the CLI fills itself — the model never sees or writes these."""
    slots = {
        "greeting": packet.get("greeting", "Hello,"),
        "offer_line": packet.get("offer", {}).get("what", "") or "",
        "cta": skeleton.get("cta", "Worth a quick chat this week?"),
        "signature": signature_line(identity),
        "postal_address": postal_address_line(identity),
        "privacy_line": privacy_line(identity),
        "optout_line": optout_line(identity),
    }
    value_prop = packet.get("offer", {}).get("value_prop") or ""
    if value_prop:
        slots["offer_line"] = f"{slots['offer_line']} — {value_prop}" if slots["offer_line"] else value_prop
    if prev_days is not None:
        slots["prev_days"] = str(prev_days)
    return slots


def render_body(skeleton: dict, slots: dict[str, str], observation: str) -> str:
    body = skeleton["body"]
    all_slots = {**slots, "observation": observation.strip()}
    for k, v in all_slots.items():
        body = body.replace("{{" + k + "}}", v)
    # a slot the caller forgot to supply (e.g. {{prev_days}} on a non-follow_up skeleton being
    # misused) must never ship literally — fail loudly instead of mailing a template artefact.
    missing = _find_tokens(body)
    if missing:
        raise ValueError(f"unfilled skeleton slot(s): {', '.join(missing)}")
    return body.strip() + "\n"


def _find_tokens(body: str) -> list[str]:
    return re.findall(r"\{\{(\w+)\}\}", body)


def render_txt(*, to: str, subject: str, body: str) -> str:
    return f"To: {to}\nSubject: {subject}\n\n{body}"
