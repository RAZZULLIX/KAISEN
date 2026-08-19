# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""ProjectEngine — runs one project's evolution loop in the main process.

Flow per generation:
  producer thread(s): champion code + memory -> prompt -> LLM -> extract
                      code -> safety scan -> dedup -> write runs/gen_N/ ->
                      submit job to WorkerPool
  worker process:     build -> verify* -> score* (guardrailed)
  main process:       parse result -> compute fitness -> select (hysteresis)
                      -> update champion/state -> results store -> notify

Also owns: baseline bootstrap, custom-code queue, live worker control,
paused/stop flags, periodic deepwork + lessons (spec-driven).
"""

from __future__ import annotations

import os
import random
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import skills as skills_mod
from .config import get_config
from .llm import GenerationCancelled, ModelOrchestrator, ServerError
from .memory import ProjectMemory
from .projects import Project, ProjectRegistry
from .scores import evaluate_score_type, metric_goodness
from .languages import ext_from_lang, fence_from_lang
from .state import ProjectState
from .telegram import pin_message, send_message
from .util import file_sha256, load_json, save_json
from .workers import WorkerPool

# Engine states: the system's ACTUAL state (shown in the status pill).
STATE_STOPPED = "stopped"    # hard stop: nothing runs until play
STATE_STOPPING = "stopping"  # stop requested: cancelling in-flight work
STATE_RUNNING = "running"    # generating freely
STATE_PAUSING = "pausing"    # pause requested: drain in-flight, then paused
STATE_PAUSED = "paused"      # drained, stays stopped until play

_SESSION_MAX_TEXT = 120_000   # keep the streaming tail, drop the head
_SESSION_KEEP_DONE = 10       # completed sessions retained in the live view
_SESSION_KEEP_SECS = 600.0    # completed sessions older than this are pruned


class Session:
    """One live LLM generation: prompt + code streaming in, status."""

    def __init__(self, sid: int, kind: str, gen: int, prompt: str):
        self.id = sid
        self.kind = kind
        self.gen = gen
        self.prompt = prompt
        self.text = ""
        self.status = "generating"          # generating | done | error
        self.server_id: Optional[str] = None
        self.error = ""
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.waiting = False
        self.cancel = threading.Event()
        self._tokens = 0
        self.tps = 0.0

    def push(self, token: str, count: int = 1) -> None:
        self.text += token
        if len(self.text) > _SESSION_MAX_TEXT:
            self.text = self.text[-_SESSION_MAX_TEXT:]
        # REAL tps: tokens actually received / wall time since the request
        # started (prompt evaluation included — the request started then).
        self._tokens += int(count)
        self.tps = self._tokens / max(0.001, time.time() - self.started_at)

    def finish(self, server_id: Optional[str] = None, error: str = "") -> None:
        self.server_id = server_id
        self.finished_at = time.time()
        if error:
            self.status = "error"
            self.error = error
        else:
            self.status = "done"

    def snapshot(self, text_tail: int = 40_000, prompt_tail: int = 4000) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "gen": self.gen,
            "status": self.status,
            "server_id": self.server_id,
            "error": self.error[-400:],
            "text": self.text[-text_tail:],
            "prompt": self.prompt[-prompt_tail:],
            "tps": round(self.tps, 1),
            "waiting": self.waiting,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
        }


class SessionHub:
    """Registry of live LLM sessions (the "Live" window source)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: List[Session] = []
        self._next_id = 0

    def begin(self, kind: str, gen: int, prompt: str) -> Session:
        with self._lock:
            s = Session(self._next_id, kind, gen, prompt)
            self._next_id += 1
            self._sessions.append(s)
            return s

    def iter_active(self) -> List[Session]:
        with self._lock:
            return [s for s in self._sessions if s.status == "generating"]

    def active_by_server(self) -> Dict[str, float]:
        """server_id -> fastest active session tps (for the pill's per-LLM
        stats; cheap — no text payload)."""
        with self._lock:
            out: Dict[str, float] = {}
            for s in self._sessions:
                if s.status == "generating" and s.server_id:
                    out[s.server_id] = max(out.get(s.server_id, 0.0), s.tps)
            return out

    def active_counts(self) -> Dict[str, int]:
        """server_id -> number of ACTIVE sessions on that endpoint."""
        with self._lock:
            out: Dict[str, int] = {}
            for s in self._sessions:
                if s.status == "generating" and s.server_id:
                    out[s.server_id] = out.get(s.server_id, 0) + 1
            return out

    def aggregate_tps(self) -> float:
        """Sum of all active session TPS (the pill's aggregate)."""
        with self._lock:
            return sum(s.tps for s in self._sessions if s.status == "generating")

    def cancel_all(self) -> None:
        for s in self.iter_active():
            s.cancel.set()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            now = time.time()
            active = [s for s in self._sessions if s.status == "generating"]
            done = [s for s in self._sessions if s.status != "generating"]
            keep = done[-_SESSION_KEEP_DONE:]
            # prune sessions older than the retention window
            kept = [s for s in keep if s.finished_at is None or (now - s.finished_at) < _SESSION_KEEP_SECS]
            self._sessions = active + kept
            # Number each endpoint's sessions by start order (#1, #2, ...)
            # so concurrent chats on one LLM are distinguishable.
            slots: Dict[str, int] = {}
            out: List[Dict[str, Any]] = []
            for s in active + kept:
                d = s.snapshot()
                if s.server_id:
                    n = slots.get(s.server_id, 0) + 1
                    slots[s.server_id] = n
                    d["slot"] = n
                out.append(d)
            return out


class EngineEvent:
    """Callbacks for the GUI server."""
    def __init__(self) -> None:
        self.on_state: Optional[Callable[[str], None]] = None   # project_id -> state changed
        self.on_worker: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None


