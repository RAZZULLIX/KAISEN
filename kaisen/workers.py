# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Subprocess worker pool.

Each worker is an isolated OS process: it pulls jobs, runs the project
pipeline (build/verify/score), and reports results + progress back to the
main process.  A crashing candidate can never take down the framework.

The pool supports live add / remove / kill via the GUI.

Messages (pickle-safe dicts on multiprocessing queues):
  job      {job_id, generation, candidate, workdir}        -> worker
  result   {job_id, worker_id, pipeline_result, ok}        -> main
  progress {worker_id, stage, extra}                       -> main
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .projects import Project, ProjectRegistry
from .config import get_config

WORKER_START_TIMEOUT = 15.0


def _setup_build_cache(project: Project) -> None:
    """Opt-in ccache integration for the build step.  When the project
    spec sets engine.build_cache: true and ccache is on PATH, the worker
    routes compiler invocations through a per-project masquerade dir so
    unchanged translation units are reused across generations (keyed on
    preprocessed source + flags).  Graceful fallback: no ccache -> plain
    builds with a one-time warning.  Off by default."""
    spec = project.spec
    enabled = bool((spec.get("engine") or {}).get("build_cache"))
    if not enabled:
        return
    import shutil
    ccache = shutil.which("ccache")
    if not ccache:
        print(f"[KAISEN] worker: engine.build_cache is on but ccache is not installed — builds run uncached")
        return
    cache_dir = project.path / ".kaisen_cache"
    masq = cache_dir / "bin"
    try:
        masq.mkdir(parents=True, exist_ok=True)
        for name in ("gcc", "g++", "cc", "clang", "clang++"):
            link = masq / name
            if not link.exists():
                os.symlink(ccache, link)
    except OSError:
        return
    os.environ["CCACHE_DIR"] = str(cache_dir)
    os.environ["CCACHE_MAXSIZE"] = str(
        (spec.get("engine") or {}).get("build_cache_max_size") or "10G")
    # BASEDIR makes cache keys path-independent so objects reuse across
    # machines/checkouts; the compiler still sees the real paths.
    os.environ["CCACHE_BASEDIR"] = str(project.path)
    path = os.environ.get("PATH", "")
    if str(masq) not in path:
        os.environ["PATH"] = str(masq) + os.pathsep + path


def _worker_main(
    worker_id: int,
    project_id: str,
    registry_root: str,
    jobs_q: "multiprocessing.Queue",
    results_q: "multiprocessing.Queue",
    progress_q: "multiprocessing.Queue",
) -> None:
    """Worker process entry point."""
    from pathlib import Path
    from .pipeline import run_pipeline
    from .projects import ProjectRegistry

    # The registry root is passed through: the worker must resolve the
    # project from the SAME root the engine was configured with (custom
    # roots, temp roots, tests) — never from the framework default.
    registry = ProjectRegistry(Path(registry_root))

    # Quiet mode / affinity: pin this worker to specific cores and/or lower
    # its priority so a busy score job can't drown the dashboard or LLMs.
    # The affinity/quiet flags come from config.json (workers.affinity /
    # workers.quiet) and apply to the whole worker process.
    try:
        _cfg = get_config()
        affinity = str(_cfg.workers.get("affinity", "") or "")
        quiet = bool(_cfg.workers.get("quiet", False))
    except Exception:
        affinity, quiet = "", False
    if affinity:
        try:
            cpus = set()
            for token in affinity.split(","):
                token = token.strip()
                if not token:
                    continue
                if "-" in token:
                    lo, _, hi = token.partition("-")
                    cpus.update(range(int(lo), int(hi) + 1))
                else:
                    cpus.add(int(token))
            if cpus:
                os.sched_setaffinity(0, cpus)
        except Exception:
            pass
    if quiet:
        try:
            os.nice(10)
        except Exception:
            pass

    def emit(stage: str, extra: Dict[str, Any]):
        try:
            progress_q.put({"worker_id": worker_id, "stage": stage, "extra": extra or {}})
        except Exception:
            pass

    emit("idle", {})
    while True:
        try:
            job = jobs_q.get(timeout=1.0)
        except Exception:
            continue
        if job is None:
            break
        emit("starting", {"generation": job.get("generation"), "gen_dir": job.get("workdir")})
        try:
            # Fresh Project per job: spec changes (timeouts, engine knobs,
            # build_cache) apply at the NEXT generation without a restart.
            project = registry.require(project_id)
            _setup_build_cache(project)
            result = run_pipeline(
                project,
                job["candidate"],
                job["workdir"],
                progress=lambda stage, extra: emit(stage, {**extra, "generation": job.get("generation"), "gen_dir": job.get("workdir")}),
                context={"job": job, **(job.get("context") or {})},
            )
            results_q.put({"worker_id": worker_id, "job_id": job.get("job_id"), "ok": True, "result": result})
        except Exception as e:
            results_q.put({
                "worker_id": worker_id, "job_id": job.get("job_id"), "ok": False,
                "result": {"ok": False, "stage": "worker", "outcome": "worker_error",
                           "reason": f"worker exception: {e}", "metrics": {}},
            })
        emit("idle", {})


