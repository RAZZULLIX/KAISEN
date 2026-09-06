"""Campaign driver: slot filling, target finalization, crash-resume without
double counting, and exactly-once bug capture — against a stateful fake
server that simulates generations advancing between polls."""
import json

from kaisen.campaign import CampaignDriver


class FakeServer:
    """Implements KaiClient.call() over an in-memory engine pool.

    Simulating reality: every /api/iterations poll advances each RUNNING,
    unpaused engine by one generation (appending a history row)."""

    def __init__(self, pids=("p1", "p2", "p3"), fail_every=7):
        self.pids = list(pids)
        self.fail_every = fail_every  # every Nth gen of a project is verify_fail
        self.engines = {pid: {"engine_state": "stopped", "paused": False}
                        for pid in self.pids}
        self.iterations = {pid: [] for pid in self.pids}
        self.best = {pid: {"metrics": {"time_ms": 100.0 - 10 * i}}
                     for i, pid in enumerate(self.pids)}
        self.calls = []

    def call(self, method, path, body=None, **kw):
        self.calls.append((method, path))
        body = body or {}
        if path == "/api/active":
            return {"engines": [
                {"project_id": pid, **e} for pid, e in self.engines.items()]}
        if path.startswith("/api/iterations"):
            pid = path.split("project_id=")[1]
            # advance the simulation: the QUERIED project's engine scores one
            # generation per poll while running (reads of other projects don't
            # advance it — time passes per observed engine, not per API call)
            e = self.engines[pid]
            if e["engine_state"] == "running" and not e["paused"]:
                n = len(self.iterations[pid]) + 1
                outcome = ("verify_fail" if self.fail_every and n % self.fail_every == 0
                           else "ok")
                detail = (f"FUZZ MISMATCH case=3 tag=rand:1 input=[42] "
                          f"expected='7' got='5'" if outcome == "verify_fail"
                          else "")
                self.iterations[pid].append({
                    "iteration": n, "outcome": outcome,
                    "detail": detail, "metrics": {"time_ms": 90.0},
                    # reality: a failed generation is NOT scored — no fitness
                    "fitness": (None if outcome == "verify_fail" else 1.0)})
            return self.iterations[pid]
        if path == "/api/engine/switch":
            pid = body["project_id"]
            assert pid in self.engines, f"unknown project {pid}"
            self.engines[pid]["engine_state"] = "running"
            return {"ok": True}
        if path == "/api/engine/pause":
            pid = body["project_id"]
            self.engines[pid]["paused"] = bool(body.get("paused"))
            return {"ok": True}
        if path.startswith("/api/projects/") and path.endswith("/best"):
            pid = path.split("/")[3]
            return self.best[pid]
        if path == "/api/projects":
            return {"projects": [{"id": p} for p in self.pids]}
        raise AssertionError(f"unexpected call {method} {path}")


def _driver(srv, tmp_path, target=5, parallel=2):
    state = tmp_path / "campaign.json"
    drv = CampaignDriver(srv, state_path=state, target_gens=target,
                         max_parallel=parallel, poll_s=0.0)
    drv.register(srv.pids)
    return drv


def _run_to_completion(drv, max_ticks=200):
    for _ in range(max_ticks):
        drv.tick()
        if all(p["state"] in ("done", "failed")
               for p in drv.state["projects"].values()):
            break


def test_driver_fills_slots_and_finishes_every_project(tmp_path):
    srv = FakeServer(fail_every=0)  # no failures in this scenario
    drv = _driver(srv, tmp_path, target=5, parallel=2)
    _run_to_completion(drv)

    for pid in srv.pids:
        p = drv.state["projects"][pid]
        assert p["state"] == "done", (pid, p)
        # exactly `target` new history rows per project — no over-running
        assert len(srv.iterations[pid]) == 5, pid
        # engine paused at the end, champion metric recorded
        assert srv.engines[pid]["paused"] is True
        assert p["best_ms"] == srv.best[pid]["metrics"]["time_ms"]


def test_parallelism_is_respected(tmp_path):
    """With parallel=2 and 3 projects, never more than 2 engines run at once;
    the third starts only after one finishes."""
    srv = FakeServer(fail_every=0)
    drv = _driver(srv, tmp_path, target=4, parallel=2)
    peak_running = 0
    for _ in range(200):
        running = sum(1 for e in srv.engines.values()
                      if e["engine_state"] == "running" and not e["paused"])
        peak_running = max(peak_running, running)
        drv.tick()
        if all(p["state"] in ("done", "failed")
               for p in drv.state["projects"].values()):
            break
    assert peak_running <= 2


