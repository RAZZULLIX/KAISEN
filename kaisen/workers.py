# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Shared subprocess worker pool.

ONE pool for the whole process, like the LLM orchestrator: every project
engine submits jobs to the same scheduler, and a fixed set of worker
processes (`workers.default_count` / `workers.max_count`) drains it.  Jobs
are handed out ONE PER FREE WORKER in ROTATION between the projects that
have jobs waiting — the same fairness the LLM pool gives generations — so
no project can hog the machine and none is starved.  The number of jobs
outstanding at once (queued + running) is bounded by the TOTAL worker
allowance (`workers.max_count`), never per project; a project may opt into
its own `max_workers` ceiling (spend guard) or `reserve_workers`
guaranteed slots.  A hundred projects at once means a hundred job
SUBMISSIONS, not a hundred worker processes — the old per-engine pools
scaled workers with the project count and could exhaust the machine.

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

import collections
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .projects import Project, ProjectRegistry
from .config import get_config

WORKER_START_TIMEOUT = 15.0
# A removed worker gets this long to finish its in-flight job before the
# pool falls back to terminate+requeue (the job is never lost either way).
WORKER_RETIRE_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# a forked child must not inherit the parent's live sockets
# ---------------------------------------------------------------------------

def _close_inherited_sockets_in_child() -> None:
    """Drop every inherited *socket* in a freshly forked child.

    A worker only ever talks to the parent over pipes, so every socket it
    inherits is the parent's — and keeping one alive is not a local cost.
    An LLM stream the parent abandons (retry, nodata timeout, pause/cancel)
    only stops the server when the LAST reference to the connection is gone:
    with a forked worker holding an inherited copy, llama.cpp kept decoding
    the abandoned request into a socket nobody read.  The box stayed busy on
    that zombie (its own tps counter went on counting) and the NEXT
    generation sent to it queued behind it, so the live view showed a
    session stuck at zero tokens that never left "prefill" while the box
    looked busy — the exact shape of a stalled engine.

    Pipes are deliberately left alone: multiprocessing queues, events and
    the child sentinel are how the child talks to the pool.
    """
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:                      # no procfs to sweep
        return
    for name in names:
        try:
            fd = int(name)
            if fd > 2 and stat.S_ISSOCK(os.fstat(fd).st_mode):
                os.close(fd)
        except (OSError, ValueError):
            continue


