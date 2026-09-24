"""Layer 3 (throughput): tests for the model-worker process (enrich/ner_worker.py) and its wiring into
enrich/runner.py's _people_fn. Offline by default — a fake worker `target` (matching _worker_main's
signature) is injected everywhere, so nothing here imports gliner or torch except the single opt-in
`@pytest.mark.gpu` smoke test (skipped unless LF_GPU_TESTS=1).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from leadforge.config import Config, EnrichCfg
from leadforge.enrich import runner
from leadforge.enrich.extract import extract_people_ner, people_from_ner_entities
from leadforge.enrich.ner_worker import NerWorker, resolve_device
from leadforge.util import pid_alive

_OWNER_SCRIPT = Path(__file__).parent / "_fixtures" / "ner_worker_owner.py"
_SRC_DIR = str(Path(__file__).parent.parent / "src")

FIXTURE_TEXT = "About us. Jane Doe, Owner. Contact John Smith — Managing Director — for quotes."
FIXTURE_URL = "http://example.test/about"

# Canned raw GLiNER-shaped entities for FIXTURE_TEXT, hand-picked to match the real spans so both the
# fake worker and a fake in-thread model return exactly this for the same text.
FIXTURE_ENTITIES = [
    {"start": FIXTURE_TEXT.index("Jane Doe"), "end": FIXTURE_TEXT.index("Jane Doe") + len("Jane Doe"),
     "text": "Jane Doe", "label": "person name", "score": 0.9},
    {"start": FIXTURE_TEXT.index("Owner"), "end": FIXTURE_TEXT.index("Owner") + len("Owner"),
     "text": "Owner", "label": "job title", "score": 0.5},
    {"start": FIXTURE_TEXT.index("John Smith"), "end": FIXTURE_TEXT.index("John Smith") + len("John Smith"),
     "text": "John Smith", "label": "person name", "score": 0.92},
    {"start": FIXTURE_TEXT.index("Managing Director"),
     "end": FIXTURE_TEXT.index("Managing Director") + len("Managing Director"),
     "text": "Managing Director", "label": "job title", "score": 0.6},
]


def _fake_worker_target(req_q, resp_q, ready_q, device, model_name, batch_size, batch_wait_ms):
    """Stand-in for ner_worker._worker_main: no gliner/torch import, canned entities keyed off the
    request text so tests can assert exact matches against the in-thread path."""
    ready_q.put(("ok", device))
    while True:
        req = req_q.get()
        if req is None:
            return
        ents = FIXTURE_ENTITIES if req["text"] == FIXTURE_TEXT else []
        resp_q.put({"id": req["id"], "ok": True, "entities": ents})


def _fake_failing_target(req_q, resp_q, ready_q, device, model_name, batch_size, batch_wait_ms):
    """Model 'load' fails — matches a missing package / bad weights / CUDA-not-actually-there."""
    ready_q.put(("error", "FakeLoadError: boom"))


def _fake_hanging_target(req_q, resp_q, ready_q, device, model_name, batch_size, batch_wait_ms):
    """Starts fine but never answers a request — exercises the per-call timeout path."""
    ready_q.put(("ok", device))
    while True:
        req = req_q.get()
        if req is None:
            return
        time.sleep(30)  # long enough that every test's predict() timeout fires first


@pytest.fixture
def fake_worker():
    w = NerWorker(device="cpu", target=_fake_worker_target, start_timeout_s=10.0)
    assert w.available, w.start_error
    yield w
    w.shutdown()


# --------------------------------------------------------------------------- post-processing parity
def test_worker_and_in_thread_paths_agree_on_post_processing(fake_worker, monkeypatch):
    """The whole point of sharing people_from_ner_entities: the worker path and the legacy in-thread
    path (extract_people_ner) must produce identical PersonCandidate lists for the same entities."""

    class _FakeModel:
        def predict_entities(self, text, labels, threshold):
            return FIXTURE_ENTITIES if text.startswith(FIXTURE_TEXT[:20]) else []

    monkeypatch.setattr("leadforge.enrich.extract._gliner_model", lambda: _FakeModel())
    in_thread = extract_people_ner(FIXTURE_TEXT, FIXTURE_URL)

    ents = fake_worker.predict(FIXTURE_TEXT, ["person name", "job title"], 0.4, timeout_s=5.0)
    via_worker = people_from_ner_entities(ents, FIXTURE_TEXT, FIXTURE_URL)

    assert in_thread, "fixture must actually produce candidates, or this test proves nothing"
    assert [(c.name, c.title, c.snippet, c.context) for c in in_thread] == \
           [(c.name, c.title, c.snippet, c.context) for c in via_worker]


def test_worker_predict_returns_same_entities_it_was_given():
    w = NerWorker(device="cpu", target=_fake_worker_target, start_timeout_s=10.0)
    try:
        ents = w.predict(FIXTURE_TEXT, ["person name", "job title"], 0.4, timeout_s=5.0)
        assert ents == FIXTURE_ENTITIES
    finally:
        w.shutdown()


# --------------------------------------------------------------------------- fallback paths
def test_worker_start_failure_marks_unavailable():
    w = NerWorker(device="cpu", target=_fake_failing_target, start_timeout_s=10.0)
    try:
        assert not w.available
        assert "boom" in (w.start_error or "")
    finally:
        w.shutdown()


def test_people_fn_falls_back_to_heuristic_when_worker_unavailable(monkeypatch):
    monkeypatch.setattr(runner, "ner_available", lambda: True)
    runner._ner_worker_state["worker"] = None
    runner._ner_worker_state["fallback_logged"] = False
    runner._ner_worker_state["fallback_count"] = 0
    cfg = Config()
    fn = runner._people_fn(cfg)
    assert fn is runner.extract_people


def test_worker_request_timeout_falls_back_to_heuristic(monkeypatch):
    w = NerWorker(device="cpu", target=_fake_hanging_target, start_timeout_s=10.0)
    try:
        assert w.available
        monkeypatch.setattr(runner, "ner_available", lambda: True)
        runner._ner_worker_state["worker"] = w
        runner._ner_worker_state["fallback_logged"] = False
        runner._ner_worker_state["fallback_count"] = 0
        cfg = Config()
        cfg.enrich.ner_timeout_s = 0.2
        fn = runner._people_fn(cfg)
        out = fn(FIXTURE_TEXT, FIXTURE_URL)  # must not raise / must not hang the caller
        assert isinstance(out, list)
        assert runner._ner_worker_state["fallback_count"] == 1
    finally:
        w.shutdown()
        runner._ner_worker_state["worker"] = None


def test_ner_disabled_uses_heuristic_without_starting_a_worker(monkeypatch):
    cfg = Config()
    cfg.enrich.ner = False
    runner._start_ner_worker(cfg)
    try:
        assert runner._ner_worker_state["worker"] is None
        assert runner._people_fn(cfg) is runner.extract_people
    finally:
        runner._stop_ner_worker()


# --------------------------------------------------------------------------- shutdown / no orphans
def test_worker_shutdown_leaves_no_live_process():
    w = NerWorker(device="cpu", target=_fake_worker_target, start_timeout_s=10.0)
    assert w.available
    proc = w._proc
    assert proc.is_alive()
    w.shutdown()
    assert not proc.is_alive()
    assert not w.available


def test_worker_shutdown_is_idempotent():
    w = NerWorker(device="cpu", target=_fake_worker_target, start_timeout_s=10.0)
    w.shutdown()
    w.shutdown()  # must not raise


def test_worker_exits_when_owner_process_dies():
    """Orphan guard (found live 2026-09-24 by process-list inspection: a worker survived after its
    owning process was gone). Spawns a real separate OS process ("owner") that starts a NerWorker
    (fake target, no gliner/torch), reports the worker's pid, then kills the owner directly — bypassing
    its shutdown()/finally entirely, the same way a crashed test runner or a hard kill would. The
    worker's own parent-liveness watchdog (ner_worker._start_parent_watchdog, polling every
    PARENT_WATCHDOG_POLL_S) must notice and exit on its own within a bounded time."""
    from leadforge.enrich.ner_worker import PARENT_WATCHDOG_POLL_S

    owner = subprocess.Popen([sys.executable, str(_OWNER_SCRIPT), _SRC_DIR],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = owner.stdout.readline().strip()
        assert line.startswith("WORKER_PID "), f"owner process failed to start: {line!r}"
        worker_pid = int(line.split()[1])
        assert pid_alive(worker_pid)

        owner.kill()  # simulate a hard kill of the owner — no cleanup path runs
        owner.wait(timeout=10)

        deadline = time.monotonic() + PARENT_WATCHDOG_POLL_S + 8.0
        while time.monotonic() < deadline and pid_alive(worker_pid):
            time.sleep(0.5)
        assert not pid_alive(worker_pid), \
            f"worker pid {worker_pid} outlived its killed owner past the watchdog deadline"
    finally:
        if owner.poll() is None:
            owner.kill()


# --------------------------------------------------------------------------- config validation
def test_ner_device_accepts_canonical_values():
    for v in ("auto", "cpu", "cuda"):
        assert EnrichCfg(ner_device=v).ner_device == v


def test_ner_device_normalizes_case():
    assert EnrichCfg(ner_device="CUDA").ner_device == "cuda"
    assert EnrichCfg(ner_device="Auto").ner_device == "auto"


def test_ner_device_rejects_bogus_value():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        EnrichCfg(ner_device="tpu")


def test_resolve_device_cpu_and_cuda_pass_through():
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("cuda") == "cuda"


def test_resolve_device_auto_is_cpu_or_cuda():
    assert resolve_device("auto") in ("cpu", "cuda")


# --------------------------------------------------------------------------- opt-in real-GPU smoke test
@pytest.mark.gpu
@pytest.mark.skipif(os.environ.get("LF_GPU_TESTS") != "1", reason="set LF_GPU_TESTS=1 to run the real-GPU smoke test")
def test_gpu_worker_extracts_a_name_from_a_fixture_page():
    """Really loads GLiNER on cuda (no fakes) and extracts a name from a fixture page — run once with
    LF_GPU_TESTS=1 and the output pasted into the delivery report, per the task's GATES."""
    pytest.importorskip("gliner")
    pytest.importorskip("torch")
    import torch
    assert torch.cuda.is_available(), "this smoke test requires an actual CUDA device"

    w = NerWorker(device="cuda", start_timeout_s=120.0)
    try:
        assert w.available, w.start_error
        assert w.started_device == "cuda"
        text = "Welcome to Acme Roofing. Our founder, Sarah Connolly, has run the business since 2004."
        ents = w.predict(text, ["person name", "job title"], 0.4, timeout_s=30.0)
        names = [e["text"] for e in ents if e["label"] == "person name"]
        assert any("Connolly" in n for n in names), ents
        candidates = people_from_ner_entities(ents, text, "http://acme-roofing.test")
        assert any("Connolly" in c.name for c in candidates), candidates
    finally:
        w.shutdown()
