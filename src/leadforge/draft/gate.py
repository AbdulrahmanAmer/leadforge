"""The mechanical no-fabrication gate (v0.3 unit F, docs/09 Wave 2 F). Pure, no I/O.

`check_draft(packet, draft) -> {"ok": bool, "reasons": [str]}` is the single entry point, called by
`draft apply`/`draft check` AND directly by the orchestrator's own gate script with a packet/draft
pair it built itself — so this stays tolerant of a packet missing any optional key (only `facts`,
read as a possibly-empty list, is assumed to exist at all; everything else uses `.get()`).

What it checks, every draft against its own packet:
  NUMBER   every number in subject+observation must appear in the packet JSON or the skeleton's
           `constraints.template_numbers`.
  EMAIL/URL every email/URL-looking substring must appear verbatim in the packet JSON.
  PROPER NOUN every capitalised multi-word phrase, and every capitalised word not at the start of a
           sentence, must appear in the packet JSON (business name, city, legal name, allowed DM
           name, sender name, offer text — or the skeleton's whitelisted `constraints.literals`).
  USED_FACT the cited `used_fact` key must exist in `packet["facts"]`, and its key or value must
           actually be referenced in the drafted text (loosely: any token >3 chars from the key, or
           the value itself).
  LENGTH   subject <= constraints.max_subject_chars; observation <= constraints.max_observation_words.
  BANNED   a small set of unverifiable social-proof / results / competitor claims.
  NEGATION a packet fact `booking` being present (truthy) means the draft may not deny online
           booking; its absence means the draft may not claim one.
"""

from __future__ import annotations

import json
import re
from typing import Any

_BANNED_PATTERNS = [
    re.compile(r"\b(dozens|hundreds|many)\s+of\s+(garages|businesses|clients|companies)\b", re.I),
    re.compile(r"\bwe(?:'ve| have)?\s+(cut|reduced|increased|saved)\b", re.I),
    re.compile(r"\bcompetitors?\b", re.I),
    re.compile(r"\bmost\s+(garages|businesses)\b", re.I),
    re.compile(r"\bguarantee(?:d|s)?\b", re.I),
]

_ALNUM = r"[A-Za-z0-9'\-]*"  # digits included so an identifier like "A1"/"MOT4" stays one token,
                             # never split into a bare letter + a bare number that each fail alone
