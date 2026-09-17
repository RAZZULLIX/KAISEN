"""Fair worker dispatch: jobs are handed to free workers in ROTATION
between projects — the same fairness the LLM pool gives generations.

Contract under test (drives the REAL worker subprocesses against sleepy
projects, so "queued" and "in flight" are observable):

  - no project can hog the pool: two projects with jobs waiting ALTERNATE,
    they never run one project's whole backlog first;
  - a project's `max_workers` caps ITS outstanding jobs and never throttles
    the other projects;
  - `reserve_workers` guarantees a project slots without waiting its turn;
  - the ONLY queue is the job queue, and it is bounded by the TOTAL worker
    allowance (`workers.max_count`) — never per project.
"""
import os
import time

import pytest

from kaisen.config import get_config
from kaisen.workers import get_worker_pool

SLEEP = "0.8"


def _mk_project(registry, pid):
    spec = {
        "id": pid, "name": pid, "language": "python",
        "steps": {
            "build": {"program": "harness/build.py",
                      "args": ["{candidate}", "{artifact}"]},
            "verify": [], "score": [],
        },
        "metrics": {"ms": {"label": "ms", "unit": "ms",
                           "direction": "lower", "weight": 1}},
        "data": {"baseline_source": "baseline.py"},
    }
    p = registry.create(pid, spec)
    (p.path / "baseline.py").write_text("print(0)\n", encoding="utf-8")
    (p.path / "harness").mkdir(exist_ok=True)
    h = p.path / "harness" / "build.py"
    h.write_text("#!/usr/bin/env python3\n"
                 "import sys, time, shutil, os\n"
                 f"time.sleep(float(os.environ.get('KQ_SLEEP', {SLEEP!r})))\n"
                 "shutil.copy(sys.argv[1], sys.argv[2])\n", encoding="utf-8")
    os.chmod(h, 0o755)
    return p


def _submit(pool, registry, runs_dir, pid, n, tag=""):
    for i in range(n):
        d = runs_dir / f"{pid}_{tag}gen_{i:03d}"
        d.mkdir(parents=True, exist_ok=True)
        cand = d / "candidate.py"
        cand.write_text(f"print({i})\n", encoding="utf-8")
        pool.submit({"job_id": f"{pid}{tag}-job-{i}", "generation": i,
                     "candidate": str(cand), "workdir": str(d),
                     "project_id": pid, "registry_root": registry.root})


def _pump_until(pool, pred, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        pool.pump()
        if pred():
            return True
        time.sleep(0.05)
    return False


def _sample_peak_running(pool, seconds):
    """Highest CONCURRENT running count per project over a window.  Peak
    sampling is the robust way to observe a cap: it does not depend on
    catching a transient steady state between polls."""
    peak: dict = {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        pool.pump()
        for proj, n in (pool.queue_depth().get("running") or {}).items():
            peak[proj] = max(peak.get(proj, 0), n)
        time.sleep(0.02)
    return peak


def _started_order(pool, n, timeout=30.0):
    """The order in which N jobs BEGAN, read from the progress messages
    (stage != idle) — the dispatcher's rotation made observable."""
    seen = []
    t0 = time.time()
    while len(seen) < n and time.time() - t0 < timeout:
        pool.pump()
        for w in pool.list_workers():
            pid = w.get("project_id")
            gen = w.get("generation")
            stage = w.get("stage")
            if pid and gen is not None and stage and stage != "idle":
                key = (pid, int(gen))
                if key not in seen:
                    seen.append(key)
        time.sleep(0.05)
    return seen


@pytest.fixture
def pool():
    p = get_worker_pool()
    yield p
    p.stop_all()


def test_jobs_rotate_between_projects(tmp_path, registry, pool):
    """Two projects, one worker, two jobs each: the jobs must ALTERNATE
    (A, B, A, B), not drain one project before the other."""
    _mk_project(registry, "pa")
    _mk_project(registry, "pb")
    pool.start(1)
    pool.register("pa")
    pool.register("pb")
    _submit(pool, registry, tmp_path / "runs", "pa", 2)
    _submit(pool, registry, tmp_path / "runs", "pb", 2)
    order = _started_order(pool, 4, timeout=40)
    projects = [pid for pid, _gen in order]
    assert len(order) == 4, f"only {order}"
    # one worker, both projects waiting: the jobs ALTERNATE
    assert projects == ["pa", "pb", "pa", "pb"], order


def test_max_workers_caps_one_project_only(tmp_path, registry, pool):
    """`max_workers` limits ONE project's outstanding jobs; the other
    project keeps using the pool."""
    _mk_project(registry, "cap")
    _mk_project(registry, "free")
    pool.start(3)
    pool.register("cap", max_workers=1)
    pool.register("free")
    done = []
    pool.register("cap", result_handler=lambda m: done.append(m["job_id"]))
    _submit(pool, registry, tmp_path / "runs", "cap", 3)   # never blocks
    _submit(pool, registry, tmp_path / "runs", "free", 1)
    # While both projects have work, the capped project never runs two jobs
    # at once — and the other project is served regardless.
    peak = _sample_peak_running(pool, 6.0)
    assert peak.get("cap", 0) == 1, peak
    assert peak.get("free", 0) == 1, peak
    pool.set_limits("cap", max_workers=0)                # clear the cap
    assert _pump_until(pool, lambda: len(done) >= 3, 60), done


def test_reserve_workers_are_guaranteed_slots(tmp_path, registry, pool):
    """`reserve_workers: 2` is served up to two concurrent jobs without
    waiting for its turn, even when another project is flooding the pool."""
    _mk_project(registry, "hold")
    _mk_project(registry, "flood")
    pool.start(2)
    pool.register("hold", reserve_workers=2)
    pool.register("flood")
    # flood first, so the rotation head is the OTHER project
    _submit(pool, registry, tmp_path / "runs", "flood", 4)
    _submit(pool, registry, tmp_path / "runs", "hold", 2)
    peak = _sample_peak_running(pool, 8.0)
    assert peak.get("hold", 0) == 2, peak     # reserved slots, no queueing


def test_only_the_job_queue_is_bounded_and_it_is_global(tmp_path, registry, pool):
    """The job backlog is bounded by the TOTAL worker allowance — not per
    project: `pending()` never exceeds `max_count`, and a submission above
    the bound waits instead of queueing forever."""
    _mk_project(registry, "q1")
    cap = pool.max_count()
    get_config().workers["max_count"] = 3           # small, observable bound
    try:
        pool.start(1)
        for i in range(3):                          # exactly the allowance
            _submit(pool, registry, tmp_path / "runs", "q1", 1, tag=f"s{i}_")
        assert _pump_until(pool, lambda: pool.pending() >= 1, 10)
        assert pool.pending() <= 3, pool.pending()
        depth = pool.queue_depth()
        assert depth["cap"] == 3, depth
        # one more than the allowance: the submission WAITS (the total bound
        # is the only backpressure; generations are never queued separately)
        import threading
        returned = []
        t = threading.Thread(target=lambda: (_submit(pool, registry,
                                                     tmp_path / "runs", "q1", 1,
                                                     tag="over_"),
                                             returned.append(True)), daemon=True)
        t.start()
        t.join(timeout=0.6)
        assert not returned, "a submission above the allowance must wait"
    finally:
        get_config().workers["max_count"] = cap
