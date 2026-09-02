# Agent drafting protocol (v0.3, ADR-012)

Input (from `leadforge draft export --campaign <c> --purpose <p>`): NDJSON. Line 1 is a header
(purpose/offer/sender/constraints/instructions), then one packet line per target —

```json
{"target": 2, "grade": "A", "tokens_est": 236, "packet": {"co": "Abbey Service Centre", "city": "Birmingham",
 "facts": [{"k": "category", "v": "Auto repair shop", "src": "maps", "at": "2026-08-31T13:55:23Z"},
           {"k": "site_stale", "v": 2019, "src": "site", "at": "2026-08-31T09:37:46Z"},
           {"k": "dm_name", "v": "Harjit Chadha", "src": "agent", "at": "2026-08-31T13:55:23Z"}],
 "offer": {"what": "...", "value_prop": "..."}, "sender": {"from_name": "GainLev tech"},
 "purpose": "client_campaign", "greeting": "Hi Harjit,", "grade": "A"}}
```

Output (`drafts.ndjson`): one line per packet you drafted —

```json
{"target": 2, "subject": "Quick note for Abbey Service Centre",
 "observation": "Harjit, noticed your site footer still says 2019 - worth a refresh.", "used_fact": "site_stale"}
```

or, when you decide not to draft that target: `{"target": 2, "abstain": true}`.

## Rules

1. **Read `facts` and nothing else.** Every claim in `subject` and `observation` must trace to a `k`/`v` pair in the packet, the
   business name (`co`), or the city — never to outside knowledge, even knowledge that happens to be true.
2. Write ONLY `subject` and `observation`. `greeting`, the offer line, the CTA, the signature, the postal address, the privacy line and
   the opt-out line are filled by the CLI at `draft apply` — never write them yourself, and never repeat them in `observation`.
3. **Cite exactly one fact** as `used_fact` (its `k`), and make sure the drafted text actually references it — the key or its value must
   appear in `subject`+`observation`. Two facts in one line reads as a dossier, not a note someone actually looked at.
4. **Never invent** a number, name, email, URL, or a competitor/social-proof/results claim ("dozens of garages...", "we cut their...",
   "guaranteed") — `draft apply`'s mechanical gate rejects every one of these, unconditionally, before anything is stored.
5. **Negation matters.** If the packet has no `booking` fact, don't claim they take bookings online; if it has one, don't say they
   don't. Same principle for every fact: state only what's there, in the direction it's evidenced.
6. **Respect the greeting, don't re-derive it.** `packet.greeting` already reflects the name-allowed decision (docs/08 owner decision 6)
   — a registry-sourced director name only when corroborated by a second source. If it says `"Hello,"`, do not address anyone by name in
   `observation` either, even if a `dm_name` fact happens to be present from a different corroboration path.
7. **Abstain on `grade: "C"` when there's nothing worth saying.** Grade C means only segment-level facts (`no_website`/`no_social_link`)
   — if the one available fact can't carry a real one-line observation, set `"abstain": true` rather than pad. Abstaining is a normal,
   counted outcome (`insufficient_evidence`), not a failure.
8. Keep `observation` to one sentence and under the packet's `constraints.max_observation_words`; keep `subject` under
   `constraints.max_subject_chars`. Short beats clever.
9. Draft the WHOLE batch in one pass, write `drafts.ndjson`, then run `leadforge draft apply --in drafts.ndjson --json`. Read the digest:
   `rejected` lines have their reasons in the stored message's `gate_json` (`leadforge draft check --in drafts.ndjson --json` re-runs the
   same gate without storing anything, if you want to check first). Fix and re-run only the rejected targets — do not re-derive facts
   from anywhere but the packet you were handed.
10. `leadforge draft render --campaign <c> --out drafts/` writes one `.txt` per stored draft for a human to read before anything is
    approved or sent — drafting never sends anything itself.
