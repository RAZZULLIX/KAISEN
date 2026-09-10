"""Shared worker pool: one pool for every project, FIFO job order.

The regression the user cares about: N projects must NOT spawn N×workers —
the pool is process-wide, jobs queue in submission order, and results
route back to the owning engine by project_id."""
import json
import time

import pytest

from kaisen.engine import ProjectEngine
from kaisen.llm import ModelOrchestrator
from kaisen.projects import ProjectRegistry
from kaisen.workers import get_worker_pool, reset_worker_pool


@pytest.fixture(autouse=True)
def _fresh_pool():
    reset_worker_pool()
    yield
    reset_worker_pool()


def _mk_project(registry, pid):
    spec = {
        "id": pid, "name": f"Proj {pid}", "language": "python",
        "steps": {
            "build": {"program": "harness/build.py", "args": ["{candidate}", "{artifact}"]},
            "verify": [], "score": [],
        },
        "metrics": {"ms": {"label": "ms", "unit": "ms", "direction": "lower", "weight": 1}},
        "data": {"baseline_source": "baseline.py"},
    }
    p = registry.create(pid, spec)
    (p.path / "baseline.py").write_text("print(0)\n", encoding="utf-8")
    return p


def _mk_engine(project, registry, cfg, worker_count):
    from kaisen.config import FrameworkConfig
    orch = ModelOrchestrator(cfg if isinstance(cfg, FrameworkConfig) else cfg)
    eng = ProjectEngine(project, orchestrator=orch, registry=registry,
                        worker_count=worker_count)
    return eng


def test_shared_pool_is_one_singleton():
    pool_a = get_worker_pool()
    pool_b = get_worker_pool()
    assert pool_a is pool_b


def test_many_engines_share_one_pool(tmp_path, tmp_cfg):
    """Two engines each requesting 3 workers must yield ONE pool of 3
    workers, not 6 — the '20 projects × N workers melts the machine' trap."""
    root = tmp_path / "projects"
    root.mkdir()
    registry = ProjectRegistry(root)
    a = _mk_project(registry, "proj-a")
    b = _mk_project(registry, "proj-b")
    eng_a = ProjectEngine(a, orchestrator=ModelOrchestrator(tmp_cfg),
                          registry=registry, worker_count=3)
    eng_b = ProjectEngine(b, orchestrator=ModelOrchestrator(tmp_cfg),
                          registry=registry, worker_count=3)
    assert eng_a.pool is eng_b.pool            # the SAME pool object
    pool = eng_a.pool
    pool.start(3)
    assert pool.worker_count() == 3            # not 6: the second engine's
    pool.start(3)                              # request(3) is a no-op
    assert pool.worker_count() == 3            # (target already >= 3)
    # a LARGER request grows the shared pool exactly once
    pool.start(7)
    assert pool.worker_count() == 7
    # the global cap is enforced
    for _ in range(5):
        pool.add_worker()
    assert pool.worker_count() <= pool.max_count()
    reset_worker_pool()


def _pump_until(pool, pred, timeout=5.0):
    """multiprocessing.Queue delivery is async (feeder thread): pump until
    the predicate is satisfied or the timeout elapses."""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        pool.pump()
        if pred():
            return True
        _time.sleep(0.02)
    pool.pump()
    return pred()


def test_jobs_route_to_owning_engine_by_project(tmp_path, tmp_cfg):
    """Two engines registered on the shared pool: results for proj-a must
    reach eng_a's handler, proj-b's reach eng_b's — never crossed."""
    root = tmp_path / "projects"
    root.mkdir()
    registry = ProjectRegistry(root)
    a = _mk_project(registry, "proj-a")
    b = _mk_project(registry, "proj-b")
    pool = get_worker_pool()
    got_a, got_b = [], []
    pool.register("proj-a", result_handler=lambda m: got_a.append(m.get("project_id")))
    pool.register("proj-b", result_handler=lambda m: got_b.append(m.get("project_id")))
    # Simulate two results landing (as workers would emit them)
    pool.results_q.put({"job_id": "1", "worker_id": 1, "project_id": "proj-a", "ok": True, "result": {}})
    pool.results_q.put({"job_id": "2", "worker_id": 2, "project_id": "proj-b", "ok": True, "result": {}})
    assert _pump_until(pool, lambda: len(got_a) == 1 and len(got_b) == 1)
    assert got_a == ["proj-a"]
    assert got_b == ["proj-b"]
    # unregister: stragglers are dropped, never misrouted
    pool.unregister("proj-a")
    pool.results_q.put({"job_id": "3", "worker_id": 1, "project_id": "proj-a", "ok": True, "result": {}})
    pool.pump()
    assert got_a == ["proj-a"]                # nothing new arrived


def test_jobs_carry_project_context(tmp_path, tmp_cfg):
    """_submit must tag every job with project_id + the engine's registry
    root so a shared worker resolves the right project (temp roots stay
    isolated from the real projects tree)."""
    root = tmp_path / "projects"
    root.mkdir()
    registry = ProjectRegistry(root)
    p = _mk_project(registry, "proj-x")
    eng = ProjectEngine(p, orchestrator=ModelOrchestrator(tmp_cfg),
                        registry=registry, worker_count=0)
    submitted = []
    eng.pool.submit = lambda job: submitted.append(job)   # no real workers
    gen_dir = eng._make_gen_dir(1)
    cand = gen_dir / "candidate.py"
    cand.write_text("print(1)\n")
    eng._submit(1, str(cand), gen_dir, baseline=False)
    assert len(submitted) == 1
    job = submitted[0]
    assert job["project_id"] == "proj-x"
    assert job["registry_root"] == str(registry.root)
    assert "generation" in job and job["generation"] == 1


def test_worker_state_records_project_attribution(tmp_path):
    """Progress messages must carry project_id/project_name so the
    dashboard card can name which project a worker is serving."""
    pool = get_worker_pool()
    pool._workers_state[99] = {"worker_id": 99, "status": "idle", "stage": "idle"}
    pool._apply_progress({"worker_id": 99, "stage": "starting",
                          "project_id": "proj-z", "project_name": "Proj z",
                          "extra": {"generation": 4}})
    st = pool._workers_state[99]
    assert st["project_id"] == "proj-z"
    assert st["project_name"] == "Proj z"
    assert st["status"] == "running"
    # idle beat clears attribution
    pool._apply_progress({"worker_id": 99, "stage": "idle", "extra": {}})
    assert pool._workers_state[99]["project_id"] is None
    assert pool._workers_state[99]["project_name"] is None