_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?%?(?![A-Za-z])")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL_RE = re.compile(r"(?:https?://\S+|www\.[^\s,;]+)", re.I)
_MULTI_CAP_RE = re.compile(rf"\b[A-Z]{_ALNUM}(?:\s+[A-Z]{_ALNUM})+\b")
_WORD_RE = re.compile(rf"[A-Za-z]{_ALNUM}")

_BOOKING_DENY_RE = re.compile(
    r"\b(no online book\w*|don'?t take bookings?|do not take bookings?|"
    r"doesn'?t take bookings? online|can'?t book online|cannot book online)\b", re.I,
)
_BOOKING_CLAIM_RE = re.compile(
    r"\b(book\w* (?:online|a slot online|an appointment online)|take bookings? online|"
    r"you can book online)\b", re.I,
)


def _haystack(packet: dict) -> str:
    return json.dumps(packet, ensure_ascii=False)


def _contains_word(haystack: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", haystack) is not None


def _interior_segments(text: str) -> list[str]:
    """Per sentence, the text AFTER its first word — dropped because ordinary English capitalises a
    sentence's first word regardless of evidence. Both proper-noun checks below scan only this: a
    multi-word phrase starting right after a sentence boundary ("Acme Garage Ltd has ...") must still
    be checked on 'Acme Garage Ltd', not swallow the sentence-initial word into one unmatchable run."""
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        m = re.match(rf"^\s*[A-Za-z]{_ALNUM}", sent)
        if m:
            out.append(sent[m.end():])
    return out


def _used_fact_referenced(key: str, value: Any, text: str) -> bool:
    text_cf = text.casefold()
    if str(value) and str(value).casefold() in text_cf:
        return True
    key_words = [t for t in re.split(r"[_\s]+", str(key)) if len(t) > 3]
    return any(t.casefold() in text_cf for t in key_words) or str(key).casefold() in text_cf


def check_draft(packet: dict, draft: dict) -> dict:
    packet = packet or {}
    draft = draft or {}
    reasons: list[str] = []

    subject = str(draft.get("subject") or "")
    observation = str(draft.get("observation") or "")
    # NUMBER/EMAIL/URL/BANNED/NEGATION scan the two slots together; PROPER NOUN checks each slot
    # SEPARATELY (below) — joining them here would let the multi-word regex chain across the
    # subject/observation boundary (`\s+` matches the newline) and would treat the observation's
    # first word as merely "sentence-interior" instead of its own sentence start.
    text = f"{subject}\n{observation}"
    haystack = _haystack(packet)

    constraints = packet.get("constraints") or {}
    template_numbers = {str(n) for n in (constraints.get("template_numbers") or [])}
    literals = set(constraints.get("literals") or [])

    # NUMBER
    for m in _NUMBER_RE.findall(text):
        bare = m.rstrip("%")
        if m in template_numbers or bare in template_numbers:
            continue
        if _contains_word(haystack, m) or _contains_word(haystack, bare):
            continue
        reasons.append(f"NUMBER: '{m}' is not in the packet or template_numbers")

    # EMAIL
    for m in _EMAIL_RE.findall(text):
        if m not in haystack:
            reasons.append(f"EMAIL: '{m}' is not in the packet")

    # URL
    for m in _URL_RE.findall(text):
        if m not in haystack:
            reasons.append(f"URL: '{m}' is not in the packet")

    # PROPER NOUN — each slot checked separately (see the `text` comment above), each sentence's own
    # first word dropped first (see _interior_segments)
    for slot in (subject, observation):
        for seg in _interior_segments(slot):
            for m in _MULTI_CAP_RE.findall(seg):
                if m in literals or _contains_word(haystack, m):
                    continue
                reasons.append(f"PROPER_NOUN: '{m}' is not in the packet or literals")
            for w in _WORD_RE.findall(seg):
                if not w[:1].isupper() or w in literals or _contains_word(haystack, w):
                    continue
                reasons.append(f"PROPER_NOUN: '{w}' is not in the packet or literals")

    # USED_FACT
    used_fact = draft.get("used_fact")
    facts = packet.get("facts") or []
    if not used_fact:
        reasons.append("USED_FACT: draft cites no used_fact")
    else:
        fact_row = next((f for f in facts if f.get("k") == used_fact), None)
        if fact_row is None:
            reasons.append(f"USED_FACT: '{used_fact}' is not a packet fact")
        elif not _used_fact_referenced(used_fact, fact_row.get("v"), text):
            reasons.append(f"USED_FACT: neither the key '{used_fact}' nor its value is referenced")

    # LENGTH
    max_subject = int(constraints.get("max_subject_chars") or 60)
    max_words = int(constraints.get("max_observation_words") or 45)
    if len(subject) > max_subject:
        reasons.append(f"LENGTH: subject is {len(subject)} chars (max {max_subject})")
    n_words = len(observation.split())
    if n_words > max_words:
        reasons.append(f"LENGTH: observation is {n_words} words (max {max_words})")

    # BANNED
    for pat in _BANNED_PATTERNS:
        if pat.search(text):
            reasons.append(f"BANNED: matched an unverifiable-claim pattern ({pat.pattern})")

    # NEGATION
    booking_fact = next((f for f in facts if f.get("k") == "booking"), None)
    booking_true = bool(booking_fact and booking_fact.get("v"))
    if booking_true and _BOOKING_DENY_RE.search(text):
        reasons.append("NEGATION: packet shows online booking, but the draft denies it")
    if not booking_true and _BOOKING_CLAIM_RE.search(text):
        reasons.append("NEGATION: draft claims online booking, but the packet shows none")

    return {"ok": not reasons, "reasons": reasons}