class ProjectEngine:
    def __init__(
        self,
        project: Project,
        orchestrator: Optional[ModelOrchestrator] = None,
        registry: Optional[ProjectRegistry] = None,
        worker_count: int = 4,
        events: Optional[EngineEvent] = None,
    ):
        from .llm import get_orchestrator
        from .projects import ProjectRegistry as _Reg
        self.project = project
        self._code_ext = ext_from_lang(self.project.spec.get("language", "c"))
        self._code_lang = str(self.project.spec.get("language", "c"))
        self._code_fence = fence_from_lang(self._code_lang)
        self.orchestrator = orchestrator or get_orchestrator()
        self.registry = registry or _Reg()
        self.events = events or EngineEvent()
        self.state = ProjectState(project)
        self.memory = ProjectMemory(project, self.state)
        self.results = skills_mod.ResultsStore(project.path)
        self.pool = WorkerPool(registry, project.id, progress_cb=self._on_progress)
        self.pool.set_result_handler(self._on_result)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = STATE_STOPPED
        self._active_generations = 0  # producers inside LLM request -> submit
        self._running = False
        self._producer_threads: List[Any] = []
        self._pump_thread: Optional[threading.Thread] = None
        self._in_flight: Dict[int, Dict[str, Any]] = {}
        self._gen_server: Dict[int, str] = {}
        self._repair_server: Dict[int, str] = {}
        # Bounded compile loop: per-generation LLM repair attempt counter.
        # Cap = engine override (KAI AUTOFIX) > config autofix.llm_repair_max.
        self._llm_repair_count: Dict[int, int] = {}
        self._autofix_settings: Dict[str, Optional[int]] = {
            "max_tries": None, "repair_max": None}
        self._last_log: List[Dict[str, Any]] = []
        self._custom_queue: List[Dict[str, Any]] = []
        self._deepwork_last = 0
        self._worker_count = worker_count
        self._multi = 1
        # Opt-in fuzzy prompt basis: when > 0, each generation's prompt is
        # seeded with a RANDOM one of the top N scored iterations instead of
        # the champion (default 0 = champion always — the safe behavior).
        self._fuzzy_top_n = 0
        self.sessions = SessionHub()
        self.prompt_override: Optional[str] = None
        self.prompt_override_active = False

    # ======================================================================
    # lifecycle
    # ======================================================================

    @property
    def engine_state(self) -> str:
        with self._lock:
            return self._state

    @property
    def is_generating(self) -> bool:
        """True while any generation work is in progress (LLM stream,
        producer mid-cycle, or worker evaluation).  Stays true during the
        pause drain so the pill keeps the green 'generating' until done."""
        with self._lock:
            if self._active_generations > 0 or self._in_flight:
                return True
        return bool(self.sessions.iter_active())

    def _set_state(self, st: str) -> None:
        with self._lock:
            prev = self._state
            self._state = st
        if prev != st:
            self._log(f"engine state: {prev} -> {st}")
            self.state.data["llm_paused"] = st in (STATE_PAUSING, STATE_PAUSED)
            self.state.save()
            self._emit_state()

    def start(self, multi: int = 1, paused: bool = False) -> None:
        """Start the engine.  `paused=True` boots the worker pool, pump
        and baseline evaluation but spawns NO producers — nothing
        generates until the user presses play."""
        if self.engine_state == STATE_RUNNING:
            return
        self._stop.clear()
        self._set_state(STATE_PAUSED if paused else STATE_RUNNING)
        self._running = True
        self._refresh_spec()
        self.project.ensure_dirs()
        self.state.data["active"] = True
        self.state.data["started_at"] = self.state.data.get("started_at") or time.time()
        self.state.save()
        self._check_baseline_source()
        self._bootstrap_baseline()
        if not self.pool.worker_count():
            self.pool.start(self._worker_count)
        if self._pump_thread is None or not self._pump_thread.is_alive():
            self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
            self._pump_thread.start()
        self._multi = max(1, int(multi))
        if not paused:
            self._ensure_producers(self._multi)
        self._log("engine started (paused — press play)" if paused else "engine started")

    def _ensure_producers(self, n: int) -> None:
        # Prune dead threads, then top up to n.  Each producer carries its
        # own stop event so `set_multi` can retire individual pipelines.
        alive = [(t, ev) for t, ev in self._producer_threads if t and t.is_alive()]
        for i in range(len(alive), n):
            ev = threading.Event()
            t = threading.Thread(target=self._producer_loop, args=(i, ev), daemon=True, name=f"producer-{i}")
            t.start()
            alive.append((t, ev))
        self._producer_threads = alive

    def set_multi(self, n: int) -> int:
        """Adjust the number of parallel generation pipelines at runtime.
        Returns the effective count."""
        n = max(1, int(n))
        self._ensure_producers(n)
        while len(self._producer_threads) > n:
            _t, ev = self._producer_threads.pop()
            ev.set()
        self._multi = n
        self._log(f"parallel generation pipelines: {n}")
        return n

    def set_fuzzy(self, top_n: int) -> int:
        """Opt-in fuzzy prompt basis (KAI FUZZY): 0 = champion always
        (default); N > 0 = each generation's prompt is seeded with a RANDOM
        one of the top N scored iterations.  Also enables the bounded
        recent-outcomes section in the prompt.  Runtime only — resets on
        engine restart."""
        top_n = max(0, int(top_n))
        self._fuzzy_top_n = top_n
        mode = "ON" if top_n else "OFF (champion only)"
        self._log(f"fuzzy prompt basis: {mode}" + (f", top {top_n}" if top_n else ""))
        return top_n

    def stop(self) -> None:
        """Hard stop: cancel in-flight LLM streams, kill worker evals, stay
        stopped until start()."""
        if self.engine_state == STATE_STOPPED:
            return
        self._set_state(STATE_STOPPING)
        self._stop.set()
        self.sessions.cancel_all()
        self.pool.stop_all()
        with self._lock:
            # Every queued/in-flight evaluation is a real generation —
            # record its cancellation instead of dropping the row.
            for g in list(self._in_flight.keys()):
                self.state.append_history({"generation": g, "outcome": "cancelled", "detail": "engine stopped with evaluation in flight"})
            self._in_flight.clear()
        self.state.save()
        self._running = False
        self.state.data["active"] = False
        self.state.save()
        self._set_state(STATE_STOPPED)
        self._log("engine stopped")


    # -- user controls (request semantics) --------------------------------
    def request_pause(self) -> bool:
        """Graceful pause: finish the active generation(s), then pause."""
        if self.engine_state == STATE_RUNNING:
            self._set_state(STATE_PAUSING)
            self._log("pause requested — draining active generation")
        return True

    def request_resume(self) -> bool:
        """Cancel a pause (or restart after stop) and keep generating."""
        st = self.engine_state
        if st in (STATE_PAUSING, STATE_PAUSED):
            self._set_state(STATE_RUNNING)
            self._ensure_producers(self._multi)
        elif st in (STATE_STOPPED, STATE_STOPPING):
            self.start(multi=self._multi)
        return True

    def request_stop(self) -> bool:
        self.stop()
        return True

    # ======================================================================
    # baseline bootstrap
    # ======================================================================

    def _bootstrap_baseline(self) -> None:
        if self.state.best.get("fitness") is not None:
            return
        baseline = (self.project.spec.get("data") or {}).get("baseline_source")
        if not baseline:
            self._log("no baseline source configured — first evaluation establishes baseline")
            return
        src = self.project.path / baseline
        if not src.exists():
            self._log(f"baseline source missing: {src}")
            return
        gen = self.state.next_generation()
        gen_dir = self._make_gen_dir(gen)
        candidate = gen_dir / f"candidate{self._code_ext}"
        shutil.copy2(src, candidate)
        self._log(f"baseline evaluation (gen {gen}): {src.name}")
        self._submit(gen, str(candidate), gen_dir, baseline=True)

    # ======================================================================
    # producer loop
    # ======================================================================

    def _producer_loop(self, pipeline_id: int, stop_event: Optional[threading.Event] = None) -> None:
        while not self._stop.is_set() and not (stop_event is not None and stop_event.is_set()):

            try:
                if self.engine_state in (STATE_PAUSING, STATE_PAUSED, STATE_STOPPING, STATE_STOPPED):
                    time.sleep(1.0)
                    continue
                self._refresh_spec()
                self._check_baseline_source()
                qsize = int(get_config().workers.get("queue_size", 15))
                # Legacy-style queueing: producers keep preparing programs
                # while the workers test.  Pause only when the WAITING
                # backlog (in-flight beyond what the workers can hold at
                # once) fills the queue.
                waiting = max(0, len(self._in_flight) - self.pool.worker_count())
                if waiting >= max(1, qsize):
                    time.sleep(1.0)
                    continue
                gen = self.state.next_generation()
                gen_dir = self._make_gen_dir(gen)
                champion_path = self._champion_path()
                code = self._champion_code()
                code, champion_path = self._prompt_basis(champion_path, code)
                if self.prompt_override_active and self.prompt_override:
                    prompt = self.prompt_override
                else:
                    prompt = self._build_prompt(gen, code, champion_path)
                (gen_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
                session = self.sessions.begin(kind="code", gen=gen, prompt=prompt)
                with self._lock:
                    self._active_generations += 1
                try:
                    try:
                        raw, sid = self.orchestrator.request_stream(
                            prompt,
                            pipeline_id=pipeline_id,
                            on_token=session.push,
                            cancel_event=session.cancel,
                            session=session,
                            skill="generation",
                        )
                        self._gen_server[gen] = sid
                        session.finish(server_id=sid)
                    except GenerationCancelled:
                        session.finish(error="cancelled by stop")
                        self.state.append_history({"generation": gen, "outcome": "cancelled", "detail": "generation killed by stop"})
                        self.state.save()
                        self._emit_state()
                        self._log(f"gen {gen}: cancelled by stop")
                        continue
                    except ServerError as e:
                        session.finish(error=str(e))
                        # A failed request IS a generation outcome — record
                        # it so the results table never silently loses rows.
                        self.state.append_history({"generation": gen, "outcome": "request_failed", "detail": str(e)[:300]})
                        self.state.save()
                        self._emit_state()
                        self._log(f"LLM request failed (gen {gen}): {e}")
                        time.sleep(3.0)
                        continue
                    (gen_dir / "llm_raw.txt").write_text(raw, encoding="utf-8")
                    extracted = skills_mod.extract_code(raw, self._code_lang)
                    if not extracted:
                        self.state.append_history({"generation": gen, "outcome": "no_code", "detail": "no extractable code"})
                        self.state.save()
                        self._emit_state()
                        time.sleep(2.0)
                        continue
                    extracted = skills_mod.ensure_headers(extracted)
                    bad = skills_mod.find_dangerous(extracted, self._code_lang)
                    if bad:
                        self.state.append_history({"generation": gen, "outcome": "rejected_dangerous", "detail": bad})
                        self.state.save()
                        self._emit_state()
                        continue
                    violation = self._scope_check(extracted)
                    if violation:
                        self.state.append_history({"generation": gen, "outcome": "scope_violation", "detail": violation})
                        self.state.save()
                        self._emit_state()
                        self._log(f"gen {gen}: {violation}")
                        continue
                    candidate = gen_dir / f"candidate{self._code_ext}"
                    candidate.write_text(extracted, encoding="utf-8")
                    if self._dedup_check(extracted, gen, gen_dir):
                        continue
                    self._submit(gen, str(candidate), gen_dir)
                finally:
                    with self._lock:
                        self._active_generations = max(0, self._active_generations - 1)
            except Exception as e:
                self._log(f"producer error: {e}")
                time.sleep(2.0)

    def _submit(self, gen: int, candidate: str, gen_dir: Path, baseline: bool = False,
                **extra: Any) -> None:
        job_id = f"{int(time.time() * 1000)}-{gen}"
        context = {} if baseline else self._abort_context()
        # Compile-loop caps (KAI AUTOFIX > spec engine.autofix > config
        # autofix) reach the pipeline worker through the job context.
        if not baseline:
            context["autofix_max_tries"] = self._autofix_effective()["max_tries"]
        job = {
            "job_id": job_id,
            "generation": gen,
            "candidate": candidate,
            "workdir": str(gen_dir),
            "baseline": baseline,
            "context": context,
        }
        job.update(extra)
        with self._lock:
            self._in_flight[gen] = {"generation": gen, "baseline": baseline,
                                    "gen_dir": str(gen_dir), "job_id": job_id, **extra}
        self.pool.submit(job)

    def _abort_context(self) -> Dict[str, Any]:
        """Score early-abort rules: a candidate whose live metric already
        provably cannot beat the champion is killed mid-benchmark."""
        rules: Dict[str, Dict[str, Any]] = {}
        best = self.state.best or {}
        best_metrics = best.get("metrics") or {}
        if not best_metrics:
            return {"score_abort": {}}
        schema = (self.project.spec or {}).get("metrics", {})
        select = (self.project.spec or {}).get("select") or {}
        try:
            hyst = float(select.get("hysteresis", 1.0) or 1.0)
        except (TypeError, ValueError):
            hyst = 1.0
        hyst = max(1.0, hyst)
        for key, spec_m in schema.items():
            bv = best_metrics.get(key)
            if bv is None:
                continue
            try:
                bv = float(bv)
            except (TypeError, ValueError):
                continue
            direction = (spec_m or {}).get("direction", "lower")
            if bv <= 0:
                continue
            if direction == "lower":
                rules[key] = {"direction": "lower", "limit": bv * hyst}
            elif direction == "higher":
                rules[key] = {"direction": "higher", "limit": bv / hyst}
        return {"score_abort": rules}
    def submit_custom_code(self, code: str, source: str = "user", language: Optional[str] = None) -> Dict[str, Any]:
        lang = language or self._code_lang
        gen = self.state.next_generation()
        gen_dir = self._make_gen_dir(gen)
        code = skills_mod.ensure_headers(code, lang)
        bad = skills_mod.find_dangerous(code, lang)
        if bad:
            raise ValueError(f"rejected dangerous code: {bad}")
        candidate = gen_dir / f"candidate{ext_from_lang(lang)}"
        candidate.write_text(code, encoding="utf-8")
        (gen_dir / "source.txt").write_text(source, encoding="utf-8")
        self._submit(gen, str(candidate), gen_dir)
        return {"generation": gen, "queued": True}

    def _make_gen_dir(self, gen: int) -> Path:
        d = self.project.runs_dir / f"gen_{gen:06d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ======================================================================
    # champion helpers
    # ======================================================================

    def _champion_path(self) -> str:
        best = self.state.best
        if best.get("code_path") and Path(best["code_path"]).exists():
            return best["code_path"]
        default = self.project.best_dir / f"program{self._code_ext}"


        if default.exists():
            return str(default)
        return str(default)

    def _champion_code(self) -> str:
        try:
            return Path(self._champion_path()).read_text(encoding="utf-8")
        except Exception:
            return "// no champion yet"

    def _scope_check(self, code: str) -> Optional[str]:
        """Edit-scope guard: when the spec declares data.edit_scope (a list
        of function names), a candidate that changes functions OUTSIDE that
        set is rejected before it ever reaches the pipeline.  Heuristic
        (regex function extraction), but it catches the common whole-file
        rewrite where the model touches unrelated code.  Returns a violation
        message, or None if the candidate is allowed."""
        scope = (self.project.spec.get("data") or {}).get("edit_scope")
        if not scope:
            return None
        allowed = {str(s) for s in scope} if isinstance(scope, list) else {str(scope)}
        if not allowed or "*" in allowed:
            return None
        champ = self._champion_code()
        if not champ or champ.startswith("// no champion"):
            return None
        champ_names = skills_mod.extract_function_names(champ, self._code_lang)
        cand_names = skills_mod.extract_function_names(code, self._code_lang)
        changed = (champ_names | cand_names) - (champ_names & cand_names)
        if not champ_names and not cand_names:
            return None  # nothing to compare — let the pipeline judge
        outside = changed - allowed
        if outside:
            return (f"edit scope violated: candidate changes function(s) "
                    f"{sorted(outside)} outside the allowed set {sorted(allowed)}")
        return None

    def _dedup_check(self, code: str, gen: int, gen_dir: Path) -> bool:
        # The project language matters: semantic_hash defaults to the C
        # normalizer, which treats `//` as a comment — on python (and other
        # hash-comment languages) floor division would be stripped before
        # hashing, silently discarding distinct candidates as duplicates.
        h = skills_mod.semantic_hash(code, self._code_lang)
        seen_file = self.project.path / "seen_hashes.json"
        seen = set(load_json(seen_file, []) or [])
        if h in seen:
            self.state.append_history({"generation": gen, "outcome": "duplicate_skip", "detail": h[:16]})
            self.state.save()
            self._emit_state()
            return True
        seen.add(h)
        save_json(seen_file, sorted(seen))
        return False

    # ======================================================================
    # spec freshness / baseline drift / fuzzy basis / retention
    # ======================================================================

    def _refresh_spec(self) -> None:
        """Re-read project.json so spec changes (timeouts, metrics, engine
        knobs) apply at the NEXT generation instead of only after a restart.
        Cheap — one small JSON read per generation."""
        self.project.reload()
        self._code_ext = ext_from_lang(self.project.spec.get("language", "c"))
        self._code_lang = str(self.project.spec.get("language", "c"))
        self._code_fence = fence_from_lang(self._code_lang)

    def _check_baseline_source(self) -> None:
        """Detect when data.baseline_source changed since the engine (or the
        previous run) recorded it.  A changed baseline means the stored
        champion was measured against a different file — warn loudly instead
        of silently scoring against the wrong reference."""
        baseline = (self.project.spec.get("data") or {}).get("baseline_source")
        if not baseline:
            return
        src = self.project.path / baseline
        h = file_sha256(src) if src.exists() else None
        stored = self.state.data.get("baseline_source_hash")
        if stored is None:
            self.state.data["baseline_source_hash"] = h
            self.state.save()
            return
        if h != stored:
            self.state.data["baseline_source_hash"] = h
            self.state.append_history({
                "generation": self.state.generation,
                "outcome": "baseline_source_changed",
                "detail": (f"baseline '{baseline}' changed since last run — "
                           "champion comparison point is stale"),
            })
            self.state.save()
            self._emit_state()
            self._log(f"WARNING: baseline source '{baseline}' changed — comparison point is stale")

    def _prompt_basis(self, champion_path: str, code: str) -> tuple:
        """(code, path) to seed this generation's prompt.  Default: the
        champion.  Fuzzy mode (FUZZY <n>): a random one of the top N scored
        iterations, so different pipelines explore around different local
        optima instead of all hammering the same file."""
        if self._fuzzy_top_n <= 0:
            return code, champion_path
        rows = [r for r in self.results.read()
                if str(r.get("fitness", "")).replace(".", "", 1).isdigit()]
        if len(rows) <= 1:
            return code, champion_path
        score_key, score_type = self._active_score_type()
        direction = score_type.get("direction", "higher")
        scored = []
        for r in rows:
            try:
                f = float(r["fitness"])
            except (TypeError, ValueError):
                continue
            g = int(r.get("generation", 0))
            cand = self.project.runs_dir / f"gen_{g:06d}" / f"candidate{self._code_ext}"
            if cand.exists():
                scored.append((f, g, cand))
        if not scored:
            return code, champion_path
        scored.sort(key=lambda t: t[0], reverse=(direction == "higher"))
        top = scored[:max(1, self._fuzzy_top_n)]
        _f, g, cand = random.choice(top)
        try:
            return cand.read_text(encoding="utf-8"), str(cand)
        except Exception:
            return code, champion_path

    def _valid_rate(self, window: int = 20) -> Dict[str, Any]:
        """Rolling telemetry: fraction of recent generations that scored OK,
        plus per-outcome counts — a run gone toxic (90% build_fail/repair
        churn) is visible at a glance instead of buried in history."""
        hist = self.state.history[-window:]
        counts: Dict[str, int] = {}
        for h in hist:
            counts[h.get("outcome", "?")] = counts.get(h.get("outcome", "?"), 0) + 1
        good = counts.get("ok", 0) + counts.get("valid", 0)
        total = len(hist)
        return {"window": total, "valid_rate": round(good / total, 3) if total else None,
                "outcome_counts": counts}

    def _maybe_prune_runs(self) -> None:
        """Opt-in retention: spec engine.retention {keep_last, keep_best} —
        delete old per-generation dirs beyond the window, always keeping the
        champion's generation and anything still in flight."""
        retention = (self.project.spec.get("engine") or {}).get("retention") or {}
        if not retention.get("enabled"):
            return
        keep_last = max(1, int(retention.get("keep_last", 50) or 50))
        keep_best = bool(retention.get("keep_best", True))
        cutoff = self.state.generation - keep_last
        best_gen = int((self.state.best or {}).get("generation", -1) or -1)
        with self._lock:
            inflight = set(self._in_flight.keys())
        for d in self.project.runs_dir.iterdir():
            if not d.is_dir() or not d.name.startswith("gen_"):
                continue
            try:
                g = int(d.name[4:])
            except ValueError:
                continue
            if g >= cutoff or g in inflight:
                continue
            if keep_best and g == best_gen:
                continue
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    def _write_diff_summary(self, gen_dir: Path, candidate: str) -> None:
        """Machine-readable diff summary against the baseline, written next
        to each scored generation — harvesting/attribution without hand-diffing."""
        baseline = (self.project.spec.get("data") or {}).get("baseline_source")
        if not baseline:
            return
        src = self.project.path / baseline
        if not src.exists():
            return
        try:
            import difflib
            a = src.read_text(encoding="utf-8", errors="replace").splitlines()
            b = Path(candidate).read_text(encoding="utf-8", errors="replace").splitlines()
            sm = difflib.SequenceMatcher(None, a, b)
            added = removed = 0
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "insert":
                    added += i2 - i1 or j2 - j1
                elif tag == "delete":
                    removed += i2 - i1 or j2 - j1
                elif tag == "replace":
                    added += j2 - j1
                    removed += i2 - i1
            save_json(gen_dir / "diff.json", {
                "candidate": Path(candidate).name,
                "baseline": str(src.name),
                "baseline_lines": len(a),
                "candidate_lines": len(b),
                "added_lines": added,
                "removed_lines": removed,
                "changed_lines": added + removed,
            })
        except Exception:
            pass

    # ======================================================================
    # prompt assembly
    # ======================================================================

    def _build_prompt(self, gen: int, code: str, champion_path: str) -> str:
        spec = self.project.spec
        schema = spec.get("metrics", {})
        best = self.state.best
        best_metrics = best.get("metrics", {})
        metric_lines = []
        for key, ms in schema.items():
            val = best_metrics.get(key)
            val_s = f"{float(val):.4f}" if val is not None else "N/A"
            metric_lines.append(
                f"  {key}: {val_s} {ms.get('unit', '')} ({ms.get('direction', 'higher')}-better, weight {ms.get('weight', 1.0)})"
            )
        metric_table = "\n".join(metric_lines) if metric_lines else "  (no metrics yet — first evaluation establishes baseline)"

        memo = self._last_memo()
        memory_section = ""
        lesson = self.memory.load_lesson()
        if lesson:
            memory_section += f"=== LESSON FROM LAST ITERATION ===\n{lesson[:2000]}\n\n"

        if memo:
            memory_section += f"=== DEEPWORK MEMO ===\n{memo[:2000]}\n\n"
        kc = self.memory.keyword_counts(limit=10)
        if kc:
            memory_section += "=== KEYWORD FREQUENCIES (lessons/memos) ===\n" + ", ".join(f"{k}:{v}" for k, v in kc.items()) + "\n\n"
        memory_section += "=== RECENT HISTORY ===\n" + self.memory.build_history_blob()
        # Fuzzy mode doubles as the mule's own memory: bounded recent scored
        # outcomes (config -> size, delta vs champion) so the model can see
        # its own measured results instead of guessing.
        if self._fuzzy_top_n > 0:
            recent_rows = [r for r in self.results.read()[-10:]
                           if str(r.get("fitness", "")).replace(".", "", 1).isdigit()]
            if recent_rows:
                lines = ["=== RECENT SCORED OUTCOMES (last 10) ==="]
                for r in recent_rows:
                    try:
                        f = float(r["fitness"])
                    except (TypeError, ValueError):
                        continue
                    best_f = best.get("fitness")
                    delta = ""
                    if best_f:
                        try:
                            base = float(best_f)
                            if base:
                                pct = (f - base) / base * 100.0
                                delta = f" (delta {pct:+.1f}% vs champion)"
                        except (TypeError, ValueError):
                            pass
                    metrics_str = " ".join(f"{k}={v}" for k, v in r.items()
                                            if k not in ("generation", "outcome", "fitness") and v != "")
                    lines.append(f"  gen {r.get('generation')}: {r.get('outcome')} {metrics_str}{delta}".rstrip())
                memory_section += "\n".join(lines) + "\n\n"

        contract = str((spec.get("data") or {}).get("contract_text") or spec.get("description", ""))
        scope = (spec.get("data") or {}).get("edit_scope")

        variables = {
            "goal": spec.get("prompts", {}).get("goal", spec.get("description", "Improve the program.")),
            "best_metrics": "Champion metrics: " + (", ".join(f"{k}={v}" for k, v in best_metrics.items()) or "N/A"),
            "metric_table": metric_table,
            "memory_section": memory_section,
            "champion_path": champion_path,
            "lang_fence": self._code_fence,
            "current_code": code[-30000:],
            "contract": contract,
            "scope_names": ", ".join(sorted(
                {str(s) for s in scope} if isinstance(scope, list) else [str(scope)]
            )) if scope else "",
            # Legacy aliases (ported prompts from the original harnesses)
            "TARGET_FILE": str((spec.get("data") or {}).get("score_target", "")),
            "ORIGINAL_SIZE": str(self._target_size(spec)),
            "BEST_SIZE": str(best_metrics.get("compressed_size", "N/A")) if best_metrics else "N/A",
            "BEST_RATIO": self._best_ratio(spec, best_metrics),
            "HISTORY_BLOB": self.memory.build_history_blob(),
            "CURRENT_CODE": code[-30000:],
            "STAGNATION": str(self.state.stagnation),
        }
        # Sloppy goals become measurable directives; tier-aware output
        # contract appended for the model size at hand.
        from .promptlib import detect_tier, expand_goal, generation_boost
        variables["goal"] = expand_goal(variables["goal"])
        spec_for_prompt = dict(spec)
        spec_for_prompt["prompts"] = {**(spec.get("prompts") or {}), "goal": variables["goal"]}
        # Merge framework default blocks with project blocks (project wins),
        # then assemble per the spec's block configuration.
        from .skills import load_blocks
        default_dir = Path(__file__).resolve().parent / "prompts_blocks"
        merged = load_blocks(default_dir)
        merged.update(load_blocks(self.project.prompts_dir / "blocks"))
        prompt = skills_mod.assemble_prompt(
            spec_for_prompt,
            variables,
            self.project.prompts_dir / "blocks",
            stage="generation",
            blocks=merged,
            default_structural=(["contract", "metrics", "memory", "current_code", "output_format"]
                                + (["scope"] if scope else [])),
        )
        tier = detect_tier(self.orchestrator.active_config)
        return prompt + "\n\n" + generation_boost(tier, self._code_lang)

    def _target_size(self, spec: Dict[str, Any]) -> int:
        target = str((spec.get("data") or {}).get("score_target", ""))
        if not target:
            return 0
        p = Path(target)
        if not p.is_absolute():
            p = self.project.path / p
        try:
            return p.stat().st_size
        except Exception:
            return 0

    def _best_ratio(self, spec: Dict[str, Any], best_metrics: Dict[str, Any]) -> str:
        size = best_metrics.get("compressed_size")
        orig = self._target_size(spec)
        if size is None or orig <= 0:
            return "N/A"
        try:
            return f"{float(size) / orig:.9f}"
        except (TypeError, ValueError):
            return "N/A"

    def _last_memo(self) -> str:
        d = self.memory.memos_dir
        if not d.exists():
            return ""
        files = sorted(d.glob("gen_*.md"))
        if not files:
            return ""
        try:
            return files[-1].read_text(encoding="utf-8")
        except Exception:
            return ""

    # ======================================================================
    # result handling (SELECT)
    # ======================================================================

    def _on_result(self, msg: Dict[str, Any]) -> None:
        result = msg.get("result", {})
        job_id = msg.get("job_id")
        job = None
        gen = None
        with self._lock:
            for g, j in list(self._in_flight.items()):
                if j.get("job_id") == job_id:
                    job = j
                    gen = g
                    self._in_flight.pop(g, None)
                    break
        # Dashboard cards: remember the worker's final metrics/outcome.
        self.pool.set_worker_result(msg.get("worker_id"), result)
        if gen is None:
            return
        try:
            self._apply_result(gen, job, result)
        except Exception as e:
            self._log(f"result handling error gen {gen}: {e}")

    def _active_score_type(self) -> tuple:
        """Return (key, type_spec) for the project's active score type."""
        spec = self.project.spec
        scores = spec.get("scores") or {}
        types = scores.get("types") or {}
        active = scores.get("active")
        if active and active in types:
            return active, types[active]
        from .scores import synthesize_score_type
        return "weighted_composite", synthesize_score_type(spec.get("metrics", {}))

    def _apply_result(self, gen: int, job: Dict[str, Any], result: Dict[str, Any]) -> None:
        spec = self.project.spec
        schema = spec.get("metrics", {})
        baseline = bool(job and job.get("baseline"))
        ok = bool(result.get("ok"))
        metrics = result.get("metrics", {})
        outcome = result.get("outcome", "unknown")

        if not ok:
            entry = {"generation": gen, "outcome": outcome, "detail": result.get("reason", "")[:800]}
            if baseline and not self.state.best.get("fitness"):
                entry["detail"] = "BASELINE FAILED: " + entry["detail"]
            self.state.append_history(entry)
            self.state.save()
            self.results.append({**{"generation": gen, "outcome": outcome}, **metrics})
            self._emit_state()
            self._log(f"gen {gen}: {outcome} ({result.get('stage')})")
            self._maybe_llm_repair(gen, job, result, baseline)
            return

        # HARD CONSTRAINTS: a metric with a `constraint` REJECTS the
        # candidate outright — no fitness weighting compensates. This is
        # where "without changing the output" is enforced, not begged.
        from .scores import check_constraints
        violations = check_constraints(metrics, schema)
        if violations:
            self.state.append_history({"generation": gen, "outcome": "constraint_violated",
                                       "detail": "; ".join(violations)})
            self.state.save()
            self.results.append({**{"generation": gen, "outcome": "constraint_violated"}, **metrics})
            self._emit_state()
            self._log(f"gen {gen}: constraint_violated {'; '.join(violations)}")
            return

        score_key, score_type = self._active_score_type()
        # Two-stage scoring: when any score step declares stage "confirm",
        # selection uses the CONFIRM metrics (the robust measurement) — the
        # screen metrics still appear in results/telemetry but never crown a
        # champion on screen-noise alone.
        sel_metrics = result.get("confirm_metrics") or metrics
        if sel_metrics:
            fitness = evaluate_score_type(sel_metrics, score_type)
        else:
            fitness = evaluate_score_type(metrics, score_type)
        if fitness is None:
            self.state.append_history({"generation": gen, "outcome": "no_score", "detail": f"missing metrics for score '{score_key}': {str(metrics)[:300]}"})
            self.state.save()
            self._emit_state()
            return

        if job and job.get("gen_dir"):
            self._write_diff_summary(
                Path(job["gen_dir"]),
                str(Path(job["gen_dir"]) / f"candidate{self._code_ext}"))
        self._maybe_prune_runs()

        best_f = self.state.best.get("fitness")
        # hysteresis >= 1: 1 = any strict improvement; >1 requires beating
        # the champion by that factor.  Values < 1 are always wrong (they
        # accept WORSE results as improvements) — clamp defensively.
        hysteresis = max(1.0, float((spec.get("select") or {}).get("hysteresis", 1.0001)))
        direction = score_type.get("direction", "higher")
        if best_f is None:
            improved = True
        elif direction == "lower":
            improved = fitness < float(best_f) / hysteresis
        else:
            improved = fitness > float(best_f) * hysteresis

        entry = {
            "generation": gen,
            "outcome": "ok" if improved else "valid",
            "detail": f"fitness={fitness:.5f} " + " ".join(f"{k}={v}" for k, v in metrics.items()),
            "fitness": fitness,
            "metrics": metrics,
        }
        fixes = result.get("build_fixes") or []
        if fixes:
            entry["detail"] = "AUTOFIX[" + ", ".join(fixes) + "] " + entry["detail"]
        # Skill scoreboard — per (model, skill):
        #  one-shot = passed the pipeline with NO corrective loop
        #             (no autofix, no LLM repair) on the first try;
        #  win      = this generation became the champion.
        gen_sid = self._gen_server.get(gen)
        repaired = bool(job and job.get("repaired"))
        if gen_sid and not fixes and not repaired:
            self.orchestrator.record_outcome(gen_sid, "generation", "oneshot")
        if repaired:
            rsid = self._repair_server.get(gen)
            if rsid:
                self.orchestrator.record_outcome(rsid, "llm_repair", "oneshot")
                self.orchestrator.record_outcome(rsid, "llm_repair", "win")
        self.state.append_history(entry)

        if improved:
            if gen_sid:
                self.orchestrator.record_outcome(gen_sid, "generation", "win")
            code = ""
            if job:
                cand = Path(job["gen_dir"]) / f"candidate{self._code_ext}"
                if cand.exists():
                    code = cand.read_text(encoding="utf-8")
            champion_path = self.project.best_dir / f"program{self._code_ext}"
            champion_path.parent.mkdir(parents=True, exist_ok=True)
            if code:
                champion_path.write_text(code, encoding="utf-8")
                # Legacy parity: verify the champion write landed intact —
                # a torn write here would silently poison every later diff.
                try:
                    if champion_path.read_text(encoding="utf-8") != code:
                        self._log("WARNING: champion write verification failed — re-writing")
                        champion_path.write_text(code, encoding="utf-8")
                except Exception as e:
                    self._log(f"WARNING: champion write read-back failed: {e}")
            self.state.set_best({
                "fitness": fitness,
                "metrics": metrics,
                "code_path": str(champion_path),
                "artifact": result.get("artifact"),
                "generation": gen,
            })
            self.state.save()
            self.results.append({**{"generation": gen, "outcome": "NEW_BEST", "fitness": fitness}, **metrics})
            self._notify_best(gen, fitness, metrics, schema)
            # lessons (spec-driven)
            if (spec.get("skills") or {}).get("lessons", {}).get("enabled"):
                self._generate_lesson(gen, code or "", metrics, result)
            self._maybe_deepwork()
            self._log(f"gen {gen}: NEW BEST fitness={fitness:.5f}")
        else:
            self.state.save()
            self.results.append({**{"generation": gen, "outcome": "valid", "fitness": fitness}, **metrics})
            self._log(f"gen {gen}: valid fitness={fitness:.5f} (best {best_f:.5f})")

    def set_autofix_settings(self, max_tries: Optional[int] = None,
                             repair_max: Optional[int] = None) -> Dict[str, Any]:
        """Per-run compile-loop knobs (KAI AUTOFIX): deterministic autofix
        turn cap and LLM repair attempt cap for THIS engine. None = keep
        the current value. repair_max 0 = LLM repair OFF (deterministic
        autofix only, then fail)."""
        if max_tries is not None:
            self._autofix_settings["max_tries"] = max(1, int(max_tries))
        if repair_max is not None:
            self._autofix_settings["repair_max"] = max(0, int(repair_max))
        return dict(self._autofix_settings)

    def _autofix_effective(self) -> Dict[str, int]:
        """Effective compile-loop caps: KAI AUTOFIX override > project
        engine.autofix > config autofix > built-in defaults."""
        cfg = self.orchestrator.cfg
        af = cfg.data.get("autofix") or {}
        spec_af = (self.project.spec.get("engine") or {}).get("autofix") or {}
        max_tries = self._autofix_settings.get("max_tries")
        if max_tries is None:
            max_tries = spec_af.get("tries")
        if max_tries is None:
            max_tries = int(af.get("max_tries", 5) or 5)
        repair = self._autofix_settings.get("repair_max")
        if repair is None:
            repair = spec_af.get("repair")
        if repair is None:
            repair = int(af.get("llm_repair_max", 3) or 3)
        return {
            "max_tries": max(1, int(max_tries)),
            "repair_max": max(0, int(repair)),
        }

    def _maybe_llm_repair(self, gen: int, job: Optional[Dict[str, Any]],
                          result: Dict[str, Any], baseline: bool) -> None:
        """Gate for the LAST-RESORT autofix layer — the compile loop.

        Fires only when ALL of: build failed; the deterministic fixer was
        engaged (mode != off) and exhausted (no fixes applied or fixes did
        not help); the generation's repair attempts are still under the
        cap (engine override / config autofix.llm_repair_max — flag on =
        deterministic autofix + LLM repair in a bounded loop; flag off or
        cap 0 = deterministic autofix only, then fail); the global
        autofix.llm_repair switch is on; and the run was not the sacred
        baseline.
        """
        if baseline:
            return
        if result.get("outcome") != "build_fail":
            return
        if result.get("build_fixes"):
            return  # deterministic fixer still making progress
        if job is None:
            return
        try:
            from .autofix import resolve_mode
            if resolve_mode(self.project.spec) == "off":
                return
            cfg = self.orchestrator.cfg
            if not (cfg.data.get("autofix") or {}).get("llm_repair", True):
                return
            max_attempts = self._autofix_effective()["repair_max"]
        except Exception:
            return
        if max_attempts <= 0:
            return
        used = self._llm_repair_count.get(gen, 0)
        if used >= max_attempts:
            return
        self._llm_repair_count[gen] = used + 1
        threading.Thread(target=self._llm_repair_run, args=(gen, job, result),
                         daemon=True).start()
    def _llm_repair_run(self, gen: int, job: Dict[str, Any],
                        result: Dict[str, Any]) -> None:
        """One guarded LLM repair pass: rewrite the candidate, re-run the
        FULL pipeline. Hardcoded guardrails: danger scan, length cap,
        only the candidate file is touched, repaired code still passes
        build+verify+score with the project's own timeout/memory limits."""
        try:
            gen_dir = Path(job["gen_dir"])
            cand = gen_dir / f"candidate{self._code_ext}"
            if not cand.exists():
                return
            source = cand.read_text(encoding="utf-8", errors="replace")
            stderr = str(result.get("stderr_tail") or result.get("reason") or "")[-3000:]
            from .promptlib import repair_prompt
            prompt = repair_prompt(
                lang=self._code_lang,
                goal=str((self.project.spec.get("prompts") or {}).get("goal") or "")[:400],
                source=source,
                stderr=stderr,
            )
            reply, rsid = self.orchestrator.request_stream(
                prompt, pipeline_id=gen, min_tier="small", max_retries=1,
                skill="llm_repair")
            fixed = skills_mod.extract_code(reply, self._code_lang)
            if not fixed or fixed.strip() == source.strip():
                self._log(f"gen {gen}: LLM repair produced no change — dropping")
                return
            bad = skills_mod.find_dangerous(fixed, self._code_lang)
            if bad:
                self._log(f"gen {gen}: LLM repair REJECTED by guardrails: {bad}")
                return
            if len(fixed) > 30000:
                self._log(f"gen {gen}: LLM repair REJECTED: {len(fixed)} chars (> 30000 cap)")
                return
            cand.write_text(fixed, encoding="utf-8")
            self._repair_server[gen] = rsid
            (gen_dir / "repair.txt").write_text(
                f"LLM last-resort repair (gen {gen}):\n{stderr}\n", encoding="utf-8")
            self.state.append_history({"generation": gen, "outcome": "llm_repair",
                                       "detail": "last-resort LLM repair applied — pipeline re-queued"})
            self.state.save()
            self.results.append({"generation": gen, "outcome": "llm_repair"})
            self._emit_state()
            self._submit(gen, str(cand), gen_dir, baseline=bool(job.get("baseline")),
                         repaired=True)
            self._log(f"gen {gen}: LLM repair applied — candidate rewritten, pipeline re-queued")

        except Exception as e:
            self._log(f"gen {gen}: LLM repair failed: {e}")

    def _notify_best(self, gen: int, fitness: float, metrics: Dict[str, float], schema: Dict[str, Any]) -> None:
        lines = [f"🏆 NEW BEST (gen {gen}): fitness={fitness:.5f}"]
        for key, ms in schema.items():
            if key in metrics:
                lines.append(f"  {key} = {metrics[key]:.4f} {ms.get('unit', '')}")
        resp = send_message("\n".join(lines))
        if resp and resp.get("ok"):
            try:
                pin_message(resp["result"]["message_id"])
            except Exception:
                pass
        # GitHub upload (project-level)
        gh = (self.project.spec.get("github") or {})
        if gh.get("enabled"):
            try:
                from .github import upload_best
                code = Path(self._champion_path()).read_bytes()
                readme = self._readme_text(gen, fitness, metrics)
                upload_best(gh, code, readme, gen)
            except Exception as e:
                self._log(f"github upload failed: {e}")

    def _readme_text(self, gen: int, fitness: float, metrics: Dict[str, float]) -> str:
        spec = self.project.spec
        tpl = (spec.get("github") or {}).get("readme_template") or "# Best result\n\n- **Generation:** {gen}\n- **Fitness:** {fitness}\n{metrics}"
        rows = "\n".join(f"- **{k}:** {v}" for k, v in metrics.items())
        return tpl.replace("{gen}", str(gen)).replace("{fitness}", f"{fitness:.5f}").replace("{metrics}", rows)

    # ======================================================================
    # deepwork + lessons
    # ======================================================================

    def _maybe_deepwork(self) -> None:
        spec = (self.project.spec.get("skills") or {}).get("deepwork") or {}
        if not spec.get("enabled"):
            return
        every = int(spec.get("every", 5))
        gen = self.state.generation
        if gen - self._deepwork_last < every:
            return
        if len(self.results.read()) < int(spec.get("min_rows", 5)):
            return
        self._deepwork_last = gen
        t = threading.Thread(target=self._run_deepwork, args=(gen,), daemon=True)
        t.start()

    def _run_deepwork(self, gen: int) -> None:
        try:
            from . import skills as sk
            tools = {
                "y": lambda args: self._deepwork_yelook(args),
                "r": lambda args: self._deepwork_read(args),
                "p": lambda args: self.results.query(args),
                "l": lambda args: self._deepwork_memo_lesson(args, kind="lesson"),
                "m": lambda args: self._deepwork_memo_lesson(args, kind="memo"),
            }
            prompt = sk.DEFAULT_DEEPWORK_PROMPT.replace("{project_name}", self.project.name)
            briefing = self._deepwork_briefing()
            prompt = prompt.replace("{briefing}", briefing)

            def req(prompt_text: str) -> str:
                out, sid = self.orchestrator.request_stream(
                    prompt_text, skill="deepwork")
                self._deepwork_sid = sid
                return out

            agent = sk.DeepworkAgent(prompt, tools, req)
            memo = agent.run()
            self.memory.save_memo(gen, memo)
            if getattr(self, "_deepwork_sid", None):
                self.orchestrator.record_outcome(self._deepwork_sid, "deepwork", "oneshot")
            self._log(f"deepwork memo saved (gen {gen}, {len(memo)} chars)")
        except Exception as e:
            self._log(f"deepwork failed: {e}")

    def _deepwork_yelook(self, args: str) -> str:
        rows = self.results.read()
        if not rows:
            return "ERROR: results store empty"
        # Simple listing: latest N rows with fitness
        try:
            n = 10
            parts = args.split()
            if parts and parts[0].isdigit():
                n = int(parts[0])
            out = ["generation fitness " + " ".join(sorted(set(rows[0].keys()) - {"generation", "outcome"}))]
            for r in rows[-n:]:
                out.append(f"{r.get('generation')} {r.get('fitness', '')}")
            return "\n".join(out)
        except Exception as e:
            return f"ERROR: {e}"

    def _deepwork_read(self, args: str) -> str:
        folder = args.strip().split()[0]
        gen_dir = self.project.runs_dir / folder
        if not gen_dir.exists():
            gen_dir = self.project.runs_dir / f"gen_{int(folder):06d}"
        if not gen_dir.exists():
            return f"ERROR: folder {folder} not found"
        cand = gen_dir / f"candidate{self._code_ext}"
        if not cand.exists():
            cand = gen_dir / f"program{self._code_ext}"
        if not cand.exists():
            return f"ERROR: no code in {folder}"
        return cand.read_text(encoding="utf-8")[:25000]

    def _deepwork_memo_lesson(self, args: str, kind: str) -> str:
        folder = args.strip().split()[0]
        if kind == "lesson":
            p = self.project.path / "lessons.txt"
            return p.read_text(encoding="utf-8")[:5000] if p.exists() else f"NO LESSON for {folder}"
        try:
            return self.memory.load_memo(int(folder))
        except Exception:
            return f"NO MEMO for {folder}"

    def _deepwork_briefing(self) -> str:
        rows = self.results.read()
        if not rows:
            return "(no results yet)"
        out = [f"{len(rows)} scored generations; latest fitness values:"]
        for r in rows[-15:]:
            out.append(f"  gen {r.get('generation')}: fitness={r.get('fitness', 'N/A')} outcome={r.get('outcome')}")
        return "\n".join(out)

    def _generate_lesson(self, gen: int, code: str, metrics: Dict[str, float], result: Dict[str, Any]) -> None:
        try:
            template_path = self.project.prompts_dir / "lesson.md"
            if not template_path.exists():
                return
            template = template_path.read_text(encoding="utf-8")
            prompt = (template
                      .replace("{{CODE_SNIPPET}}", code[:25000])
                      .replace("{{SCORE}}", f"{metrics}")
                      .replace("{{OUTPUT}}", str(result.get("stdout_tail", ""))[:2000]))
            lesson, lsid = self.orchestrator.request_stream(prompt, skill="lesson")
            self.memory.save_lesson(lesson)
            self.orchestrator.record_outcome(lsid, "lesson", "oneshot")
            self._log(f"lesson saved (gen {gen})")

        except Exception as e:
            self._log(f"lesson generation failed: {e}")

    # ======================================================================
    # plumbing
    # ======================================================================

    def _pump_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.pool.pump()
            except Exception:
                pass
            self._check_drain()
            time.sleep(0.2)

    def _check_drain(self) -> None:
        """PAUSING -> PAUSED once every in-flight unit has finished:
        no LLM streams, no producer mid-cycle, no worker evaluations."""
        if self.engine_state != STATE_PAUSING:
            return
        with self._lock:
            busy = self._active_generations > 0 or bool(self._in_flight)
        if busy or self.sessions.iter_active():
            return
        self._set_state(STATE_PAUSED)
        self._log("pause complete — system paused")

    def _on_progress(self, msg: Dict[str, Any]) -> None:
        if self.events.on_worker:
            self.events.on_worker(msg)

    def _emit_state(self) -> None:
        if self.events.on_state:
            self.events.on_state(self.project.id)

    def _log(self, msg: str) -> None:
        ts = time.strftime("[%Y-%m-%d][%H:%M:%S]")
        line = f"{ts} {msg.lower()}"
        self._last_log.append(line)
        if len(self._last_log) > 500:
            self._last_log = self._last_log[-500:]
        print(line)
        if self.events.on_log:
            self.events.on_log(line)

    def set_prompt_override(self, content: str) -> None:
        self.prompt_override = content
        self.prompt_override_active = bool(content and content.strip())
        self._log("prompt override " + ("active" if self.prompt_override_active else "cleared"))

    def clear_prompt_override(self) -> None:
        self.prompt_override = None
        self.prompt_override_active = False
        self._log("prompt override cleared")

    # ======================================================================
    # GUI-facing snapshot + controls
    # ======================================================================

    def snapshot(self) -> Dict[str, Any]:
        schema = self.project.spec.get("metrics", {})
        best = self.state.best
        goodness = metric_goodness(best.get("metrics", {}), schema, self.state.baselines) if best.get("metrics") else {}
        score_key, score_type = self._active_score_type()
        scores_cfg = self.project.spec.get("scores") or {}
        types = scores_cfg.get("types") or {}
        if not types:
            types = {"weighted_composite": score_type}
        spec_file = self.project.path / "project.json"
        spec_revision = file_sha256(spec_file)[:12] if spec_file.exists() else None
        return {
            "project_id": self.project.id,
            "project_name": self.project.name,
            "state": self.state.snapshot(),
            "engine_state": self.engine_state,
            "generating": self.is_generating,
            "metrics_schema": schema,
            "metric_goodness": goodness,
            "scores": {"active": score_key, "types": types},
            "workers": self.pool.list_workers(),
            "multi": self._multi,
            "llm": self.orchestrator.status(),
            "guardrails": self._guardrail_status(),
            "sessions": self.sessions.snapshot(),
            "logs": self._last_log[-80:],
            "spec_revision": spec_revision,
            "autofix": self._autofix_effective(),
            "valid_rate": self._valid_rate(),
            "fuzzy_top_n": self._fuzzy_top_n,
        }

    def _guardrail_status(self) -> Dict[str, Any]:
        from .guardrails import guardrail_state
        st = guardrail_state()
        st["project_enabled"] = bool((self.project.spec.get("guardrails") or {}).get("enabled", True))
        return st

    def add_worker(self) -> Dict[str, Any]:
        return self.pool.add_worker()

    def remove_worker(self, worker_id: int, kill: bool = False) -> bool:
        return self.pool.remove_worker(worker_id, kill=kill)

    def kill_worker(self, worker_id: int) -> bool:
        return self.pool.kill_worker(worker_id)

    def kill_worker_process(self, worker_id: int) -> bool:
        return self.pool.kill_worker_process(worker_id)
