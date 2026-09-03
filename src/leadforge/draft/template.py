"""Deterministic template drafter (v0.4 "autopilot" unit B, ADR-015). Autopilot's fallback for
`draft.service.auto_draft` when no agent runner is available, or when a batch's runner call fails
and `cfg.draft.template_fallback` is on. Pure, no I/O, no randomness: `template_draft` composes a
subject + one observation sentence for a single packet from fixed prose plus strings that appear
verbatim in the packet (the business name `co`, a fact's `v`) -- nothing here is ever invented, so
its output must always pass `draft.gate.check_draft` for the packet it was built from.

Never writes a possessive of `co` (e.g. "Centre's"): that forms a brand-new token that is not
itself present anywhere in the packet JSON, so the gate's PROPER_NOUN check would reject it even
though `co` alone is fine.
"""

from __future__ import annotations

# Priority order: highest personalisation value first. Only these five keys are ever cited here --
# a packet whose only distinctive evidence is a key outside this list (e.g. `phone_confirmed` or
# `gbp_appointments` alone) has "nothing this drafter knows how to write about" and abstains, even
# when the packet's own grade is A or B.
_PRIORITY = ("booking", "site_stale", "legal_name", "hiring", "rating")


def _observation_for(key: str, co: str, by_key: dict[str, dict]) -> str | None:
    fact = by_key.get(key)
    if fact is None:
        return None
    v = fact.get("v")
    if v in (None, ""):
        return None
    if key == "booking":
        return f"Noticed {co} {v}."
    if key == "site_stale":
        return f"Noticed the site footer at {co} still says {v}."
    if key == "legal_name":
        year = by_key.get("incorporated_year")
        if year is not None and year.get("v") not in (None, ""):
            return f"Saw {v} has been trading since {year['v']}."
        return f"Saw {v} is a registered company."
    if key == "hiring":
        return f"Noticed {co} {v}."
    if key == "rating":
        return f"Noticed {co} has {v}."
    return None


def _subject_for(co: str, max_chars: int) -> str | None:
    candidate = f"Quick note for {co}"
    if len(candidate) <= max_chars:
        return candidate
    fallback = "Quick note"
    return fallback if len(fallback) <= max_chars else None


def template_draft(packet: dict) -> dict | None:
    """A deterministic `{"subject","observation","used_fact"}` from the packet's single best fact
    (priority order above), or `None` (abstain) when the packet is grade 'C' or carries none of the
    facts this drafter knows how to write about -- the same abstain semantics `draft apply` already
    gives `{"target", "abstain": true}` for an agent's own low-evidence packets."""
    if not isinstance(packet, dict) or packet.get("grade") == "C":
        return None
    co = str(packet.get("co") or "").strip()
    if not co:
        return None

    facts = packet.get("facts") or []
    by_key = {f["k"]: f for f in facts if isinstance(f, dict) and "k" in f}
    constraints = packet.get("constraints") or {}
    max_chars = int(constraints.get("max_subject_chars") or 60)
    max_words = int(constraints.get("max_observation_words") or 45)

    subject = _subject_for(co, max_chars)
    if subject is None:
        return None

    for key in _PRIORITY:
        observation = _observation_for(key, co, by_key)
        if observation is None:
            continue
        if len(observation.split()) > max_words:
            continue
        return {"subject": subject, "observation": observation, "used_fact": key}
    return None


def template_drafts(packet_lines: list[dict]) -> list[dict]:
    """For each `{"target","packet"}` input line -> `{"target","subject","observation","used_fact"}`
    or `{"target","abstain": true}`. A line missing `target` is skipped entirely (never emitted)."""
    out: list[dict] = []
    for line in packet_lines:
        if not isinstance(line, dict) or "target" not in line:
            continue
        target = line["target"]
        draft = template_draft(line.get("packet") or {})
        if draft is None:
            out.append({"target": target, "abstain": True})
        else:
            out.append({"target": target, **draft})
    return out
