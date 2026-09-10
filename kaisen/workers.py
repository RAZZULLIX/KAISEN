# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Shared subprocess worker pool.

ONE pool for the whole process, like the LLM orchestrator: every project
engine submits jobs to the same FIFO queue, and a fixed set of workers
(global cap, `workers.default_count` / `workers.max_count`) drains it in
order.  A hundred projects at once means a hundred job SUBMISSIONS, not a
hundred worker processes — the old per-engine pools scaled workers with
the project count and could exhaust the machine.

Each worker is an isolated OS process: it pulls jobs, runs the project
pipeline (build/verify/score), and reports results + progress back to the
main process.  A crashing candidate can never take down the framework.

Jobs carry `project_id` + `registry_root` so a worker resolves the right
project (main or temp root) PER JOB — workers are not bound to a project.
Per-project handlers (`register`) route results/progress back to the
owning engine; a stopped engine unregisters and its stragglers are
dropped.

The pool supports live add / remove / kill via the GUI.

Messages (pickle-safe dicts on multiprocessing queues):
  job      {job_id, generation, candidate, workdir, project_id,
             registry_root}                                          -> worker
  result   {job_id, worker_id, project_id, pipeline_result, ok}      -> main
  progress {worker_id, project_id, project_name, stage, extra}       -> main
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .projects import Project, ProjectRegistry
from .config import get_config

WORKER_START_TIMEOUT = 15.0


def _setup_build_cache(project: Project) -> None:
    """Opt-in ccache integration for the build step.  When the project
    enables it, the worker sets CCACHE_DIR + a masquerade shim dir so the
    gcc family transparently caches unchanged translation units — builds
    with a one-time warning.  Off by default."""
    spec = project.spec
    enabled = bool((spec.get("engine") or {}).get("build_cache"))
    if not enabled:
        return
    import shutil
    ccache = shutil.which("ccache")
    if not ccache:
        print("[KAISEN] worker: engine.build_cache is on but ccache is not installed — builds run uncached")
        return
    cache_dir = project.path / ".kaisen_cache"
    masq = cache_dir / "bin"
    try:
        masq.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in ("gcc", "g++", "cc", "clang", "clang++"):
        link = masq / name
        if not link.exists():
            try:
                os.symlink(ccache, link)
            except OSError:
                pass
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
    jobs_q: "multiprocessing.Queue",
    results_q: "multiprocessing.Queue",
    progress_q: "multiprocessing.Queue",
) -> None:
    """Worker process entry point: drain the shared FIFO job queue."""
    from .pipeline import run_pipeline

    # One registry per root, cached: scanning the projects tree per job is
    # wasted work, but a project created mid-run must resolve — rebuild
    # the cache on a KeyError (fresh scan, one time per new project).
    registries: Dict[str, ProjectRegistry] = {}

    def registry_for(root: str) -> ProjectRegistry:
        reg = registries.get(root)
        if reg is None:
            reg = registries[root] = ProjectRegistry(Path(root))
        return reg

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

    def emit(stage: str, extra: Dict[str, Any], project_id: str = "",
             project_name: str = ""):
        try:
            progress_q.put({
                "worker_id": worker_id, "stage": stage, "extra": extra or {},
                "project_id": project_id, "project_name": project_name,
            })
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
        pid = str(job.get("project_id") or "")
        root = str(job.get("registry_root") or "")
        try:
            # Fresh Project per job: spec changes (timeouts, engine knobs,
            # build_cache) apply at the NEXT generation without a restart.
            reg = registry_for(root)
            try:
                project = reg.require(pid)
            except KeyError:
                reg.scan()
                project = reg.require(pid)
            emit("starting", {"generation": job.get("generation"),
                              "gen_dir": job.get("workdir")}, pid, project.name)
            _setup_build_cache(project)
            result = run_pipeline(
                project,
                job["candidate"],
                job["workdir"],
                progress=lambda stage, extra: emit(
                    stage, {**extra, "generation": job.get("generation"),
                            "gen_dir": job.get("workdir")}, pid, project.name),
                context={"job": job, **(job.get("context") or {})},
            )
            results_q.put({"worker_id": worker_id, "job_id": job.get("job_id"),
                           "project_id": pid, "ok": True, "result": result})
        except Exception as e:
            results_q.put({
                "worker_id": worker_id, "job_id": job.get("job_id"),
                "project_id": pid, "ok": False,
                "result": {"ok": False, "stage": "worker", "outcome": "worker_error",
                           "reason": f"worker exception: {e}", "metrics": {}},
            })
        emit("idle", {})