def test_crash_resume_does_not_double_count(tmp_path):
    """Driver crashes mid-run; a NEW driver instance (same state file) must
    resume from the original start_hist anchor — generations done before the
    crash still count, and no project runs past its target."""
    srv = FakeServer(fail_every=0)
    drv1 = _driver(srv, tmp_path, target=6, parallel=3)
    for _ in range(4):          # let some progress happen
        drv1.tick()
    state_file = tmp_path / "campaign.json"
    assert state_file.exists()

    # simulate the crash: every engine dies
    for e in srv.engines.values():
        e["engine_state"] = "stopped"
        e["paused"] = False

    drv2 = CampaignDriver(srv, state_path=state_file, target_gens=6,
                          max_parallel=3, poll_s=0.0)
    drv2.reconcile()
    for pid, p in drv2.state["projects"].items():
        assert p["state"] == "pending", (pid, p)  # resumable, not lost

    _run_to_completion(drv2)
    for pid in srv.pids:
        p = drv2.state["projects"][pid]
        assert p["state"] == "done", (pid, p)
        # total history per project is EXACTLY the target: progress before
        # the crash counted toward it, not on top of it
        assert len(srv.iterations[pid]) == 6, (pid, len(srv.iterations[pid]))


def test_bug_capture_is_exactly_once(tmp_path):
    srv = FakeServer(fail_every=3)   # deterministic verify_fail rows
    drv = _driver(srv, tmp_path, target=12, parallel=3)
    bugs_file = type(drv).BUGS_FILE if hasattr(type(drv), "BUGS_FILE") else None
    from kaisen import campaign as C
    real_bugs = C.BUGS_FILE
    C.BUGS_FILE = tmp_path / "campaign_bugs.jsonl"
    try:
        _run_to_completion(drv)

        # a second driver over the same state must not re-capture anything
        drv2 = CampaignDriver(srv, state_path=tmp_path / "campaign.json",
                              target_gens=12, max_parallel=3, poll_s=0.0)
        for _ in range(5):
            drv2.tick()

        rows = [json.loads(l) for l in
                (tmp_path / "campaign_bugs.jsonl").read_text().splitlines()]
        assert rows, "expected captured failures"
        # exactly-once: row count == failure rows that exist server-side
        n_fail = sum(1 for pid in srv.pids for it in srv.iterations[pid]
                     if it["outcome"] == "verify_fail")
        assert len(rows) == n_fail, (len(rows), n_fail)
        # the FUZZ diagnostic survived into the triage row
        assert any("FUZZ MISMATCH" in r["detail"] for r in rows)
        # no duplicate (project, iteration) pairs
        keys = [(r["project"], r["iteration"]) for r in rows]
        assert len(keys) == len(set(keys))
    finally:
        C.BUGS_FILE = real_bugs


def test_stop_mode_pauses_running_engines(tmp_path):
    srv = FakeServer(fail_every=0)
    drv = _driver(srv, tmp_path, target=50, parallel=3)
    for _ in range(3):
        drv.tick()
    running = [pid for pid, e in srv.engines.items()
               if e["engine_state"] == "running" and not e["paused"]]
    assert running
    for pid in running:
        drv.client.call("POST", "/api/engine/pause",
                        {"paused": True, "project_id": pid})
    drv.save()
    for pid in running:
        assert srv.engines[pid]["paused"] is True


def test_failed_generations_do_not_burn_budget(tmp_path):
    """Regression (live incident, 2026-09-06): progress counted every
    iteration, so a project failing all its generations burned its full
    budget and was marked done with zero champions — four collatz projects
    hit this in one campaign. Progress must count SCORED generations only:
    an all-failing project keeps running until it actually scores."""
    from kaisen import campaign as C
    real_bugs = C.BUGS_FILE
    C.BUGS_FILE = tmp_path / "campaign_bugs.jsonl"
    try:
        srv = FakeServer(pids=("p1",), fail_every=1)   # every generation fails
        drv = _driver(srv, tmp_path, target=5, parallel=1)
        for _ in range(30):
            drv.tick()
        p = drv.state["projects"]["p1"]
        assert p["state"] == "running", (p,)  # still trying — not falsely done
        assert p["scored"] == 0
        assert len(srv.iterations["p1"]) >= 5  # failures don't consume budget
    finally:
        C.BUGS_FILE = real_bugs
