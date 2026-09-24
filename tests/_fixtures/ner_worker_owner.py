"""Helper process for test_worker_exits_when_owner_process_dies (tests/test_ner_worker.py). Run as its
own OS process: starts a NerWorker (fake target — no gliner/torch import), prints the worker child's
pid, then blocks forever waiting to be killed. The test kills THIS process and asserts the worker
(this process's own child, i.e. the grandchild of the test process) exits within a few seconds via its
parent-liveness watchdog (ner_worker._start_parent_watchdog).
"""

from __future__ import annotations

import sys
import time


def _fake_worker_target(req_q, resp_q, ready_q, device, model_name, batch_size, batch_wait_ms):
    ready_q.put(("ok", device))
    while True:
        req = req_q.get()
        if req is None:
            return


if __name__ == "__main__":
    sys.path.insert(0, sys.argv[1])  # src dir, passed by the test
    from leadforge.enrich.ner_worker import NerWorker

    w = NerWorker(device="cpu", target=_fake_worker_target, start_timeout_s=10.0)
    print(f"WORKER_PID {w._proc.pid}", flush=True)
    time.sleep(3600)  # the test kills us long before this; the worker's watchdog then exits it