class WorkerPool:
    """The process-wide shared worker pool (singleton via get_worker_pool)."""

    def __init__(self, registry_root: Optional[str] = None):
        self.registry_root = registry_root
        self.jobs_q: "multiprocessing.Queue" = multiprocessing.Queue()
        self.results_q: "multiprocessing.Queue" = multiprocessing.Queue()
        self.progress_q: "multiprocessing.Queue" = multiprocessing.Queue()
        self._procs: Dict[int, "multiprocessing.Process"] = {}
        self._next_id = 0
        self._handlers: Dict[str, Dict[str, Callable[[Dict[str, Any]], None]]] = {}
        self._stop = multiprocessing.Event()
        self._workers_state: Dict[int, Dict[str, Any]] = {}
        self._target = 0  # intended worker count; crashed workers respawn to it
        # The pool is shared by every engine + the GUI: guard mutations
        # (spawn/remove/register) so concurrent polls can't race a resize.
        # RLock: start() -> _top_up() -> add_worker() nests the guard.
        self._lock = threading.RLock()

    # -- per-project handler routing --------------------------------------

    def register(self, project_id: str,
                 result_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
                 progress_handler: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        """Route this project's results/progress to its engine's handlers.
        The pool is shared: dispatch is keyed by project_id."""
        with self._lock:
            h = self._handlers.setdefault(str(project_id), {})
            if result_handler is not None:
                h["result"] = result_handler
            if progress_handler is not None:
                h["progress"] = progress_handler

    def unregister(self, project_id: str) -> None:
        """A stopped engine: drop its handlers.  Queued jobs may still run;
        their results find no handler and are dropped."""
        with self._lock:
            self._handlers.pop(str(project_id), None)

    # -- lifecycle --------------------------------------------------------

    def start(self, count: int) -> None:
        """Ensure the global pool has AT LEAST `count` workers.  The pool is
        shared: N projects calling start(4) still yield 4 workers, not 4N."""
        count = max(0, int(count))
        cap = self.max_count()
        with self._lock:
            self._target = max(self._target, min(count, cap))
            self._top_up()

    def max_count(self) -> int:
        try:
            return max(1, int(get_config().workers.get("max_count", 32) or 32))
        except Exception:
            return 32

    def _top_up(self) -> None:
        """Spawn up to `_target` (caller holds _lock)."""
        while len(self._procs) < self._target:
            if "error" in self._spawn_worker():
                break  # global cap reached — never spin forever

    def _spawn_worker(self) -> Dict[str, Any]:
        """Start one worker process WITHOUT touching _target (caller holds
        _lock; add_worker owns the target bump)."""
        cap = self.max_count()
        if len(self._procs) >= cap:
            # The global cap is a promise: no machine meltdown from a
            # hundred projects each wanting their own workers.
            return {"error": f"global worker cap reached ({cap})"}
        wid = self._next_id
        self._next_id += 1
        p = multiprocessing.Process(
            target=_worker_main,
            args=(wid, self.jobs_q, self.results_q, self.progress_q),
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

    def add_worker(self) -> Dict[str, Any]:
        with self._lock:
            self._target += 1
            return self._spawn_worker()

    def remove_worker(self, worker_id: int, kill: bool = False) -> bool:
        with self._lock:
            p = self._procs.pop(worker_id, None)
            if p is None or not p.is_alive():
                self._workers_state.pop(worker_id, None)
                return False
            if kill:
                p.kill()
            else:
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
        with self._lock:
            self._target = 0
            for wid in list(self._procs.keys()):
                self.remove_worker(wid, kill=True)

    def worker_count(self) -> int:
        with self._lock:
            alive = 0
            for wid, p in list(self._procs.items()):
                if not p.is_alive():
                    self._procs.pop(wid, None)
                    self._workers_state.pop(wid, None)
                else:
                    alive += 1
            # Self-heal: crashed workers respawn up to the intended target so
            # the pool silently recovers instead of losing capacity forever.
            self._top_up()
            return len(self._procs)

    def shrink_to(self, n: int) -> int:
        """Remove workers until the pool holds at most `n` processes
        (graceful terminate; a busy worker's in-flight evaluation dies).
        REMOVES only — it never spawns (growth is the caller's job, so the
        self-heal inside worker_count() cannot fight a shrink)."""
        n = max(0, int(n))
        with self._lock:
            while len(self._procs) > n:
                for wid in list(self._procs.keys()):
                    if len(self._procs) <= n:
                        break
                    st = self._workers_state.get(wid) or {}
                    kill = st.get("status") == "running"
                    self.remove_worker(wid, kill=kill)
            self._target = n
            return len(self._procs)

    def list_workers(self) -> List[Dict[str, Any]]:
        self.worker_count()
        with self._lock:
            return list(self._workers_state.values())

    # -- job submission ---------------------------------------------------

    def submit(self, job: Dict[str, Any]) -> None:
        job = dict(job)
        job.setdefault("job_id", f"{int(time.time()*1000)}-{os.getpid()}")
        self.jobs_q.put(job)

    def pending(self) -> int:
        """Approximate queued-job depth (backpressure signal for producers)."""
        try:
            return self.jobs_q.qsize()
        except Exception:
            return 0

    # -- queue draining ----------------------------------------------------

    def pump(self) -> None:
        """Drain queues (call periodically from the main loop)."""
        try:
            while True:
                msg = self.progress_q.get_nowait()
                # Worker state must ALWAYS track progress — the dashboard
                # polls list_workers(); the per-project callback is extra.
                self._apply_progress(msg)
                with self._lock:
                    h = self._handlers.get(str(msg.get("project_id") or ""))
                if h and h.get("progress"):
                    try:
                        h["progress"](msg)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            while True:
                msg = self.results_q.get_nowait()
                with self._lock:
                    h = self._handlers.get(str(msg.get("project_id") or ""))
                if h and h.get("result"):
                    try:
                        h["result"](msg)
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
        if msg.get("project_id"):
            st["project_id"] = msg["project_id"]
        if msg.get("project_name"):
            st["project_name"] = msg["project_name"]
        if stage == "idle" and not extra:
            # A plain idle beat (no job context): the worker is truly free.
            st["project_id"] = None
            st["project_name"] = None
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


_shared_pool: WorkerPool | None = None


def get_worker_pool() -> WorkerPool:
    """Process-wide shared worker pool (like get_orchestrator)."""
    global _shared_pool
    if _shared_pool is None:
        _shared_pool = WorkerPool()
    return _shared_pool


def reset_worker_pool() -> None:
    """Tests only: drop the singleton (and its worker processes)."""
    global _shared_pool
    if _shared_pool is not None:
        _shared_pool.stop_all()
        _shared_pool = None
