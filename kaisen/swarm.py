# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Swarm coordinator — parallel multi-agent work over the orchestrator's
active servers, with REAL validation through the KAISEN pipeline.

This replaces legacy/swarm_server.py (planner/contextualizer/executor/
synthesizer over raw HTTP) with an engine-integrated design:

  - concurrency through ModelOrchestrator.request_stream (respects each
    server's max_concurrent, retries, bans, cancellation)
  - every code draft is evaluated by the project's ACTUAL pipeline
    (build -> verify -> score) in a temp copy — no untested output
  - jobs are cancellable, event-streamed, and visible in the GUI
  - prompt quality comes from the prompt library (tier-aware)

Kinds:
  code_forge — N independent improvement drafts; each scored; ranked.
  pipeline   — N parallel suggested project specs, each already validated
               by the suggest loop (structure + guardrails + smoke run).
  answer     — planner -> parallel executors -> synthesizer.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import promptlib
from .languages import ext_from_lang, fence_from_lang
from .llm import GenerationCancelled, ServerError

MAX_EVENTS = 400
DRAFT_MAX_PROMPT_CODE = 12000


class SwarmJob:
    """One swarm run: tasks, live events, results."""

    def __init__(self, job_id: str, kind: str, project_id: Optional[str], request: str, n: int,
                 min_tier: str = "tiny"):
        self.id = job_id
        self.kind = kind
        self.project_id = project_id
        self.request = request
        self.n = n
        self.min_tier = min_tier if min_tier in ("tiny", "small", "large") else "tiny"
        self.state = "starting"          # starting | running | done | failed | cancelled
        self.events: List[Dict[str, Any]] = []
        self.tasks: List[Dict[str, Any]] = []
        self.results: List[Dict[str, Any]] = []
        self.error = ""
        self.cancel = threading.Event()
        self.created = time.time()
        self._lock = threading.Lock()

    def emit(self, etype: str, data: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self.events.append({"t": round(time.time(), 3), "type": etype, "data": data or {}})
            if len(self.events) > MAX_EVENTS:
                self.events = self.events[-MAX_EVENTS:]

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def task_state(self, idx: int, state: str, extra: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            while len(self.tasks) <= idx:
                self.tasks.append({})
            self.tasks[idx]["state"] = state
            if extra:
                self.tasks[idx].update(extra)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "project_id": self.project_id,
                "request": self.request[:400],
                "n": self.n,
                "state": self.state,
                "error": self.error[:600],
                "created": self.created,
                "events": self.events[-120:],
                "tasks": list(self.tasks),
                "results": list(self.results),
            }


class SwarmCoordinator:
    def __init__(self, orchestrator, project_getter: Optional[Callable[[str], Any]] = None):
        self.orchestrator = orchestrator
        self._project_getter = project_getter
        self._jobs: Dict[str, SwarmJob] = {}
        self._lock = threading.Lock()

    def _tier(self) -> str:
        return promptlib.detect_tier(self.orchestrator.active_config)

    # -- lifecycle ---------------------------------------------------------
    def start(self, kind: str, request: str, project_id: Optional[str] = None,
              n: int = 3, max_concurrent: int = 4, min_tier: str = "tiny") -> SwarmJob:
        kind = kind if kind in ("code_forge", "pipeline", "answer") else "answer"
        n = max(1, min(int(n), 12))
        job = SwarmJob(uuid.uuid4().hex[:12], kind, project_id, request, n, min_tier=min_tier)
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job, max_concurrent), daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> Optional[SwarmJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [j.snapshot() for j in self._jobs.values()]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        job.cancel.set()
        job.emit("cancelled")
        return True

    # -- runners -----------------------------------------------------------
    def _request(self, job: SwarmJob, prompt: str, label: str, min_tier: str = "tiny") -> tuple:
        if job.cancel.is_set():
            raise GenerationCancelled("swarm cancelled")
        text, sid = self.orchestrator.request_stream(
            prompt, cancel_event=job.cancel, min_tier=min_tier, skill="swarm")
        job.emit("stream_done", {"label": label, "server": sid, "len": len(text)})
        return text, sid

    def _run(self, job: SwarmJob, max_concurrent: int) -> None:
        try:
            job.set_state("running")
            if job.kind == "code_forge":
                self._run_code_forge(job, max_concurrent)
            elif job.kind == "pipeline":
                self._run_pipeline(job, max_concurrent)
            else:
                self._run_answer(job, max_concurrent)
            if job.cancel.is_set():
                job.set_state("cancelled")
            else:
                job.set_state("done")
            job.emit("finished", {"state": job.state})
        except Exception as e:
            job.set_state("failed")
            job.error = str(e)
            job.emit("error", {"message": str(e)[:600]})

    # -- code_forge --------------------------------------------------------
    def _run_code_forge(self, job: SwarmJob, max_concurrent: int) -> None:
        project = self._require_project(job)
        language = str(project.spec.get("language", "c"))
        champion_path = project.best_dir / f"program{ext_from_lang(language)}"
        if champion_path.exists():
            champion = champion_path.read_text(encoding="utf-8")
        else:
            baseline = project.path / str((project.spec.get("data") or {}).get("baseline_source", ""))
            champion = baseline.read_text(encoding="utf-8") if baseline.exists() else ""
        if not champion:
            raise RuntimeError("no champion/baseline source to improve")
        goal = str((project.spec.get("prompts") or {}).get("goal", "") or "Improve the program.")
        task = f"Improve this {language} program.\nGOAL: {goal}\n\nCURRENT PROGRAM:\n```{fence_from_lang(language)}\n{champion[:DRAFT_MAX_PROMPT_CODE]}\n```"
        tier = self._tier()
        job.emit("phase", {"phase": "forge", "message": f"{job.n} agents drafting improvements"})

        drafts: List[Dict[str, Any]] = [None] * job.n  # type: ignore
        lock = threading.Lock()

        def forge(idx: int) -> None:
            job.task_state(idx, "streaming")
            try:
                prompt = promptlib.swarm_executor(tier, job.request, task, language)
                raw, draft_sid = self._request(job, prompt, f"draft-{idx + 1}", min_tier=job.min_tier)
                from .skills import extract_code
                code = extract_code(raw, language) or raw.strip()
                job.task_state(idx, "evaluating", {"preview": code[:120]})
                res = self._evaluate(project, code)
                ok = bool(res.get("ok"))
                if ok and draft_sid and not (res.get("build_fixes") or []):
                    self.orchestrator.record_outcome(draft_sid, "swarm", "oneshot")
                with lock:
                    drafts[idx] = {
                        "code": code[:DRAFT_MAX_PROMPT_CODE],
                        "metrics": res.get("metrics"),
                        "ok": ok,
                        "stage": res.get("stage"),
                        "reason": (res.get("reason") or "")[:300],
                    }
                job.task_state(idx, "done", {"ok": ok})
            except GenerationCancelled:
                job.task_state(idx, "cancelled")
            except Exception as e:
                job.task_state(idx, "failed", {"error": str(e)[:200]})

        self._parallel(forge, job.n, max_concurrent)
        job.results = [d for d in drafts if d is not None]
        ranked = sorted(job.results, key=lambda d: (not d["ok"],) + self._worse_first(d["metrics"] or {}))
        for i, d in enumerate(ranked):
            d["rank"] = i + 1
        job.emit("results", {"count": len(job.results)})

    @staticmethod
    def _worse_first(metrics: Dict[str, Any]) -> tuple:
        # ranking key: prefer ok, then lower numeric values (heuristic —
        # direction-aware ranking happens in the GUI/apply step)
        return tuple(sorted(v for v in metrics.values() if isinstance(v, (int, float))))

    def _evaluate(self, project: Any, code: str) -> Dict[str, Any]:
        """Run the REAL pipeline on a draft in a temp copy of the project."""
        from .pipeline import run_pipeline
        from .projects import Project as ProjectCls
        tmp = Path(tempfile.mkdtemp(prefix="kaisen-swarm-"))
        try:
            def _ignore(d: str, names: List[str]) -> set:
                return {x for x in names if x in ("runs", "best", "state.json", "results.csv", "seen_hashes.json", ".kaisen_scripts")}
            shutil.copytree(project.path, tmp / project.id, ignore=_ignore)
            tp = ProjectCls(tmp / project.id)
            cand = tmp / project.id / f"candidate{ext_from_lang(tp.spec.get('language', 'c'))}"
            cand.write_text(code, encoding="utf-8")
            res = run_pipeline(tp, cand, tmp / project.id / "smoke")
            return {
                "ok": bool(res.get("ok")),
                "metrics": res.get("metrics") or {},
                "stage": res.get("stage"),
                "reason": (res.get("reason") or "")[:500],
            }
        except Exception as e:
            return {"ok": False, "metrics": {}, "stage": "worker", "reason": str(e)[:300]}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- pipeline (parallel suggest) --------------------------------------
    def _run_pipeline(self, job: SwarmJob, max_concurrent: int) -> None:
        project = self._require_project(job)
        language = str(project.spec.get("language", "c"))
        baseline_name = str((project.spec.get("data") or {}).get("baseline_source", "") or "")
        baseline = project.path / baseline_name if baseline_name else None
        if not baseline or not baseline.is_file():
            raise RuntimeError("baseline file missing — set data.baseline_source")
        code = baseline.read_text(encoding="utf-8")
        goal = str((project.spec.get("prompts") or {}).get("goal", "") or job.request or "Improve the program.")
        from .suggest import suggest_project

        specs: List[Dict[str, Any]] = [None] * job.n  # type: ignore
        lock = threading.Lock()

        def suggest(idx: int) -> None:
            job.task_state(idx, "streaming")
            try:
                def req(prompt: str) -> str:
                    return self._request(job, prompt, f"pipeline-{idx + 1}")[0]
                result = suggest_project(req, goal, code, None, language=language, max_rounds=4)
                with lock:
                    specs[idx] = {
                        "ok": bool(result.get("ok")),
                        "spec": result.get("suggested_spec") if result.get("ok") else None,
                        "notes": (result.get("notes") or [])[-3:],
                        "error": result.get("error", ""),
                    }
                job.task_state(idx, "done", {"ok": bool(result.get("ok"))})
            except GenerationCancelled:
                job.task_state(idx, "cancelled")
            except Exception as e:
                job.task_state(idx, "failed", {"error": str(e)[:200]})

        self._parallel(suggest, job.n, max_concurrent)
        job.results = [s for s in specs if s is not None]
        job.emit("results", {"count": len(job.results)})

    # -- answer (planner -> executors -> synthesizer) ----------------------
    def _run_answer(self, job: SwarmJob, max_concurrent: int) -> None:
        tier = self._tier()
        job.emit("phase", {"phase": "planner", "message": "planning"})
        plan_raw, _ = self._request(job, promptlib.swarm_planner(tier, job.request), "planner", min_tier="small")
        tasks = self._parse_tasks(plan_raw)
        if not tasks:
            tasks = [job.request]
        job.emit("phase", {"phase": "execute", "message": f"{len(tasks)} sub-tasks"})

        outputs: List[str] = [""] * len(tasks)
        lock = threading.Lock()

        def exec_task(idx: int) -> None:
            job.task_state(idx, "streaming", {"task": tasks[idx][:120]})
            try:
                prompt = promptlib.swarm_executor_text(tier, job.request, tasks[idx])
                out, _ = self._request(job, prompt, f"task-{idx + 1}", min_tier=job.min_tier)
                with lock:
                    outputs[idx] = out
                job.task_state(idx, "done")
            except GenerationCancelled:
                job.task_state(idx, "cancelled")
            except Exception as e:
                job.task_state(idx, "failed", {"error": str(e)[:200]})

        self._parallel(exec_task, len(tasks), max_concurrent)
        cleaned = [self._clean(o) or f"[ERROR: task {i + 1} failed]" for i, o in enumerate(outputs)]
        job.emit("phase", {"phase": "synthesize", "message": "synthesizing"})
        final, _ = self._request(job, promptlib.swarm_synthesizer(tier, job.request, cleaned), "synthesizer", min_tier="small")
        job.results = [{"ok": True, "answer": final, "tasks": len(tasks)}]
        job.emit("results", {"count": 1})

    # -- helpers -----------------------------------------------------------
    def _parallel(self, fn: Callable[[int], None], count: int, max_concurrent: int) -> None:
        sem = threading.Semaphore(max(1, max_concurrent))
        threads: List[threading.Thread] = []

        def runner(idx: int) -> None:
            with sem:
                fn(idx)

        for i in range(count):
            t = threading.Thread(target=runner, args=(i,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    @staticmethod
    def _parse_tasks(raw: str) -> List[str]:
        tasks: List[str] = []
        for line in raw.splitlines():
            m = re.match(r"^\s*(\d+)[.)\-:]\s+(.+)$", line.strip())
            if m:
                tasks.append(m.group(2).strip())
        if not tasks:
            tasks = [raw.strip()[:2000]]
        return tasks[:10]

    @staticmethod
    def _clean(text: str) -> str:
        # Reasoning must NEVER leak into the next step (token cost + prompt
        # pollution). Strip xml blocks and the legacy word-delimited form.
        for start_pat, end_pat in (
            (r"<thinking>", r"</thinking>"),
            (r"<reasoning>", r"</reasoning>"),
            (r"\bthinking\b", r"\bresponse\b"),
            (r"\breasoning\b", r"\banswer\b"),
        ):
            while True:
                m = re.search(start_pat, text, re.IGNORECASE)
                if not m:
                    break
                end = re.search(end_pat, text[m.end():], re.IGNORECASE)
                if end:
                    text = text[:m.start()] + text[m.end() + end.end():]
                else:
                    text = text[:m.start()] + text[m.end():]
        return text.strip()

    def _require_project(self, job: SwarmJob) -> Any:
        if not self._project_getter:
            raise RuntimeError("no project provider attached to the swarm")
        project = self._project_getter(job.project_id)
        if project is None:
            raise RuntimeError(f"project '{job.project_id}' not found")
        return project