if hasattr(os, "register_at_fork"):
    # Runs in every forked child, and in the forkserver on its own fork.
    os.register_at_fork(after_in_child=_close_inherited_sockets_in_child)


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
    retire_evt: "multiprocessing.Event" = None,
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
        # A removed worker retires AFTER its current job (never mid-job):
        # the pool signals this event, the worker drains the job it holds,
        # reports the result, and only then exits.  No job is ever lost to
        # a resize.
        if retire_evt is not None and retire_evt.is_set():
            break
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
                              "gen_dir": job.get("workdir"),
                              "job_id": job.get("job_id")}, pid, project.name)
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
        # Job payloads kept from submit() until the result is delivered.
        # A killed/crashed worker's in-flight job is re-queued from here so
        # a resize can never destroy a queued job.
        self._job_payloads: Dict[str, Dict[str, Any]] = {}
        # Retire events per worker live OUTSIDE _workers_state: state dicts
        # are serialized straight to JSON (add_worker returns one), and a
        # multiprocessing.Event would explode every endpoint that touches
        # them.
        self._retire_evts: Dict[int, "multiprocessing.Event"] = {}
        self._target = 0  # intended worker count; crashed workers respawn to it
        # ── fair job scheduler (mirrors the LLM pool's generation model) ──
        # `_q` holds each project's waiting jobs (FIFO inside a project),
        # `_order` lists the projects with jobs waiting in SERVICE ORDER:
        # the head is served next and every hand-out sends it to the back, so
        # jobs rotate between projects.  `_running` counts the jobs a project
        # has in flight, `_handed` maps a worker to the project it was given,
        # and `_limits` carries the OPT-IN per-project knobs.
        self._q: Dict[str, collections.deque] = {}
        self._order: List[str] = []
        self._running: Dict[str, int] = {}
        # Cumulative jobs served per project — the fairness made visible:
        # with projects competing, these counts stay within one of each
        # other (rotation), instead of one project racing ahead.
        self._served: Dict[str, int] = {}
        self._handed: Dict[int, str] = {}
        self._limits: Dict[str, Dict[str, Any]] = {}
        self._sched = threading.Condition()
        self._dispatcher: Optional[threading.Thread] = None
        # The pool is shared by every engine + the GUI: guard mutations
        # (spawn/remove/register) so concurrent polls can't race a resize.
        # RLock: start() -> _top_up() -> add_worker() nests the guard.
        self._lock = threading.RLock()

    # -- per-project handler routing --------------------------------------

    def register(self, project_id: str,
                 result_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
                 progress_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
                 max_workers: Optional[int] = None,
                 reserve_workers: Optional[int] = None) -> None:
        """Route this project's results/progress to its engine's handlers
        and record its OPTIONAL worker knobs.  The pool is shared: dispatch
        is keyed by project_id."""
        with self._lock:
            h = self._handlers.setdefault(str(project_id), {})
            if result_handler is not None:
                h["result"] = result_handler
            if progress_handler is not None:
                h["progress"] = progress_handler
        self.set_limits(project_id, max_workers=max_workers,
                        reserve_workers=reserve_workers)

    # -- per-project worker limits (optional) ------------------------------
    def set_limits(self, project_id: str, max_workers: Any = None,
                   reserve_workers: Any = None) -> Dict[str, Any]:
        """OPTIONAL per-project worker knobs, the worker-side twins of
        `max_parallel` / `reserve`:

          max_workers     = the most jobs this project may have RUNNING at
                            once (its jobs still queue freely) — a spend
                            guard on the machine, never a generation
                            throttle;
          reserve_workers = jobs GUARANTEED to this project: it is served up
                            to this many concurrent jobs without waiting for
                            its turn in the rotation.

        `None` leaves a knob untouched; 0 clears it.  Returns the effective
        values."""
        def _norm(v: Any) -> Optional[int]:
            if v in (None, "", "null"):
                return None
            try:
                n = int(v)
            except (TypeError, ValueError):
                return None
            return n if n > 0 else None
        pid = str(project_id)
        with self._sched:
            lim = self._limits.setdefault(pid, {"max_workers": None,
                                                "reserve_workers": None})
            if max_workers is not None:
                lim["max_workers"] = _norm(max_workers)
            if reserve_workers is not None:
                lim["reserve_workers"] = _norm(reserve_workers)
            self._sched.notify_all()
            return dict(lim)

    def limits(self, project_id: str) -> Dict[str, Any]:
        with self._sched:
            return dict(self._limits.get(str(project_id))
                        or {"max_workers": None, "reserve_workers": None})

    def _max_workers(self, project_id: str) -> Optional[int]:
        return (self._limits.get(str(project_id)) or {}).get("max_workers")

    def _reserve_workers(self, project_id: str) -> Optional[int]:
        return (self._limits.get(str(project_id)) or {}).get("reserve_workers")

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
        retire_evt = multiprocessing.Event()
        p = multiprocessing.Process(
            target=_worker_main,
            args=(wid, self.jobs_q, self.results_q, self.progress_q, retire_evt),
            daemon=True,
            name=f"kaisen-worker-{wid}",
        )
        p.start()
        self._procs[wid] = p
        self._retire_evts[wid] = retire_evt
        self._workers_state[wid] = {
            "worker_id": wid, "pid": p.pid, "status": "starting", "stage": "idle",
            "generation": None, "job_id": None, "started_at": time.time(),
        }
        return self._workers_state[wid]

    def add_worker(self) -> Dict[str, Any]:
        with self._lock:
            self._target += 1
            return self._spawn_worker()

    def _requeue_held_job(self, worker_id: int) -> None:
        """Re-queue the job a dying worker still holds.  The payload is
        re-submitted with the SAME job_id so the owning engine resolves the
        same in-flight generation; an already-delivered result makes this a
        no-op (idempotent)."""
        st = self._workers_state.get(worker_id)
        if not st:
            return
        job_id = st.get("job_id")
        if not job_id:
            return
        payload = self._job_payloads.pop(job_id, None)
        if payload is None:
            return
        st["job_id"] = None
        project = str(payload.get("project_id") or "")
        with self._sched:
            # The hand-out ENDS here: the job is queued again, so it must
            # not keep counting as running for this worker/project.
            self._finish_handout(worker_id, project)
            self._q.setdefault(project, collections.deque()).appendleft(payload)
            if project not in self._order:
                self._order.insert(0, project)
            self._ensure_dispatcher()
            self._dispatch()
            self._sched.notify_all()

    def remove_worker(self, worker_id: int, kill: bool = False, requeue: bool = True) -> bool:
        """Remove one worker.  Non-kill removal is GRACEFUL: the worker is
        signalled to retire, finishes its in-flight job (result delivered
        normally), and exits — the queue is untouched.  If it does not
        retire within WORKER_RETIRE_TIMEOUT (or `kill` was requested), it is
        terminated and its held job is re-queued, so a resize can never
        destroy a queued job."""
        # Refresh the worker state first: the "starting" progress message
        # (which claims the job_id) may still be sitting in the progress
        # queue, and a kill must know WHICH job the worker holds.
        self.pump()
        with self._lock:
            p = self._procs.get(worker_id)
            if p is None:
                self._workers_state.pop(worker_id, None)
                return False
            if not p.is_alive():
                # Crash: recover its held job, then drop it.
                if requeue:
                    self._requeue_held_job(worker_id)
                self._procs.pop(worker_id, None)
                self._workers_state.pop(worker_id, None)
                self._retire_evts.pop(worker_id, None)
                self._target = max(0, self._target - 1)
                return False
            st = self._workers_state.get(worker_id) or {}
            retire_evt = self._retire_evts.get(worker_id)
            # Drop from _procs NOW so polls/self-heal see the shrink while
            # we wait for retirement (outside the lock — the dashboard must
            # not freeze for the grace period).
            del self._procs[worker_id]
            self._target = max(0, self._target - 1)
        if kill:
            if requeue:
                with self._lock:
                    self._requeue_held_job(worker_id)
            p.kill()
        else:
            if retire_evt is not None:
                retire_evt.set()
            p.join(timeout=WORKER_RETIRE_TIMEOUT)
            if p.is_alive():
                if requeue:
                    with self._lock:
                        self._requeue_held_job(worker_id)
                p.terminate()
                p.join(timeout=3)
        if p.is_alive():
            p.kill()
        p.join(timeout=3)
        with self._lock:
            self._workers_state.pop(worker_id, None)
            self._retire_evts.pop(worker_id, None)
        return True

    def kill_worker(self, worker_id: int) -> bool:
        return self.remove_worker(worker_id, kill=True)

    def _clear_scheduler(self) -> None:
        with self._sched:
            self._q.clear()
            self._order.clear()
            self._running.clear()
            self._served.clear()
            self._handed.clear()
            self._sched.notify_all()

    def stop_all(self) -> None:
        # Shutdown intent: no re-queueing (the process is going away and
        # the jobs die with it — re-queueing would just delay the exit).
        self._clear_scheduler()
        with self._lock:
            self._target = 0
            for wid in list(self._procs.keys()):
                self.remove_worker(wid, kill=True, requeue=False)

    def worker_count(self) -> int:
        with self._lock:
            for wid, p in list(self._procs.items()):
                if not p.is_alive():
                    # Crashed worker: its in-flight job must survive.
                    self._requeue_held_job(wid)
                    self._procs.pop(wid, None)
                    with self._sched:
                        self._handed.pop(wid, None)
                    self._workers_state.pop(wid, None)
                    self._retire_evts.pop(wid, None)
            # Self-heal: crashed workers respawn up to the intended target so
            # the pool silently recovers instead of losing capacity forever.
            self._top_up()
            return len(self._procs)

    def shrink_to(self, n: int) -> int:
        """Remove workers until the pool holds at most `n` processes.
        A busy worker's in-flight job is re-queued (never lost) — the
        evaluation restarts on a surviving worker or waits in the queue.
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
            return [{k: v for k, v in st.items() if k != "_retire_evt"}
                    for st in self._workers_state.values()]

    # -- job submission ---------------------------------------------------

    def submit(self, job: Dict[str, Any]) -> None:
        """Queue one job for this project.  The ONLY queue in the system:
        worker jobs.  Jobs are handed to free workers in rotation between
        projects (see _dispatch); the caller waits only when the project's
        own `max_workers` ceiling or the GLOBAL outstanding bound (the total
        worker allowance) is reached."""
        job = dict(job)
        job.setdefault("job_id", f"{int(time.time()*1000)}-{os.getpid()}")
        project = str(job.get("project_id") or "")
        with self._sched:
            self._job_payloads[str(job["job_id"])] = job
            # The ONLY wait: the GLOBAL bound.  Outstanding jobs (queued +
            # running) never exceed the TOTAL worker allowance, so the
            # backlog is bounded without ever being per-project.  The
            # per-project `max_workers` ceiling is applied at DISPATCH time
            # (how many of this project's jobs may RUN at once) — so a
            # generation is never held back by its project's own cap.
            while self._outstanding() >= self._cap():
                self._sched.wait(0.2)
            self._q.setdefault(project, collections.deque()).append(job)
            if project not in self._order:
                self._order.append(project)
            self._ensure_dispatcher()
            self._dispatch()
            self._sched.notify_all()

    def pending(self) -> int:
        """Jobs outstanding right now (queued + handed to a worker, result
        not delivered yet) — the depth shown in the UI."""
        with self._sched:
            return self._outstanding()

    def queue_depth(self) -> Dict[str, Any]:
        """Per-project scheduler snapshot (queued / running / limits)."""
        with self._sched:
            return {
                "queued": {p: len(q) for p, q in self._q.items() if q},
                "running": {p: n for p, n in self._running.items() if n},
                "served": dict(self._served),
                "order": list(self._order),
                "limits": {p: dict(l) for p, l in self._limits.items()},
                "total": self._outstanding(),
                "cap": self._cap(),
            }

    # -- fair dispatch -----------------------------------------------------
    def _cap(self) -> int:
        """Total allowance: the global worker cap (never per project)."""
        return max(1, self.max_count())

    def _outstanding(self) -> int:
        """Queued + handed-but-unfinished jobs across EVERY project."""
        return sum(len(q) for q in self._q.values()) + len(self._handed)

    def _for_project(self, project_id: str) -> int:
        """This project's outstanding jobs (queued + running)."""
        return (len(self._q.get(project_id) or ())
                + self._running.get(project_id, 0))

    def _free_workers(self) -> List[int]:
        """Workers that can take a job right now: idle and not already
        handed one.  A worker's status comes from its progress beats.
        Deliberately lock-free here: the dispatcher never waits on `_lock`
        (the housekeeping that respawns crashed workers runs in
        `_dispatch_loop` BEFORE this, outside `_sched`)."""
        return [wid for wid, st in self._workers_state.items()
                if st.get("status") == "idle" and wid not in self._handed
                and self._procs.get(wid) is not None
                and self._procs[wid].is_alive()]

    def _next_project(self) -> Optional[str]:
        """The project to serve next, called with `_sched` held.  Reserved
        slots come first (a project with `reserve_workers` is guaranteed up
        to that many concurrent jobs), then strict rotation: the head of the
        service order, sent to the back after each hand-out so jobs
        round-robin between projects."""
        def _capped(proj: str) -> bool:
            """True when the project already runs its `max_workers` jobs."""
            cap = self._max_workers(proj)
            return cap is not None and self._running.get(proj, 0) >= cap

        for proj in list(self._order):
            reserve = self._reserve_workers(proj)
            if reserve and self._q.get(proj) and not _capped(proj) \
                    and self._running.get(proj, 0) < reserve:
                return proj
        for proj in list(self._order):
            if self._q.get(proj) and not _capped(proj):
                return proj
        return None

    def _dispatch(self) -> None:
        """Hand waiting jobs to free workers, one per project per turn.
        Called with `_sched` held."""
        while True:
            free = self._free_workers()
            if not free:
                break
            proj = self._next_project()
            if proj is None:
                break
            job = self._q[proj].popleft()
            if not self._q[proj]:
                self._q.pop(proj, None)
            if proj in self._order:
                self._order.remove(proj)
            if self._q.get(proj):
                self._order.append(proj)          # back of the rotation
            wid = free[0]
            self._handed[wid] = proj
            self._running[proj] = self._running.get(proj, 0) + 1
            self._served[proj] = self._served.get(proj, 0) + 1
            self.jobs_q.put(job)

    def _ensure_dispatcher(self) -> None:
        """Lazy dispatcher thread: wakes on submit/result/idle beats and
        tops the free workers up, with a slow safety tick."""
        if self._dispatcher is not None and self._dispatcher.is_alive():
            return
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="kaisen-worker-dispatch",
            daemon=True)
        self._dispatcher.start()

    def _dispatch_loop(self) -> None:
        """Top the free workers up from the waiting jobs.  The dispatcher
        only READS worker state and pushes to `jobs_q`: it NEVER spawns
        workers (that stays on the main-thread paths — start / add_worker /
        the periodic worker_count() from the API), because forking a
        process from a background thread can leave the child holding a
        lock the parent's other threads own, and the worker then never
        reports for duty."""
        while not self._stop.is_set():
            try:
                with self._sched:
                    self._dispatch()
                    self._sched.wait(0.5)
            except Exception:
                time.sleep(0.5)

    def _finish_handout(self, worker_id: int, project: str | None = None) -> None:
        """One hand-out ends: the job's result arrived, or its worker died
        and the job went back to the queue.  EXACT accounting matters — the
        per-project cap and the global bound are computed from it, so a
        stale idle beat must never end a hand-out early (that would let the
        dispatcher pile jobs onto a busy project).  Called with `_sched`
        held."""
        proj = self._handed.pop(worker_id, None) or project
        if proj is None:
            return
        left = self._running.get(proj, 1) - 1
        if left > 0:
            self._running[proj] = left
        else:
            self._running.pop(proj, None)

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
                # A delivered result retires the job's payload copy AND the
                # worker's job_id claim: the kill/requeue path must not
                # re-run a job whose result is already on the way.
                job_id = msg.get("job_id")
                if job_id:
                    self._job_payloads.pop(str(job_id), None)
                    st = self._workers_state.get(msg.get("worker_id"))
                    if st is not None and st.get("job_id") == job_id:
                        st["job_id"] = None
                # Scheduler bookkeeping: the handed job is finished — the
                # worker is free again and the project's count drops, which
                # may unblock a submission at its `max_workers` ceiling.
                wid = msg.get("worker_id")
                if wid is not None:
                    with self._sched:
                        self._finish_handout(wid)
                        self._dispatch()
                        self._sched.notify_all()
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
        if stage == "idle":
            # The worker can take the next job: let the scheduler push one.
            with self._sched:
                self._sched.notify_all()
        if msg.get("project_id"):
            st["project_id"] = msg["project_id"]
        if msg.get("project_name"):
            st["project_name"] = msg["project_name"]
        if stage == "idle" and not extra:
            # A plain idle beat (no job context): the worker is truly free.
            st["project_id"] = None
            st["project_name"] = None
            st["job_id"] = None
        if extra.get("generation") is not None:
            st["generation"] = extra["generation"]
        if extra.get("job_id") is not None:
            st["job_id"] = extra["job_id"]
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
