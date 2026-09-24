"""Layer 5 prototype: does Laya pick the same decision maker as the Claude autopilot labeler?

Reference = people rows labeled_by='agent' in a COPY of the parked campaign DB (is_dm=1 = Claude's pick).
Usage: python laya_dm_shadow.py <db copy> [n=150] [device=cuda]
"""
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict

import laya

db, n, dev = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 150, sys.argv[3] if len(sys.argv) > 3 else "cuda"
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
people = defaultdict(list)
for r in c.execute("SELECT p.*, b.name AS biz, b.category FROM people p JOIN businesses b ON b.id=p.business_id "
                   "WHERE p.business_id IN (SELECT business_id FROM people WHERE labeled_by='agent')"):
    people[r["business_id"]].append(r)

cases = []
for bid, rows in people.items():
    # one option per distinct person name (the table can hold the same person from several pages)
    by_name = {}
    for r in rows:
        by_name.setdefault(r["name"].strip().lower(), r)
    picks = {r["name"].strip().lower() for r in rows if r["labeled_by"] == "agent" and r["is_dm"] == 1}
    if len(by_name) < 2 or len(picks) != 1:
        continue  # only unambiguous choices: >=2 distinct candidates, exactly one agent pick
    cases.append((bid, list(by_name.values()), picks.pop()))
random.Random(7).shuffle(cases)
cases = cases[:n]

agent = laya.load("convaiinnovations/laya", device=dev)
agree = 0

choices = Counter()
NO_NONE = len(sys.argv) > 4 and sys.argv[4] == "nonone"
t0 = time.time()
for _bid, cands, pick in cases:
    crit = {f"c{i}": f"{r['name']} — {r['title'] or 'no title'}. {(r['snippet'] or '')[:160]}" for i, r in enumerate(cands)}
    if not NO_NONE:
        crit["none"] = "none of these people is the owner or a senior decision maker"
    q = {"dm": {"type": "choice",
                "instructions": "Which person is the owner or most senior decision maker of the business in `business`?",
                "criteria": crit}}
    res = agent.predict({"business": f"{cands[0]['biz']} ({cands[0]['category']})"}, q)
    choice = res["answers"]["dm"]["choice"]
    choices[choice] += 1
    got = cands[int(choice[1:])]["name"].strip().lower() if choice.startswith("c") else None
    agree += got == pick
elapsed = time.time() - t0
print("choice distribution:", choices.most_common(6))
print(f"cases={len(cases)} agree={agree} ({agree / len(cases):.1%}) per_case_ms={elapsed / len(cases) * 1000:.0f} "
      f"device={agent.device} baseline_first_candidate_would_be=see below")
first = sum(1 for _, cands, pick in cases if cands[0]["name"].strip().lower() == pick)
print(f"naive baseline (always first candidate): {first}/{len(cases)} ({first / len(cases):.1%})")
