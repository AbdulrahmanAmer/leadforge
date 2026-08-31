# DM labeling protocol

Input (from `leadforge dm export`): NDJSON, one line per business —

```json
{"biz":"biz_4f2a09c1e7b3","name":"Joe's Transmission","category":"auto repair shop","icp_titles":["Owner","General Manager","Service Manager"],"candidates":[{"i":0,"name":"Joe Alvarez","title":"Owner","snippet":"Joe Alvarez founded Joe's Transmission in 2004 and still runs the shop floor..."},{"i":1,"name":"Maria Chen","title":"Office Manager","snippet":"...Maria Chen keeps the office humming, handling scheduling and billing..."}]}
```

Output (`dm_labels.ndjson`): one line per input line —

```json
{"biz":"biz_4f2a09c1e7b3","pick":0,"confidence":0.9,"title_override":null}
```

## Rules

1. **Snippets only.** Never browse to verify. Provenance URLs are already stored for the human.
2. Pick the candidate whose ROLE best matches `icp_titles` order — actual authority over the offer decision, not seniority for its own
   sake (for a marketing offer, a "Marketing Manager" beats a "CFO").
3. `pick:-1` when: no candidate plausibly decides (all technicians/staff), names look like testimonials/customers, or the "person" is
   likely a brand/franchise figure. Better no DM than a wrong DM.
4. `confidence`: 0.9+ exact title match + founder-style snippet · 0.6–0.8 plausible authority ("Manager" at a small shop) · ≤ 0.5 use
   `pick:-1` instead.
5. `title_override`: set only when the snippet clearly shows a better title than the extracted one (e.g. extracted "team member", snippet
   says "co-owner").
6. Ambiguous duo (two owners): pick the one tied to operations/commercial decisions; mention nothing to the user — the sheet shows your
   confidence.
7. Label the WHOLE batch in one pass, write the file, run `leadforge dm apply --in dm_labels.ndjson --json`. If the export digest showed
   `remaining > 0`, loop.

`--tsv` variant: same semantics, tab-separated `biz  pick  confidence  title_override` — use if you prefer terser output.
