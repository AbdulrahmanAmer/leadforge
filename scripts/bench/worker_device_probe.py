"""Start LeadForge's NER worker the way enrichment does and prove which device it runs on."""
import subprocess
import time

from leadforge.enrich.ner_worker import NerWorker

if __name__ == "__main__":
    t = time.time()
    w = NerWorker(device="auto")
    print(f"device={w.device} available={w.available} start_s={time.time() - t:.1f} err={w.start_error}")
    text = ("Our owner John Smith and workshop manager Sarah Jones lead the team. " * 90)[:6000]
    labels = ["person name", "job title"]
    w.predict(text, labels, 0.4, timeout_s=60)  # warm-up
    t = time.time()
    for _ in range(10):
        ents = w.predict(text, labels, 0.4, timeout_s=60)
    print(f"per_page_ms={(time.time() - t) * 100:.0f} entities={len(ents)} worker_pid={w._proc.pid}")
    smi = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
                         capture_output=True, text=True).stdout
    print("worker on GPU list:", any(line.startswith(f"{w._proc.pid},") for line in smi.splitlines()))
    w.shutdown()
