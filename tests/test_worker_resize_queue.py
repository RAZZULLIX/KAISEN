"""Worker resizes must never destroy queued jobs.

Live add/remove/kill/shrink are runtime knobs, not queue wipers: a job in
the shared FIFO queue survives every resize.  A busy worker's in-flight
job is either finished (graceful retire) or re-queued with the SAME
job_id so the owning engine resolves the same generation.

These tests drive the REAL worker subprocesses against a temp python
project whose build harness sleeps, so 'queued' and 'in-flight' are
observable.
"""
import os
import time

import pytest

from kaisen.workers import get_worker_pool

SLEEP = "1.2"


def _mk_sleepy_project(registry):
    spec = {
        "id": "tq", "name": "Test Q", "language": "python",
        "steps": {
            "build": {"program": "harness/build.py",
                      "args": ["{candidate}", "{artifact}"]},
            "verify": [], "score": [],
        },
        "metrics": {"ms": {"label": "ms", "unit": "ms",
                           "direction": "lower", "weight": 1}},
        "data": {"baseline_source": "baseline.py"},
    }
    p = registry.create("tq", spec)
    (p.path / "baseline.py").write_text("print(0)\n", encoding="utf-8")
    (p.path / "harness").mkdir(exist_ok=True)
    h = p.path / "harness" / "build.py"
    h.write_text("#!/usr/bin/env python3\n"
                 "import sys, time, shutil, os\n"
                 f"time.sleep(float(os.environ.get('KQ_SLEEP', {SLEEP!r})))\n"
                 "shutil.copy(sys.argv[1], sys.argv[2])\n", encoding="utf-8")
    os.chmod(h, 0o755)
    return p


@pytest.fixture
def sleepy_project(tmp_path, registry):
    return _mk_sleepy_project(registry)


def _submit(pool, registry, runs_dir, n):
    jobs = []
    for i in range(n):
        d = runs_dir / f"gen_{i:03d}"
        d.mkdir(parents=True, exist_ok=True)
        cand = d / "candidate.py"
        cand.write_text(f"print({i})\n", encoding="utf-8")
        job = {"job_id": f"job-{i}", "generation": i, "candidate": str(cand),
               "workdir": str(d), "project_id": "tq",
               "registry_root": registry.root}
        jobs.append(job)
        pool.submit(job)
    return jobs


def _pump_until(pool, pred, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        pool.pump()
        if pred():
            return True
        time.sleep(0.1)
    return False


def _wait_busy(pool, n=1):
    """Wait until at least n workers are RUNNING (progress pumped)."""
    def ready():
        return sum(1 for w in pool.list_workers()
                   if w.get("status") == "running") >= n
    assert _pump_until(pool, ready, 15), "no worker reached running state"
    return [w for w in pool.list_workers() if w.get("status") == "running"]


def _drain(pool, results, expected, timeout=60.0):
    assert _pump_until(pool, lambda: len(results) >= expected, timeout), \
        f"only {len(results)}/{expected} results arrived"
    assert sorted(results) == sorted(f"job-{i}" for i in range(expected))


# ----------------------------------------------------------------------

def test_add_worker_keeps_queue(sleepy_project, registry, tmp_path):
    pool = get_worker_pool()
    pool.start(1)
    results = []
    pool.register("tq", result_handler=lambda m: results.append(m["job_id"]))
    _submit(pool, registry, tmp_path / "runs", 4)
    assert _pump_until(pool, lambda: pool.pending() <= 3, 10)
    pool.add_worker()
    # The queue must be untouched by the spawn.
    assert pool.pending() == 3
    _drain(pool, results, 4)


def test_kill_busy_worker_requeues_held_job(sleepy_project, registry, tmp_path):
    pool = get_worker_pool()
    pool.start(1)
    results = []
    pool.register("tq", result_handler=lambda m: results.append(m["job_id"]))
    _submit(pool, registry, tmp_path / "runs", 4)
    assert _pump_until(pool, lambda: pool.pending() <= 3, 10)
    busy = _wait_busy(pool)
    pool.kill_worker(busy[0]["worker_id"])
    # 3 queued + the killed worker's held job back in the queue = 4.
    assert _pump_until(pool, lambda: pool.pending() == 4, 10)
    pool.add_worker()
    _drain(pool, results, 4)


def test_graceful_remove_finishes_inflight_job(sleepy_project, registry, tmp_path):
    pool = get_worker_pool()
    pool.start(1)
    results = []
    pool.register("tq", result_handler=lambda m: results.append(m["job_id"]))
    _submit(pool, registry, tmp_path / "runs", 4)
    assert _pump_until(pool, lambda: pool.pending() <= 3, 10)
    busy = _wait_busy(pool)
    assert pool.remove_worker(busy[0]["worker_id"], kill=False)
    # Graceful: the worker DELIVERS its in-flight result, then exits.
    assert _pump_until(pool, lambda: len(results) >= 1, 20)
    assert pool.worker_count() == 0
    pool.add_worker()
    _drain(pool, results, 4)


def test_remove_idle_worker_is_lossless(sleepy_project, registry, tmp_path):
    pool = get_worker_pool()
    pool.start(1)
    results = []
    pool.register("tq", result_handler=lambda m: results.append(m["job_id"]))
    _submit(pool, registry, tmp_path / "runs", 2)
    assert _pump_until(pool, lambda: len(results) >= 2, 30)
    idle = [w for w in pool.list_workers() if w.get("status") != "running"]
    assert idle
    assert pool.remove_worker(idle[0]["worker_id"], kill=False)
    assert pool.worker_count() == 0
    assert sorted(results) == ["job-0", "job-1"]


def test_shrink_to_zero_is_lossless(sleepy_project, registry, tmp_path):
    pool = get_worker_pool()
    pool.start(2)
    results = []
    pool.register("tq", result_handler=lambda m: results.append(m["job_id"]))
    _submit(pool, registry, tmp_path / "runs", 4)
    assert _pump_until(pool, lambda: all(
        w.get("status") == "running" for w in pool.list_workers())
        and pool.pending() == 2, 15), "both workers must be mid-job"
    assert pool.shrink_to(0) == 0
    # Every job (2 queued + both held) is back in the queue.
    assert _pump_until(pool, lambda: pool.pending() == 4, 10)
    pool.start(2)
    _drain(pool, results, 4)


def test_crashed_worker_requeues_held_job(sleepy_project, registry, tmp_path):
    """A worker that DIES (SIGKILL, OOM, segfault — not an API kill) must
    not take its in-flight job with it: the self-heal in worker_count()
    re-queues the held job."""
    pool = get_worker_pool()
    pool.start(1)
    results = []
    pool.register("tq", result_handler=lambda m: results.append(m["job_id"]))
    _submit(pool, registry, tmp_path / "runs", 4)
    assert _pump_until(pool, lambda: pool.pending() <= 3, 10)
    busy = _wait_busy(pool)
    proc = pool._procs[busy[0]["worker_id"]]
    os.kill(proc.pid, 9)  # simulate a crash
    proc.join(timeout=5)
    # The next poll notices the death, re-queues the job, and self-heals
    # (a replacement worker spawns and starts draining again).
    assert _pump_until(pool, lambda: pool.worker_count() >= 1, 20)
    _drain(pool, results, 4)
