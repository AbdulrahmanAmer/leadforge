"""Layer 5, attempt 2: same 150 businesses as laya_dm_shadow.py, but the question respects Laya's documented
option token budget (README "high-cardinality" limit; common.py build_sequence truncates options to
(head_max_len-16)/n tokens). Options are short "Name — Title"; evidence snippets move into the state.

Usage: python laya_dm_shadow_v2.py <db copy> [n=150] [device=cuda] [with-none|nonone]
"""
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict

import laya

db = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 150
dev = sys.argv[3] if len(sys.argv) > 3 else "cuda"
with_none = (sys.argv[4] if len(sys.argv) > 4 else "nonone") == "with-none"

c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
people = defaultdict(list)
for r in c.execute("SELECT p.*, b.name AS biz, b.category FROM people p JOIN businesses b ON b.id=p.business_id "
                   "WHERE p.business_id IN (SELECT business_id FROM people WHERE labeled_by='agent')"):
    people[r["business_id"]].append(r)

cases = []
for bid, rows in people.items():
    by_name = {}
    for r in rows:
        by_name.setdefault(r["name"].strip().lower(), r)
    picks = {r["name"].strip().lower() for r in rows if r["labeled_by"] == "agent" and r["is_dm"] == 1}
    if len(by_name) < 2 or len(picks) != 1:
        continue
    cases.append((bid, list(by_name.values()), picks.pop()))
random.Random(7).shuffle(cases)  # identical sample to attempt 1
cases = cases[:n]

agent = laya.load("convaiinnovations/laya", device=dev)
agent.cfg["head_max_len"] = 384  # documented runtime knob; the default 192 is shared by all options
agree = 0
choices = Counter()
t0 = time.time()
for _bid, cands, pick in cases:
    crit = {f"c{i}": f"{r['name']} — {r['title'] or 'no title'}" for i, r in enumerate(cands)}
    if with_none:
        crit["none"] = "nobody listed is the owner or a senior manager"
    evidence = "\n".join(f"c{i}: {(r['snippet'] or r['origin'] or '')[:220]}" for i, r in enumerate(cands))
    state = {"business": f"{cands[0]['biz']} ({cands[0]['category']})", "evidence": evidence}
    q = {"dm": {"type": "choice",
                "instructions": "Which candidate is the owner or most senior decision maker of `business`? "
                                "Use `evidence`; owners, directors and managing directors outrank staff.",
                "criteria": crit}}
    res = agent.predict(state, q)
    choice = res["answers"]["dm"]["choice"]
    choices[choice] += 1
    got = cands[int(choice[1:])]["name"].strip().lower() if choice.startswith("c") else None
    agree += got == pick
elapsed = time.time() - t0
first = sum(1 for _, cands, pick in cases if cands[0]["name"].strip().lower() == pick)
print(f"variant={'with-none' if with_none else 'nonone'} choice distribution: {choices.most_common(6)}")
print(f"cases={len(cases)} agree={agree} ({agree / len(cases):.1%}) per_case_ms={elapsed / len(cases) * 1000:.0f} "
      f"device={agent.device}")
print(f"naive baseline (always first candidate): {first}/{len(cases)} ({first / len(cases):.1%})")
