"""Pill truthfulness: the tps stat is the CURRENT generation's real decode
rate, producer errors never leave ghost sessions bound to a server, bound
prefill sessions count as streaming (green LED), and waiters sit in the
FIFO slot queue."""
import threading
import time

import pytest

from kaisen.config import FrameworkConfig
from kaisen.engine import ProjectEngine, Session
from kaisen.llm import ModelOrchestrator
from kaisen.projects import ProjectRegistry


# ----------------------------------------------------------------------
# session tps = real decode rate of the CURRENT generation
# ----------------------------------------------------------------------

def test_session_tps_is_decode_rate(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("kaisen.engine.time.time", lambda: clock["t"])
    s = Session(0, "code", 1, "prompt")
    assert s.tps == 0.0
    clock["t"] += 600.0                # 10-minute queue wait
    assert s.tps == 0.0                # waiting does not produce tps
    for _ in range(100):
        clock["t"] += 0.1              # 100 tokens over ~10s of decoding
        s.push("x")
    # ~10 tps, NOT 100/(610s) — the queue wait is not part of the speed
    assert 9.9 <= s.tps <= 10.2


def test_session_tps_first_token_floor(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("kaisen.engine.time.time", lambda: clock["t"])
    s = Session(0, "code", 1, "p")
    s.push("x")                        # denominator floored at 1.0s
    assert s.tps <= 1.0


# ----------------------------------------------------------------------
# no ghost sessions after a producer error
# ----------------------------------------------------------------------

def _spec(pid):
    return {
        "id": pid, "name": pid, "language": "c",
        "steps": {"build": {"program": "gcc",
                            "args": ["-O2", "{candidate}", "-o", "{artifact}"]},
                  "verify": [], "score": []},
        "metrics": {"ms": {"direction": "lower"}},
        "engine": {"workers": 1},
        "data": {"baseline_source": "original.c"},
    }


def test_producer_error_finishes_session(tmp_path, monkeypatch):
    """A non-ServerError exception used to escape to the outer catch-all
    WITHOUT session.finish() — the session stayed 'generating' forever,
    bound to its server, with frozen tps polluting the pill.  The producer
    finally guard must finish it."""
    registry = ProjectRegistry(tmp_path / "projects")
    project = registry.create("ghost", _spec("ghost"))
    (project.path / "original.c").write_text("int main(){return 0;}")
    cfg = FrameworkConfig(tmp_path / "config.json")
    cfg.llm["servers"] = [{"id": "fake", "type": "llama",
                           "url": "http://127.0.0.1:1/completion",
                           "max_concurrent": 1, "enabled": True}]
    cfg.llm["active_ids"] = ["fake"]
    orch = ModelOrchestrator(cfg)

    def boom(prompt, **kw):
        raise RuntimeError("simulated non-ServerError crash")

    orch.request_stream = boom
    # the producer retries forever on error — make the backoff instant
    monkeypatch.setattr("kaisen.engine.time.sleep", lambda s: None)
    eng = ProjectEngine(project, orchestrator=orch, registry=registry,
                        worker_count=0)
    eng.start(multi=1, paused=False)
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(s["status"] == "error" and "producer died" in s.get("error", "")
                   for s in eng.sessions.snapshot()):
                break
            time.sleep(0.05)
        eng.stop()
        time.sleep(0.3)
        snap = eng.sessions.snapshot()
        assert any(s["status"] == "error" and "producer died" in s.get("error", "")
                   for s in snap)
        assert not any(s["status"] == "generating" for s in snap)
    finally:
        eng.stop()


# ----------------------------------------------------------------------
# FIFO slot queue: more requests than LLM slots -> requests WAIT
# ----------------------------------------------------------------------

def test_acquire_queues_waiters_until_slot_frees(tmp_path):
    cfg = FrameworkConfig(tmp_path / "config.json")
    cfg.llm["servers"] = [{"id": "one", "type": "llama",
                           "url": "http://127.0.0.1:1/completion",
                           "max_concurrent": 1, "enabled": True}]
    cfg.llm["active_ids"] = ["one"]
    orch = ModelOrchestrator(cfg)
    sA, sB = Session(0, "code", 1, "p"), Session(1, "code", 2, "p")
    got = {}

    def acquire(sess, out):
        out["sid"] = orch._acquire_server(session=sess, min_tier="tiny")

    tA = threading.Thread(target=acquire, args=(sA, got))
    tA.start()
    tA.join(timeout=5)
    assert got["sid"] == "one" and sA.server_id == "one"
    assert sA.waiting is False

    # The single slot is held: B must QUEUE, not bind to the busy server.
    tB = threading.Thread(target=acquire, args=(sB, got))
    tB.start()
    time.sleep(0.3)
    assert tB.is_alive()
    assert sB.server_id is None and sB.waiting is True

    orch.release("one")               # slot frees -> the waiter wakes
    tB.join(timeout=5)
    assert not tB.is_alive()
    assert sB.server_id == "one" and sB.waiting is False
    orch.release("one")


def test_acquire_cancel_wakes_waiter(tmp_path):
    cfg = FrameworkConfig(tmp_path / "config.json")
    cfg.llm["servers"] = [{"id": "one", "type": "llama",
                           "url": "http://127.0.0.1:1/completion",
                           "max_concurrent": 1, "enabled": True}]
    cfg.llm["active_ids"] = ["one"]
    orch = ModelOrchestrator(cfg)
    holder = Session(0, "code", 1, "p")
    holder.cancel.clear()
    assert orch._acquire_server(session=holder, min_tier="tiny") == "one"

    from kaisen.llm import GenerationCancelled
    waiter = Session(1, "code", 2, "p")
    waiter.cancel.set()               # cancelled while waiting for a slot
    with pytest.raises(GenerationCancelled):
        orch._acquire_server(session=waiter, cancel_event=waiter.cancel,
                             min_tier="tiny")
    orch.release("one")
