"""Layer 3 throughput (docs/UPGRADE-PLAN.md): a single model-worker PROCESS that owns GLiNER so crawl
threads never run torch inference themselves. Today's only caller is enrich/runner.py's worker-backed
`_people_fn`; enrich/extract.py's `extract_people_ner` (in-thread) stays as its fallback target and as
direct test coverage — both share post-processing via `extract.people_from_ner_entities` so results
cannot drift between the two paths.

Design
------
- ONE child process (multiprocessing, spawn start method — required on Windows) loads the model once
  on `enrich.ner_device` (auto/cpu/cuda) and serves requests pulled off a queue.
- Every request carries a `task` name; only "ner" exists today. A future model (Laya, Layer 5) can be
  added to the SAME process by teaching `_serve_batch` a new task — one CUDA context, one model memory
  budget, no second process.
- Requests are micro-batched: the worker collects up to `ner_batch` requests, waiting at most
  `ner_batch_wait_ms` past the first one in the batch, then serves them in one call. It uses GLiNER's
  `batch_predict_entities` when the installed version has it (checked at runtime via getattr — see
  `_worker_main`); otherwise it serves the batch sequentially through `predict_entities`, which is
  still correct, just not batched on the GPU.
- The client (`NerWorker`) is used from many crawl threads concurrently: each call blocks on its own
  `threading.Event` with a caller-supplied timeout, so one slow/hung request can never block another
  crawl thread's own predict() call — only that one caller's — and the crawl pool degrades to the
  heuristic path per-site, not run-wide, on a timeout.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

from leadforge.util import LOG, pid_alive

PARENT_WATCHDOG_POLL_S = 3.0  # how often the child checks its parent is still alive

MODEL_NAME = "urchade/gliner_small-v2.1"


def resolve_device(requested: str) -> str:
    """auto -> cuda when torch.cuda.is_available(), else cpu. cpu/cuda pass through unchanged (an
    explicit 'cuda' on a CPU-only box is a config error we surface as a worker start failure, not one
    we silently downgrade — the operator asked for cuda)."""
    requested = (requested or "auto").strip().lower()
    if requested in ("cpu", "cuda"):
        return requested
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 — no torch at all: cpu is moot anyway, NER will be unavailable
        return "cpu"


def _start_parent_watchdog(parent_pid: int) -> None:
    """Daemon thread: os._exit(1) the moment the parent pid disappears, so this process can never
    outlive an owner that died without running its shutdown() (a hard kill, an OOM, a crashed test
    worker — anything that skips the normal finally/atexit path)."""

    def _watch() -> None:
        while True:
            time.sleep(PARENT_WATCHDOG_POLL_S)
            if not pid_alive(parent_pid):
                os._exit(1)  # no Python cleanup is reachable/needed here; this IS the cleanup

    t = threading.Thread(target=_watch, name="ner-worker-parent-watchdog", daemon=True)
    t.start()


def _process_entry(target, req_q, resp_q, ready_q, device: str, model_name: str,
                    batch_size: int, batch_wait_ms: float) -> None:
    """Actual multiprocessing.Process target — always this, never `target` directly (see NerWorker._start).
    Starts the parent-liveness watchdog BEFORE handing off to the real worker loop (`_worker_main` in
    production, a fake target in tests) so orphan protection covers every worker, fake or real.

    Orphan guard (found 2026-09-24 live: a worker outlived a parent that was reaped without running its
    finally/shutdown path — e.g. the parent process itself was killed, not just the Python object
    GC'd): a daemon thread polls the parent pid every few seconds via leadforge.util.pid_alive and
    calls os._exit() the moment the parent is gone, so a dead owner can never leave this process
    (holding up to ~0.7 GB of GPU memory) running unbounded. daemon=True on the Process object is the
    other half — belt and suspenders, since a hard kill of the parent bypasses both."""
    _start_parent_watchdog(os.getppid())
    target(req_q, resp_q, ready_q, device, model_name, batch_size, batch_wait_ms)


def _worker_main(req_q, resp_q, ready_q, device: str, model_name: str,
                  batch_size: int, batch_wait_ms: float) -> None:
    """Loads the model once, reports readiness on `ready_q`, then serves requests until it receives
    the shutdown sentinel (None) on `req_q`. Always run inside _process_entry (see above), never
    passed to multiprocessing.Process directly."""
    model = None
    batch_predict = None
    try:
        from gliner import GLiNER
        model = GLiNER.from_pretrained(model_name)
        try:
            model = model.to(device)
        except Exception:  # noqa: BLE001 — device move failed; keep going on whatever .to() left it on
            pass
        batch_predict = getattr(model, "batch_predict_entities", None)
        ready_q.put(("ok", device))
    except Exception as e:  # noqa: BLE001 — the parent's start_timeout/request-timeout is the fallback
        ready_q.put(("error", f"{type(e).__name__}: {e}"))
        return

    while True:
        try:
            first = req_q.get(timeout=1.0)
        except queue.Empty:
            continue
        if first is None:  # shutdown sentinel
            return
        batch = [first]
        deadline = time.monotonic() + (batch_wait_ms / 1000.0)
        while len(batch) < batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = req_q.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                for req in batch:
                    resp_q.put({"id": req["id"], "ok": False, "error": "worker_shutdown"})
                return
            batch.append(item)
        _serve_batch(batch, model, batch_predict, resp_q)


def _serve_batch(batch: list[dict], model, batch_predict, resp_q) -> None:
    """Group by (task, labels, threshold) — everything in today's caller shares one signature, but a
    future task (Laya) won't, so batch_predict_entities only ever sees requests that share a call."""
    texts = [r["text"] for r in batch]
    groups: dict[tuple, list[int]] = {}
    for i, r in enumerate(batch):
        key = (r.get("task", "ner"), tuple(r["labels"]), r["threshold"])
        groups.setdefault(key, []).append(i)
    results: dict[int, object] = {}
    for (task, labels, threshold), idxs in groups.items():
        group_texts = [texts[i] for i in idxs]
        try:
            if task != "ner":
                raise ValueError(f"unknown task: {task!r}")
            if batch_predict is not None and len(group_texts) > 1:
                preds = batch_predict(group_texts, list(labels), threshold=threshold)
            else:
                preds = [model.predict_entities(t, list(labels), threshold=threshold) for t in group_texts]
            for i, ents in zip(idxs, preds, strict=True):
                results[i] = ents
        except Exception as e:  # noqa: BLE001 — one bad group never sinks the rest of the batch
            for i in idxs:
                results[i] = ("__error__", f"{type(e).__name__}: {e}")
    for i, r in enumerate(batch):
        val = results.get(i)
        if isinstance(val, tuple) and val and val[0] == "__error__":
            resp_q.put({"id": r["id"], "ok": False, "error": val[1]})
        else:
            resp_q.put({"id": r["id"], "ok": True, "entities": val or []})


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None


class NerWorker:
    """Owns the child process + queues for one enrichment run. `predict()` is safe to call from many
    crawl threads concurrently; the process/queues are created once, in __init__, and torn down by
    `shutdown()` — callers (run_enrich) MUST call shutdown() when the run ends so no child process is
    left orphaned.

    `worker_factory`/`process_factory` exist purely for tests: passing a fake model-loading target lets
    tests exercise start-failure/timeout/shutdown without ever importing gliner or torch."""

    def __init__(self, device: str = "auto", model_name: str = MODEL_NAME,
                 batch_size: int = 8, batch_wait_ms: float = 25.0, start_timeout_s: float = 60.0,
                 mp_context: str = "spawn", target=_worker_main) -> None:
        self.requested_device = device
        self.device = resolve_device(device)
        self.model_name = model_name
        self.batch_size = max(1, int(batch_size))
        self.batch_wait_ms = max(0.0, float(batch_wait_ms))
        self._ctx = mp.get_context(mp_context)
        self._req_q = self._ctx.Queue()
        self._resp_q = self._ctx.Queue()
        self._ready_q = self._ctx.Queue()
        self._target = target
        self._proc: mp.process.BaseProcess | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._stopped = False
        self.started_device: str | None = None
        self.start_error: str | None = None
        self._start(start_timeout_s)

    def _start(self, timeout_s: float) -> None:
        self._proc = self._ctx.Process(
            target=_process_entry,
            args=(self._target, self._req_q, self._resp_q, self._ready_q, self.device, self.model_name,
                  self.batch_size, self.batch_wait_ms),
            daemon=True, name="leadforge-ner-worker")
        self._proc.start()
        try:
            status, info = self._ready_q.get(timeout=timeout_s)
        except queue.Empty:
            self.start_error = f"worker did not become ready within {timeout_s}s"
            LOG.warning("ner worker failed to start: %s", self.start_error)
            self._terminate()
            return
        if status != "ok":
            self.start_error = info
            LOG.warning("ner worker failed to start: %s", info)
            self._terminate()
            return
        self.started_device = info
        self._reader = threading.Thread(target=self._read_loop, name="ner-worker-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._stopped:
            try:
                resp = self._resp_q.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, OSError):  # noqa: BLE001 — process torn down mid-read
                return
            with self._lock:
                pending = self._pending.pop(resp["id"], None)
            if pending is not None:
                pending.result = resp
                pending.event.set()

    @property
    def available(self) -> bool:
        return self._proc is not None and self._proc.is_alive() and self.start_error is None

    def predict(self, text: str, labels: list[str], threshold: float, timeout_s: float = 5.0,
                task: str = "ner") -> list:
        """-> raw entity dicts, same shape as GLiNER's own predict_entities(). Raises TimeoutError /
        RuntimeError on timeout / worker failure; the caller (enrich/runner.py's worker-backed
        _people_fn) catches these and falls back to the heuristic extractor for that one call."""
        if not self.available:
            raise RuntimeError(self.start_error or "ner worker is not running")
        req_id = uuid.uuid4().hex
        pending = _Pending()
        with self._lock:
            self._pending[req_id] = pending
        self._req_q.put({"id": req_id, "task": task, "text": text, "labels": labels, "threshold": threshold})
        got = pending.event.wait(timeout_s)
        with self._lock:
            self._pending.pop(req_id, None)
        if not got:
            raise TimeoutError(f"ner worker timed out after {timeout_s}s")
        resp = pending.result
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "ner worker request failed")
        return resp["entities"]

    def shutdown(self, timeout_s: float = 5.0) -> None:
        """Idempotent. Sends the shutdown sentinel, joins the child process, and stops the reader
        thread — no orphan process survives a call to this (proved by
        test_worker_shutdown_leaves_no_live_process)."""
        if self._proc is None or self._stopped:
            self._stopped = True
            return
        self._stopped = True
        try:
            self._req_q.put(None)
        except Exception:  # noqa: BLE001 — queue may already be broken; terminate() below is the backstop
            pass
        self._proc.join(timeout_s)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(2.0)
        if self._reader is not None:
            self._reader.join(2.0)
        self._close_queues()

    def _terminate(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(2.0)
        self._stopped = True
        self._close_queues()

    def _close_queues(self) -> None:
        for q in (self._req_q, self._resp_q, self._ready_q):
            try:
                q.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup only
                pass
