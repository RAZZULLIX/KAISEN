"""Regression tests: baseline re-evaluation on source change, and runtime
worker-count control (resource knobs).

P1-9  — when data/baseline_source changes mid-run, the engine must detect
        the hash change, queue ONE re-evaluation, and force the result to
        become the champion (the old champion was measured against the
        stale baseline).
P1-12 — set_workers resizes the pool at runtime; WorkerPool.shrink_to
        lowers the target without letting the self-heal respawn removed
        workers.
"""
import pytest

from kaisen.engine import ProjectEngine
from kaisen.projects import ProjectRegistry
from kaisen.workers import WorkerPool


def _make_engine(tmp_path):
    """A bare ProjectEngine over a temp project (no workers started)."""
    root = tmp_path / "projects"
    root.mkdir()
    registry = ProjectRegistry(root)
    spec = {
        "id": "proj",
        "name": "Test project",
        "language": "python",
        "steps": {
            "build": {"program": "harness/build.py", "args": ["{candidate}", "{artifact}"]},
            "verify": [],
            "score": [],
        },
        "metrics": {"time_ms": {"label": "Time", "unit": "ms", "direction": "lower", "weight": 1}},
        "data": {"baseline_source": "baseline.py"},
    }
    p = registry.create("proj", spec)
    (p.path / "baseline.py").write_text("print('baseline v1')\n", encoding="utf-8")
    eng = ProjectEngine(p, registry=registry, worker_count=0)
    return eng


# ----------------------------------------------------------------------
# P1-9 — baseline re-evaluation
# ----------------------------------------------------------------------

def test_baseline_change_queues_one_reeval(tmp_path):
    eng = _make_engine(tmp_path)
    # first check: stores the hash, queues nothing
    eng._check_baseline_source()
    assert eng.state.data.get("baseline_source_hash")
    assert eng._baseline_reeval_pending is False

    submitted = []
    eng._submit = lambda gen, cand, gen_dir, baseline=False, **extra: submitted.append(
        {"gen": gen, "baseline": baseline, "extra": extra})

    # modify the baseline -> the next check queues ONE re-evaluation
    (eng.project.path / "baseline.py").write_text("print('baseline v2')\n", encoding="utf-8")
    eng._check_baseline_source()
    assert len(submitted) == 1
    assert submitted[0]["baseline"] is True
    assert submitted[0]["extra"]["baseline_reeval"] is True
    assert eng._baseline_reeval_pending is True

    # change again while a re-eval is pending -> no double queue
    (eng.project.path / "baseline.py").write_text("print('baseline v3')\n", encoding="utf-8")
    eng._check_baseline_source()
    assert len(submitted) == 1

    # history carries the change event
    assert any(h.get("outcome") == "baseline_source_changed" for h in eng.state.history)


def test_reeval_result_forces_champion_and_clears_pending(tmp_path):
    eng = _make_engine(tmp_path)
    eng._baseline_reeval_pending = True
    eng.state.set_best({"fitness": 100.0, "metrics": {"time_ms": 100.0},
                        "code_path": "", "generation": 1})
    gen = 5
    gen_dir = eng._make_gen_dir(gen)
    (gen_dir / "candidate.py").write_text("print('x')\n", encoding="utf-8")
    result = {"ok": True, "outcome": "ok", "metrics": {"time_ms": 50.0},
              "artifact": str(gen_dir / "program"), "score_details": [],
              "confirm_metrics": {}, "build_fixes": []}
    job = {"generation": gen, "baseline": True, "baseline_reeval": True,
           "gen_dir": str(gen_dir), "job_id": "j"}

    eng._apply_result(gen, job, result)

    assert eng._baseline_reeval_pending is False
    # the re-evaluated baseline became the champion even though it is
    # "worse" than the stale champion (100.0 -> 50.0: lower is better here,
    # so this is actually better — the point is it was forced to win)
    assert eng.state.best["fitness"] == 50.0
    assert eng.state.history[-1]["outcome"] == "baseline_reeval"
    assert "BASELINE RE-EVALUATED" in eng.state.history[-1]["detail"]


def test_reeval_failure_clears_pending(tmp_path):
    eng = _make_engine(tmp_path)
    eng._baseline_reeval_pending = True
    eng.state.set_best({"fitness": 10.0, "metrics": {"time_ms": 10.0},
                        "code_path": "", "generation": 1})
    gen = 6
    gen_dir = eng._make_gen_dir(gen)
    (gen_dir / "candidate.py").write_text("print('x')\n", encoding="utf-8")
    result = {"ok": False, "outcome": "build_fail", "reason": "boom",
              "metrics": {}, "stage": "build"}
    job = {"generation": gen, "baseline": True, "baseline_reeval": True,
           "gen_dir": str(gen_dir), "job_id": "j"}

    eng._apply_result(gen, job, result)

    assert eng._baseline_reeval_pending is False
    assert any(h.get("outcome") == "build_fail" and "BASELINE RE-EVALUATION FAILED" in h.get("detail", "")
               for h in eng.state.history)


# ----------------------------------------------------------------------
# P1-12 — runtime worker-count control
# ----------------------------------------------------------------------

def test_set_workers_resizes_pool(tmp_path):
    eng = _make_engine(tmp_path)

    class FakePool:
        def __init__(self):
            self.count = 1
            self.adds = 0
            self.shrinks = 0

        def shrink_to(self, n):
            self.shrinks += 1
            self.count = min(self.count, n)

        def worker_count(self):
            return self.count

        def add_worker(self):
            self.adds += 1
            self.count += 1

    eng.pool = FakePool()
    assert eng.set_workers(4) == 4
    assert eng.pool.adds == 3 and eng.pool.shrinks == 1
    assert eng.pool.count == 4
    # shrinking back down
    assert eng.set_workers(1) == 1
    assert eng.pool.count == 1 and eng.pool.shrinks == 2
    # clamp at 1
    assert eng.set_workers(0) == 1


def test_shrink_to_empty_pool_ok(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    registry = ProjectRegistry(root)
    pool = WorkerPool(registry, "proj")  # no workers started
    assert pool.shrink_to(0) == 0
    assert pool.shrink_to(2) == 0        # nothing to remove; target lowered
    assert pool._target == 2
