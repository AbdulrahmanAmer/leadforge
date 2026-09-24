"""Laya CPU vs GPU latency on this machine. Usage: python laya_bench.py cpu|cuda"""
import sys
import time

import laya
import torch

dev = sys.argv[1]
t0 = time.time()
agent = laya.load("convaiinnovations/laya", device=dev)
load_s = time.time() - t0
questions = laya.triage_questions()
states = [
    {"message": "Our garage needs a new website, the old one does not work on phones"},
    {"message": "Contact us: John Smith, Owner and Managing Director"},
    {"message": "Domain parked. This domain may be for sale."},
    {"message": "Book your MOT online today, open Monday to Saturday"},
]
agent.predict(states[0], questions)  # warm-up
if dev == "cuda":
    torch.cuda.synchronize()
n = 40
t0 = time.time()
for i in range(n):
    res = agent.predict(states[i % len(states)], questions)
if dev == "cuda":
    torch.cuda.synchronize()
per_ms = (time.time() - t0) / n * 1000
print(f"device={agent.device} dtype={getattr(agent, 'dtype', None)} load_s={load_s:.1f} per_predict_ms={per_ms:.1f}")
if dev == "cuda":
    print(f"gpu_mem_peak_mb={torch.cuda.max_memory_allocated() / 2**20:.0f}")
print("sample:", {k: v.get("choice", v.get("score", v.get("noul"))) for k, v in res["answers"].items()})
