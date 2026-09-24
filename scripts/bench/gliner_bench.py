"""GLiNER (LeadForge's NER model) CPU vs GPU. Usage: python gliner_bench.py cpu|cuda"""
import sys
import time

import torch
from gliner import GLiNER

dev = sys.argv[1]
t0 = time.time()
model = GLiNER.from_pretrained("urchade/gliner_small-v2.1").to(dev)
load_s = time.time() - t0
para = ("Welcome to Smith & Sons Garage, a family-run MOT and servicing centre in Leeds since 1987. "
        "Our owner John Smith and workshop manager Sarah Jones lead a team of six qualified technicians. "
        "Contact our service advisor Mark Taylor on 0113 496 0000 or email info@smithgarage.co.uk. ")
text = (para * 20)[:6000]  # LeadForge caps page text at 6000 chars (extract.py)
labels = ["person name", "job title"]
model.predict_entities(text, labels, threshold=0.4)  # warm-up
if dev == "cuda":
    torch.cuda.synchronize()
n = 20
t0 = time.time()
for _ in range(n):
    ents = model.predict_entities(text, labels, threshold=0.4)
if dev == "cuda":
    torch.cuda.synchronize()
print(f"device={dev} torch_threads={torch.get_num_threads()} load_s={load_s:.1f} "
      f"per_page_ms={(time.time() - t0) / n * 1000:.0f} entities={len(ents)}")
if dev == "cuda":
    print(f"gpu_mem_peak_mb={torch.cuda.max_memory_allocated() / 2**20:.0f}")