class WorkerPool:
    def __init__(
        self,
        registry: ProjectRegistry,
        project_id: str,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.registry = registry
        self.project_id = project_id
        self.progress_cb = progress_cb
        self.jobs_q: "multiprocessing.Queue" = multiprocessing.Queue()
        self.results_q: "multiprocessing.Queue" = multiprocessing.Queue()
        self.progress_q: "multiprocessing.Queue" = multiprocessing.Queue()
        self._procs: Dict[int, "multiprocessing.Process"] = {}
        self._next_id = 0
        self._results_reader: Optional[Any] = None
        self._progress_reader: Optional[Any] = None
        self._result_cb: Optional[Callable[[Dict[str, Any]], None]] = None
        self._stop = multiprocessing.Event()
        self._workers_state: Dict[int, Dict[str, Any]] = {}
        self._target = 0  # intended worker count; crashed workers respawn to it
    # -- lifecycle --------------------------------------------------------

    def start(self, count: int) -> None:
        self._target = int(count)
        for _ in range(int(count)):
            self.add_worker()
    def add_worker(self) -> Dict[str, Any]:
        wid = self._next_id
        self._next_id += 1
        self._target += 1
        p = multiprocessing.Process(
            target=_worker_main,
            args=(wid, self.project_id, str(self.registry.root),
                  self.jobs_q, self.results_q, self.progress_q),
            daemon=True,
            name=f"kaisen-worker-{wid}",
        )
        p.start()
        self._procs[wid] = p
        self._workers_state[wid] = {
            "worker_id": wid, "pid": p.pid, "status": "starting", "stage": "idle",
            "generation": None, "started_at": time.time(),
        }
        return self._workers_state[wid]
    def remove_worker(self, worker_id: int, kill: bool = False) -> bool:
        p = self._procs.pop(worker_id, None)
        if p is None or not p.is_alive():
            self._workers_state.pop(worker_id, None)
            return False
        if kill:
            p.kill()
        else:
            # Graceful: send a sentinel job? Simpler: terminate.
            p.terminate()
        p.join(timeout=3)
        if p.is_alive():
            p.kill()
        self._workers_state.pop(worker_id, None)
        self._target = max(0, self._target - 1)
        return True
    def kill_worker(self, worker_id: int) -> bool:
        return self.remove_worker(worker_id, kill=True)

    def stop_all(self) -> None:
        self._target = 0
        for wid in list(self._procs.keys()):
            self.remove_worker(wid, kill=True)

    def worker_count(self) -> int:
        alive = 0
        for wid, p in list(self._procs.items()):
            if not p.is_alive():
                self._procs.pop(wid, None)
                self._workers_state.pop(wid, None)
            else:
                alive += 1
        # Self-heal: crashed workers respawn up to the intended target so
        # the pool silently recovers instead of losing capacity forever.
        while len(self._procs) < self._target:
            self.add_worker()   # bumps the target — put it back: the slot
            self._target -= 1   # was already accounted for.
            alive += 1
        return alive

    def shrink_to(self, n: int) -> int:
        """Remove workers until the pool holds at most `n` processes
        (graceful terminate; a busy worker's in-flight evaluation dies).
        REMOVES only — it never spawns (growth is the caller's job, so the
        self-heal inside worker_count() cannot fight a shrink). Returns the
        resulting live count."""
        n = max(0, int(n))
        # Remove the excess FIRST, using len(_procs) directly (not
        # worker_count(), whose self-heal would respawn up to _target).
        while len(self._procs) > n:
            for wid in list(self._procs.keys()):
                if len(self._procs) <= n:
                    break
                st = self._workers_state.get(wid) or {}
                kill = st.get("status") == "running"
                self.remove_worker(wid, kill=kill)
        # Then lower the target so a later worker_count() cannot respawn
        # the removed workers (remove_worker already decremented it).
        self._target = n
        return len(self._procs)

    def list_workers(self) -> List[Dict[str, Any]]:
        self.worker_count()
        return list(self._workers_state.values())

    # -- job submission ---------------------------------------------------

    def submit(self, job: Dict[str, Any]) -> None:
        job = dict(job)
        job.setdefault("job_id", f"{int(time.time()*1000)}-{os.getpid()}")
        self.jobs_q.put(job)

    def set_result_handler(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        """cb(result_msg) called in the main process for each finished job."""
        self._result_cb = cb

    def pump(self) -> None:
        """Drain queues (call periodically from the main loop)."""
        try:
            while True:
                msg = self.progress_q.get_nowait()
                # Worker state must ALWAYS track progress — the dashboard
                # polls list_workers(); the optional callback is extra.
                self._apply_progress(msg)
                if self.progress_cb:
                    try:
                        self.progress_cb(msg)
                    except Exception:
                        pass
        except Exception:
            pass
        if self._result_cb is None:
            return
        try:
            while True:
                msg = self.results_q.get_nowait()
                try:
                    self._result_cb(msg)
                except Exception:
                    pass
        except Exception:
            pass

    def _apply_progress(self, msg: Dict[str, Any]) -> None:
        wid = msg.get("worker_id")
        if wid not in self._workers_state:
            return
        st = self._workers_state[wid]
        extra = msg.get("extra") or {}
        stage = msg.get("stage") or "idle"
        st["stage"] = stage
        st["status"] = "idle" if stage == "idle" else "running"
        if extra.get("generation") is not None:
            st["generation"] = extra["generation"]
        if extra.get("gen_dir"):
            st["temp_dir"] = extra["gen_dir"]
        if extra.get("live") is not None:
            st["live"] = extra["live"]
        if extra.get("elapsed") is not None:
            st["elapsed"] = extra["elapsed"]
        if extra.get("rss") is not None:
            st["rss"] = extra["rss"]
        if extra.get("pid"):
            st["child_pid"] = extra["pid"]
        elif stage == "idle":
            st["child_pid"] = None
        st["extra"] = extra

    def set_worker_result(self, worker_id: int, result: Dict[str, Any]) -> None:
        """Record a worker's final pipeline result (for the dashboard cards)."""
        st = self._workers_state.get(worker_id)
        if st is None:
            return
        st["child_pid"] = None
        st["result"] = {
            "ok": bool(result.get("ok")),
            "outcome": result.get("outcome"),
            "metrics": result.get("metrics") or {},
            "timings": result.get("timings") or {},
        }

    def kill_worker_process(self, worker_id: int) -> bool:
        """Kill the subprocess a worker is currently running (harness +
        candidate tree), keeping the worker alive.  The worker's pipeline
        step fails and it returns to idle."""
        st = self._workers_state.get(worker_id)
        if st is None:
            return False
        pid = st.get("child_pid")
        if not pid or st.get("status") != "running":
            return False
        from .util import kill_pid_tree
        # The harness runs in its own session/process group — kill the
        # whole tree (harness + candidate program).
        return kill_pid_tree(int(pid))
