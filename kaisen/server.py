# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Dashboard HTTP server.

Generic framework endpoints + active-project endpoints + live controls.
Runs in the main process next to the ProjectEngine.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None

from .config import TEMP_ROOT, FrameworkConfig, PROJECTS_DIR, get_config, save_secret
from .engine import STATE_PAUSED, STATE_STOPPED, STATE_STOPPING, ProjectEngine
from .projects import ProjectRegistry
from .guardrails import check_command, guardrail_state
from .util import load_json, save_json
from .suggest import _safe_rel_path


def _json(data: Any, status: int = 200):
    return web.json_response(data, status=status)


def _auth_middleware(api_key: str):
    """Optional server password, using only globally recognized auth forms:
    `Authorization: Bearer <key>` (API clients) and HTTP Basic auth
    (browsers get the standard password prompt; any username, the key as
    password). Constant-time comparison. Never installed when no key is
    configured — everything stays open by default."""

    @web.middleware
    async def _check(request, handler):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            ok = hmac.compare_digest(auth[7:].strip(), api_key)
        elif auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace")
                ok = hmac.compare_digest(decoded.split(":", 1)[-1], api_key)
            except Exception:
                ok = False
        else:
            ok = False
        if not ok:
            return web.json_response(
                {"ok": False, "error": "unauthorized — server password required"},
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="KAISEN"'},
            )
        return await handler(request)

    return _check


class DashboardServer:
    def __init__(
        self,
        registry: ProjectRegistry,
        config: FrameworkConfig,
        engine: Optional[ProjectEngine] = None,
        host: str = "0.0.0.0",
        port: int = 8080,
        temp_root: Optional[Path] = None,
        restore_paused: bool = False,
    ):
        self.registry = registry
        self.cfg = config
        self.engines: Dict[str, ProjectEngine] = {}
        self._selected_project_id: Optional[str] = None
        # Crash-recovery file lives NEXT TO config.json (repo root in
        # production, temp dir in tests).
        self._pool_file = Path(config.path).parent / "engine_pool.json"
        if engine is not None:
            self.set_engine(engine)
        self.host = host
        self.port = port
        self.app = web.Application()
        # Optional server password. Default: no auth (loopback-only bind).
        # "server": {"api_key": "..."} in config.json; env KAISEN_API_KEY
        # wins. When set, EVERY route (pages, /api, /kai) requires it.
        key = os.environ.get("KAISEN_API_KEY", "").strip()
        if not key:
            key = str((self.cfg.data.get("server") or {}).get("api_key", "") or "").strip()
        self.api_key_set = bool(key)
        if key:
            self.app.middlewares.append(_auth_middleware(key))
        # TEMP ROOT — explicit opt-in scratch space for quick agent runs.
        # Everything under temp/ is wiped when the server closes AND at
        # the next startup (crash safety). The real projects/ + config are
        # NEVER touched by temp activity.
        import shutil as _shutil
        self._temp_root = Path(temp_root) if temp_root is not None else TEMP_ROOT
        _shutil.rmtree(self._temp_root, ignore_errors=True)
        self._temp_root.mkdir(parents=True, exist_ok=True)
        self._temp_registry = ProjectRegistry(self._temp_root)
        # Crash recovery: bring back the pool that was running when the
        # daemon died.  Temp projects are wiped at startup so only REAL
        # projects restore; restore_paused=True (tests) boots them paused.
        self._restore_paused = restore_paused
        self._restore_engine_pool(registry)
        self.app.on_cleanup.append(self._on_cleanup)
        self._runner = None
        self._site = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._suggest_orchestrator = None
        self._probe_inflight: set = set()
        self._suggest_state: Dict[str, Any] = {"running": False, "stage": "idle"}
        self._suggest_lock = threading.Lock()
        self._pending_data_file: Optional[Dict[str, str]] = None
        # Sticky KAI sessions: cookie -> KaiSession so an HTTP client keeps
        # its PROJECT selection between requests (see docs/KAI.md).
        self._kai_sessions: Dict[str, Any] = {}
        self._kai_lock = threading.Lock()
        # Server management must work BEFORE any engine exists (first run):
        # fall back to a bare orchestrator when no project engine is up.
        self._base_orchestrator = None
        self._swarm = None
        self._agent_state: Dict[str, Any] = {"running": False, "turns": [], "summary": "", "tokens": "", "started": None}
        self._agent_cancel = threading.Event()
        self._setup_routes()

    def _registry_for(self, pid: str):
        """(project, registry) resolution: REAL projects always win over
        temp ones — temp can never shadow the user's real setup."""
        p = self.registry.get(pid)
        if p is not None:
            return p, self.registry
        p = self._temp_registry.get(pid)
        if p is not None:
            return p, self._temp_registry
        raise KeyError(f"project '{pid}' not found")

    def _is_temp(self, project) -> bool:
        try:
            return str(project.path).startswith(str(self._temp_registry.root))
        except Exception:
            return False

    async def _on_cleanup(self, app):
        """Server close: stop temp engines and wipe the temp root."""
        import shutil as _shutil
        for pid, eng in list(self.engines.items()):
            if eng and self._is_temp(eng.project):
                try:
                    eng.stop()
                except Exception:
                    pass
                self.engines.pop(pid, None)
                if self._selected_project_id == pid:
                    self._selected_project_id = None
        _shutil.rmtree(self._temp_root, ignore_errors=True)

    @property
    def engine(self) -> Optional[ProjectEngine]:
        """The SELECTED engine (GUI/KAI session focus). The full pool of
        concurrently running project engines lives in self.engines."""
        if self._selected_project_id:
            return self.engines.get(self._selected_project_id)
        return None

    @engine.setter
    def engine(self, eng: Optional[ProjectEngine]) -> None:
        if eng is None:
            self._selected_project_id = None
            self._persist_engine_pool()
            return
        self.engines[eng.project.id] = eng
        self._selected_project_id = eng.project.id
        self._persist_engine_pool()

    def set_engine(self, engine: ProjectEngine) -> None:
        self.engine = engine

    def _engine_for(self, pid: Optional[str]) -> Optional[ProjectEngine]:
        if pid:
            return self.engines.get(pid)
        return self.engine

    def _persist_engine_pool(self) -> None:
        """Snapshot which projects are running (and how many pipelines each)
        to engine_pool.json — crash recovery: the next daemon boot restores
        the pool instead of silently losing every in-flight run."""
        try:
            data = {
                "selected": self._selected_project_id,
                "engines": {
                    pid: {"multi": getattr(eng, "_multi", 1)}
                    for pid, eng in self.engines.items()
                },
            }
            save_json(self._pool_file, data)
        except Exception:
            pass

    def _restore_engine_pool(self, registry: ProjectRegistry) -> None:
        """Bring back the pool recorded before the last shutdown/crash.
        Only real projects restore (temp/ is wiped at startup); engines
        boot with their saved multi.  Best-effort — never blocks startup."""
        data = load_json(self._pool_file, None)
        if not isinstance(data, dict):
            return
        engines = data.get("engines") or {}
        restored = 0
        for pid, info in engines.items():
            try:
                project = registry.get(pid)
                if project is None:
                    continue
                from .engine import EngineEvent, ProjectEngine
                from .llm import ModelOrchestrator
                eng = ProjectEngine(
                    project,
                    ModelOrchestrator(self.cfg),
                    registry,
                    worker_count=project.default_workers,
                    events=EngineEvent(),
                )
                eng.start(multi=int(info.get("multi") or project.default_multi),
                          paused=self._restore_paused)
                self.engines[pid] = eng
                restored += 1
            except Exception:
                continue
        sel = data.get("selected")
        if sel and sel in self.engines:
            self._selected_project_id = sel
        if restored:
            print(f"[KAISEN] restored {restored} engine(s) from engine_pool.json")
        elif engines:
            print("[KAISEN] engine_pool.json present but no engines restored (projects missing?)")

    def _engines_summary(self) -> List[Dict[str, Any]]:
        out = []
        for pid, eng in self.engines.items():
            st = eng.state
            best = (st.best or {}) if st else {}
            snap = None
            try:
                snap = eng.snapshot()
            except Exception:
                snap = None
            row = {
                "project_id": pid,
                "name": eng.project.name if eng.project else pid,
                "engine_state": eng.engine_state,
                "generation": (st.generation if st else 0),
                "paused": (st.paused if st else True),
                "best_fitness": best.get("fitness"),
                "best_metrics": dict(best.get("metrics") or {}),
                "multi": getattr(eng, "_multi", 1),
                "workers": len(getattr(getattr(eng, "pool", None), "_procs", {}) or {}),
            }
            if snap:
                row["spec_revision"] = snap.get("spec_revision")
                row["autofix"] = snap.get("autofix")
                row["valid_rate"] = snap.get("valid_rate")
                row["fuzzy_top_n"] = snap.get("fuzzy_top_n")
            out.append(row)
        return out

    def _orch(self):
        """Engine orchestrator when a project runs, else a bare one —
        both read/write the same config.json registry."""
        if self.engine is not None:
            return self.engine.orchestrator
        if self._base_orchestrator is None:
            from .llm import ModelOrchestrator
            self._base_orchestrator = ModelOrchestrator(self.cfg)
        return self._base_orchestrator

    def _swarm_coord(self):
        """Swarm coordinator, created on first use (orchestrator-backed)."""
        if self._swarm is None:
            from .swarm import SwarmCoordinator
            self._swarm = SwarmCoordinator(
                self._orch(),
                project_getter=lambda pid: self._registry_for(pid)[0],
            )
        return self._swarm

    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    def _setup_routes(self) -> None:
        if web is None:
            raise RuntimeError("aiohttp is required for the dashboard server")
        r = self.app.router
        r.add_get("/", self._index)
        r.add_delete("/api/projects/{pid}", self._api_project_delete)
        r.add_get("/api/projects", self._api_projects_list)
        r.add_get("/api/suggest/status", self._api_suggest_status)
        r.add_post("/api/engine/autofix", self._api_engine_autofix)
        r.add_post("/api/projects", self._api_projects_create)
        r.add_post("/api/projects/suggest", self._api_projects_suggest)
        r.add_post("/kai", self._api_kai)
        r.add_get("/api/projects/{pid}/spec", self._api_project_spec)
        r.add_put("/api/projects/{pid}/spec", self._api_project_spec_update)
        r.add_get("/api/projects/{pid}/best", self._api_project_best)
        r.add_post("/api/projects/{pid}/smoke", self._api_project_smoke)
        r.add_post("/api/projects/{pid}/score", self._api_project_score)
        r.add_post("/api/projects/{pid}/pipeline-suggest", self._api_project_pipeline_suggest)
        r.add_post("/api/swarm/start", self._api_swarm_start)
        r.add_get("/api/swarm", self._api_swarm_list)
        r.add_get("/api/swarm/{job_id}", self._api_swarm_status)
        r.add_post("/api/swarm/{job_id}/cancel", self._api_swarm_cancel)
        r.add_post("/api/projects/{pid}/agent/start", self._api_project_agent_start)
        r.add_get("/api/agent/status", self._api_agent_status)
        r.add_post("/api/agent/cancel", self._api_agent_cancel)
        r.add_post("/api/snapshots", self._api_snapshots_take)
        r.add_get("/api/snapshots", self._api_snapshots_list)
        r.add_post("/api/snapshots/restore", self._api_snapshots_restore)
        r.add_get("/api/prefs", self._api_prefs_get)
        r.add_put("/api/prefs", self._api_prefs_set)
        r.add_post("/api/prefs/reset", self._api_prefs_reset)
        r.add_post("/api/config-agent", self._api_config_agent)
        r.add_get("/api/projects/{pid}/activate", self._api_project_activate)
        r.add_post("/api/engine/switch", self._api_engine_switch)
        r.add_post("/api/engine/start", self._api_engine_start)
        r.add_post("/api/engine/stop", self._api_engine_stop)
        r.add_post("/api/engine/pause", self._api_engine_pause)
        r.add_post("/api/engine/fuzzy", self._api_engine_fuzzy)
        r.add_get("/api/active", self._api_active)
        r.add_post("/api/active/custom_code", self._api_custom_code)
        # ── Legacy-shape endpoints (the original dashboard UI speaks these) ──
        r.add_get("/api/workers", self._api_workers_legacy)
        r.add_get("/api/state", self._api_state_legacy)
        r.add_get("/api/llm/status", self._api_llm_status_legacy)
        r.add_get("/api/llm/modelstats", self._api_modelstats)
        r.add_get("/api/llm/live", self._api_llm_live_legacy)
        r.add_get("/api/debug/logs", self._api_debug_logs_legacy)
        r.add_get("/api/iterations", self._api_iterations_legacy)
        r.add_post("/api/workers/{wid}/kill", self._api_worker_kill_legacy)
        r.add_post("/api/workers/{wid}/kill-process", self._api_worker_kill_process_legacy)
        r.add_post("/api/engine/multi", self._api_engine_multi)
        r.add_get("/open_folder/{path:.*}", self._api_open_folder)
        r.add_get("/api/model/status", self._api_model_status_legacy)
        r.add_get("/api/models", self._api_models_legacy)
        r.add_get("/api/prompts", self._api_prompts_legacy)
        r.add_post("/api/queue/custom_code", self._api_custom_code)
        r.add_post("/api/config", self._api_config_post_legacy)
        r.add_post("/api/override/llm_pause", self._api_llm_pause_legacy)
        r.add_post("/api/llm/stop", self._api_llm_stop_legacy)
        r.add_post("/api/llm/resume", self._api_llm_resume_legacy)
        r.add_get("/api/override/status", self._api_override_status_legacy)
        r.add_post("/api/override/model/toggle", self._api_override_model_toggle_legacy)
        r.add_post("/api/override/model", self._api_override_model_legacy)
        r.add_delete("/api/override/model", self._api_override_model_clear_legacy)
        r.add_post("/api/override/prompt/toggle", self._api_override_prompt_toggle_legacy)
        r.add_post("/api/override/prompt", self._api_override_prompt_legacy)
        r.add_delete("/api/override/prompt", self._api_override_prompt_clear_legacy)
        r.add_post("/api/workers/add", self._api_worker_add)
        r.add_post("/api/workers/remove/{wid}", self._api_worker_remove)
        r.add_post("/api/workers/kill/{wid}", self._api_worker_kill)
        r.add_post("/api/servers/add", self._api_server_add)
        r.add_post("/api/servers/remove/{sid}", self._api_server_remove)
        r.add_post("/api/servers/active", self._api_server_active)
        r.add_post("/api/servers/health/{sid}", self._api_server_health)
        r.add_post("/api/servers/label", self._api_server_label)
        r.add_post("/api/onboarding/complete", self._api_onboarding_complete)
        r.add_post("/api/onboarding/demo", self._api_onboarding_demo)
        r.add_get("/api/config", self._api_config_get)
        r.add_put("/api/config", self._api_config_put)
        r.add_get("/api/guardrails", self._api_guardrails)
        r.add_get("/api/autofix", self._api_autofix_get)
        r.add_post("/api/autofix", self._api_autofix_set)
        r.add_post("/api/autofix/apply", self._api_autofix_apply)
        r.add_get("/api/notes", self._api_notes_get)
        r.add_post("/api/notes", self._api_notes_create)
        r.add_put("/api/notes/{nid}", self._api_notes_update)
        r.add_delete("/api/notes/{nid}", self._api_notes_delete)
        r.add_post("/api/notes/reorder", self._api_notes_reorder)
        r.add_post("/api/notes/colors", self._api_notes_colors)
        r.add_post("/api/notes/check-similarity", self._api_notes_similarity)
        r.add_post("/api/notes/{nid}/comment", self._api_notes_comment_add)
        r.add_post("/api/notes/{nid}/comment/delete", self._api_notes_comment_delete)
        r.add_post("/api/notes/{nid}/archive", self._api_notes_archive)
        r.add_get("/api/system", self._api_system)
        pages = Path(__file__).resolve().parent.parent / "pages"
        # The original dashboard references assets at root paths. The GUI
        # evolves constantly — never let browsers serve stale HTML/JS/CSS.
        def _nocache(path):
            def handler(request):
                resp = web.FileResponse(path)
                resp.headers["Cache-Control"] = "no-store"
                return resp
            return handler
        r.add_get("/style.css", _nocache(pages / "style.css"))
        r.add_get("/script.js", _nocache(pages / "script.js"))
        r.add_static("/svg", str(pages / "svg"))
        if (pages / "fonts").is_dir():
            r.add_static("/fonts", str(pages / "fonts"))

    async def _index(self, request):
        pages = Path(__file__).resolve().parent.parent / "pages"
        html_path = pages / "dashboard.html"
        if not html_path.exists():
            return web.Response(text="dashboard.html missing", status=404)
        resp = web.FileResponse(html_path)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    # projects
    # ------------------------------------------------------------------ #
    async def _api_projects_list(self, request):
        active_id = self.engine.project.id if self.engine else None
        temp = [{**p, "temp": True} for p in self._temp_registry.list()]
        return _json({
            "projects": self.registry.list() + temp,
            "active_id": active_id,
        })

    async def _api_project_delete(self, request):
        pid = request.match_info["pid"]
        eng = self.engines.get(pid)
        if eng is not None:
            if eng.engine_state not in (STATE_STOPPED, STATE_STOPPING):
                return _json({"ok": False, "error": "project is running — stop the engine first"}, 409)
            eng.stop()
            self.engines.pop(pid, None)
            if self._selected_project_id == pid:
                self._selected_project_id = None
        try:
            _p, reg = self._registry_for(pid)
            reg.delete(pid)
        except Exception as e:
            return _json({"ok": False, "error": str(e)}, 400)
        reg.scan()
        return _json({"ok": True})

    async def _api_projects_create(self, request):
        data = await request.json()
        pid = str(data.get("id", "")).strip().lower()
        spec = data.get("spec") or {}
        temp = bool(data.get("temp"))
        out = self._create_project(pid, spec, temp=temp)
        return _json(out, 400 if not out.get("ok") else 200)

    def _create_project(self, pid: str, spec: Dict[str, Any], temp: bool = False) -> Dict[str, Any]:
        """Shared project creation: bake defaults, guardrail-scan, write
        spec + bundled files. Returns {ok, project?, warnings?, error?}.
        temp=True writes the project under the TEMP ROOT (temp/) instead
        of projects/ — the real setup is never touched by temp runs."""
        spec = dict(spec)
        # Bake the framework default (autofix on/off) into NEW projects;
        # existing projects keep their own explicit setting.
        spec.setdefault("skills", {}).setdefault("autofix_build", self.cfg.autofix_build_enabled)
        # Suggested specs carry `files` (harness scripts + baseline) — write
        # them out, don't persist them inside project.json.
        files = dict(spec.pop("files", {}) or {})
        bad = self._scan_spec_commands(spec)
        if bad:
            return {"ok": False, "error": f"guardrail blocked pipeline: {bad}"}
        reg = self._temp_registry if temp else self.registry
        try:
            p = reg.create(pid, spec)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        warnings: List[str] = self._write_spec_files(p, files, spec)
        # The user's data file (original, immutable): written from the
        # suggest-time attachment, declared protected in the spec.
        data_file = self._pending_data_file
        self._pending_data_file = None
        prot = (spec.get("data") or {}).get("protected_files", []) or []
        if data_file and prot:
            rel = prot[0]
            safe, why = _safe_rel_path(rel)
            if not safe:
                warnings.append(f"skipped data file: {why}")
            else:
                target = p.path / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(str(data_file.get("content_b64", ""))))
        elif prot:
            warnings.append(f"protected file '{prot[0]}' declared but no data file attached — pipeline steps reading it will fail")
        reg.scan()
        p = reg.require(pid)
        if warnings:
            return {"ok": True, "project": {"id": p.id, "name": p.name}, "warnings": warnings}
        return {"ok": True, "project": {"id": p.id, "name": p.name}}

    async def _api_onboarding_complete(self, request):
        """Mark the first-run wizard as done (persisted in config.json)."""
        self.cfg.data.setdefault("onboarding", {})["done"] = True
        self.cfg.save()
        return _json({"ok": True})

    async def _api_onboarding_demo(self, request):
        """One-click demo project (bundled template): prime counter with a
        full build → verify → score harness. Gives first-time users an
        instant, real evolution loop."""
        from .config import FRAMEWORK_ROOT
        tpl = FRAMEWORK_ROOT / "kaisen" / "templates" / "demo_prime"
        if not tpl.is_dir():
            return _json({"ok": False, "error": "demo template missing from the install"}, 500)
        spec = load_json(tpl / "project.json", {})
        if not spec:
            return _json({"ok": False, "error": "demo template project.json unreadable"}, 500)
        files: Dict[str, str] = {}
        harness = tpl / "harness"
        if harness.is_dir():
            for py in sorted(harness.glob("*.py")):
                files[f"harness/{py.name}"] = py.read_text(encoding="utf-8")
        baseline = tpl / "original.c"
        if baseline.is_file():
            files["original.c"] = baseline.read_text(encoding="utf-8")
        spec["files"] = files
        out = self._create_project("demo-prime", spec)
        return _json(out, 400 if not out.get("ok") else 200)

    async def _api_projects_suggest(self, request):
        """Agent-assisted project creation: a goal (and optionally a
        program/data). With no program, the AI writes the baseline itself.
        The local LLM proposes the whole pipeline; the framework validates
        it (structure, guardrails, lint, real smoke run) and retries the
        LLM with error feedback until it passes."""
        data = await request.json()
        goal = str(data.get("goal", ""))
        code = str(data.get("code", ""))
        data_file = data.get("data_file")
        # Language: explicit field wins; then the program filename; then
        # the words of the goal ("in c++", "a rust program"...).
        from .languages import lang_from_ext, lang_from_goal, normalize_lang
        language = normalize_lang(data.get("language") or "")
        if not data.get("language"):
            detected = lang_from_ext(Path(str(data.get("code_file", ""))).suffix or None)
            if not detected:
                detected = lang_from_goal(goal)
            if detected:
                language = detected
        if not goal:
            return _json({"ok": False, "error": "a goal is required — describe what the program must do (no program needed: the AI writes it)"}, 400)

        with self._suggest_lock:
            if self._suggest_state.get("running"):
                return _json({"ok": False, "error": "a suggestion is already running"}, 409)
        if data_file:
            from .suggest import MAX_DATA_B64
            name = str(data_file.get("name", ""))
            content_b64 = str(data_file.get("content_b64", ""))
            if not name or ".." in name or "/" in name or "\\" in name:
                return _json({"ok": False, "error": "unsafe data file name"}, 400)
            if len(content_b64) > MAX_DATA_B64:
                return _json({"ok": False, "error": "data file too large"}, 400)
            try:
                base64.b64decode(content_b64, validate=True)
            except Exception:
                return _json({"ok": False, "error": "invalid data file encoding"}, 400)
            data_file = {"name": name, "content_b64": content_b64}
        result = await self._run_suggest(goal, code, data_file, language, server_ids=data.get("server_ids"))
        if result.get("ok"):
            self._pending_data_file = data_file
            return _json({
                "ok": True,
                "suggested_spec": result["suggested_spec"],
                "validation": {"rounds": result["rounds"], "ok": True, "notes": result.get("notes", [])},
            })
        return _json({"ok": False, "error": result.get("error", "suggest failed")}, result.get("status", 502))

    async def _api_kai(self, request):
        """KAI protocol — the LLM-facing API.

        Plain text in, plain text out. The body carries one command per
        line (CANDIDATE blocks end with END); every reply starts with OK
        or ERR. See kaisen/kai.py for the command reference.

        PROJECT is sticky across requests via the `kaisen_kai_sid` cookie
        (curl -c/-b keeps it; a plain curl without a cookie starts fresh),
        so PROJECT <id> no longer has to ride in every request body.
        """
        import uuid as _uuid
        from . import kai
        text = await request.text()
        if not text.strip():
            return web.Response(text="ERR empty body — HELP for the reference\n",
                                content_type="text/plain", charset="utf-8")
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        sid = request.cookies.get("kaisen_kai_sid")
        if not sid:
            sid = _uuid.uuid4().hex[:16]
        with self._kai_lock:
            entry = self._kai_sessions.get(sid)
            if entry is None or entry.get("created", 0) < time.time() - 86400 * 7:
                entry = {
                    "session": kai.KaiSession(kai.KaiClient(f"http://{host}:{self.port}")),
                    "created": time.time(),
                }
                self._kai_sessions[sid] = entry
            session = entry["session"]
        try:
            out = await asyncio.to_thread(
                kai.handle_text, text, host=host, port=self.port, auto_start=False, session=session
            )
        except Exception as e:
            out = f"ERR {e}"
        resp = web.Response(text=out + "\n", content_type="text/plain", charset="utf-8")
        resp.set_cookie("kaisen_kai_sid", sid, max_age=86400 * 7, samesite="Lax")
        return resp


    async def _run_suggest(self, goal: str, code: str, data_file, language: str, server_ids=None) -> Dict[str, Any]:
        """Shared suggest loop: own orchestrator, live progress state, then
        the guarded suggest_project loop. Returns the raw result dict."""
        def _on_progress(kw: Dict[str, Any]) -> None:
            with self._suggest_lock:
                for key in ("stage", "round", "raw_label", "ok"):
                    if key in kw:
                        self._suggest_state[key] = kw[key]
                if kw.get("raw"):
                    self._suggest_state["raw"] = str(kw["raw"])[-8000:]
                    self._suggest_state["tokens"] = ""
                if kw.get("error"):
                    self._suggest_state["notes"].append(str(kw["error"])[-300:])
                # Stepwise progress: one row per pipeline step, clickable.
                steps = self._suggest_state.setdefault("steps", [])
                if "step" in kw:
                    entry = next((s for s in steps if s.get("id") == str(kw["step"])), None)
                    if entry is None:
                        entry = {"id": str(kw["step"])}
                        steps.append(entry)
                    entry["state"] = str(kw.get("state", ""))
                    for key in ("label", "output", "error", "attempt"):
                        if kw.get(key) is not None:
                            entry[key] = kw[key]
                if kw.get("stage") in ("smoke", "repair"):
                    entry = next((s for s in steps if s.get("id") == "validation"), None)
                    if entry is None:
                        entry = {"id": "validation", "label": "Validate — smoke run"}
                        steps.append(entry)
                    entry["state"] = "running"
                    if kw.get("raw_label"):
                        entry["label"] = str(kw["raw_label"])
                if kw.get("stage") == "done":
                    entry = next((s for s in steps if s.get("id") == "validation"), None)
                    if entry is not None:
                        entry["state"] = "done"
                self._suggest_state["elapsed"] = round(time.time() - t0, 1)
        from .llm import ModelOrchestrator
        suggest_orch = ModelOrchestrator(self.cfg)
        if isinstance(server_ids, list) and server_ids:
            # Per-job server choice: must NOT leak into config.json's
            # global active_ids (set_active persists by default).
            suggest_orch.set_active([str(i) for i in server_ids if str(i) in suggest_orch._servers], persist=False)
        active = list(suggest_orch._active_ids)
        from .guardrails import _scan_denylist
        bad, reason = _scan_denylist(code)
        if bad:
            return {"ok": False, "error": f"input code blocked: {reason}"}
        t0 = time.time()
        with self._suggest_lock:
            self._suggest_state = {
                "running": True, "round": 1, "max_rounds": 8,
                "stage": "starting", "tokens": "", "raw": "", "raw_label": "",
                "notes": [], "server_ids": active, "steps": [], "elapsed": 0.0, "ok": None, "_t0": t0,
            }


        def _sink(token: str, n: int = 1) -> None:
            self._suggest_state["tokens"] = (self._suggest_state.get("tokens", "") + token)[-8000:]
            self._suggest_state["elapsed"] = round(time.time() - t0, 1)



        last_sid = {"v": None}

        def _request_with_progress(prompt) -> str:
            if isinstance(prompt, list):
                text, sid = suggest_orch.request_chat_stream(
                    prompt, on_token=_sink, skill="suggest")
            else:
                text, sid = suggest_orch.request_stream(
                    prompt, on_token=_sink, skill="suggest")
            last_sid["v"] = sid
            return text

        from .suggest import suggest_project
        try:
            result = await asyncio.to_thread(
                suggest_project,
                _request_with_progress,
                goal, code, data_file,
                on_progress=_on_progress,
                language=language,
            )
        except Exception as e:
            with self._suggest_lock:
                self._suggest_state["running"] = False
                self._suggest_state["stage"] = "failed"
                self._suggest_state["elapsed"] = round(time.time() - t0, 1)
                self._suggest_state["notes"].append(str(e)[-300:])
            return {"ok": False, "error": str(e)}
        with self._suggest_lock:
            self._suggest_state["running"] = False
            self._suggest_state["stage"] = "done" if result.get("ok") else "failed"
            self._suggest_state["ok"] = bool(result.get("ok"))
            self._suggest_state["elapsed"] = round(time.time() - t0, 1)
        # Scoreboard: a suggest run that validated WITHOUT repair rounds
        had_repairs = any(str(s.get("id")) == "repair" for s in (result.get("steps") or []))
        if last_sid["v"]:
            if result.get("ok") and not had_repairs:
                suggest_orch.record_outcome(last_sid["v"], "suggest", "oneshot")
            if result.get("ok"):
                suggest_orch.record_outcome(last_sid["v"], "suggest", "win")
        return result

    async def _api_project_score(self, request):
        """Score an arbitrary source file through the project's full
        build+verify+score pipeline — no engine, no LLM run, no ceremony.
        Writes an audit copy + result.json under runs/score_<id>/ and
        returns the metrics.  Works for temp projects too (resolves via the
        project's own root)."""
        pid = request.match_info["pid"]
        data = await request.json()
        path = str(data.get("path", "") or "").strip()
        if not path:
            return _json({"ok": False, "error": "path is required (the source file to score)"}, 400)
        try:
            p = self._registry_for(pid)[0]
        except KeyError:
            return _json({"error": "project not found"}, 404)
        cand = Path(path)
        if not cand.is_absolute():
            cand = (p.path / path).resolve()
        if not cand.is_file():
            return _json({"ok": False, "error": f"file not found: {cand}"}, 404)
        import shutil
        import uuid
        from .pipeline import run_pipeline
        from .languages import ext_from_lang
        workdir = p.path / "runs" / f"score_{uuid.uuid4().hex[:8]}"
        workdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cand, workdir / f"scored_source{ext_from_lang(p.spec.get('language', 'c'))}")
        result = await asyncio.to_thread(run_pipeline, p, cand, workdir)
        save_json(workdir / "result.json", {
            "path": str(cand), "ok": bool(result.get("ok")),
            "stage": result.get("stage"), "outcome": result.get("outcome"),
            "metrics": result.get("metrics"), "timings": result.get("timings"),
        })
        if result.get("ok"):
            return _json({"ok": True, "metrics": result.get("metrics"),
                          "timings": result.get("timings"),
                          "stage": "done", "gen_dir": str(workdir),
                          "score_details": result.get("score_details")})
        return _json({"ok": False, "error": result.get("reason") or "score failed",
                      "stage": result.get("stage"), "outcome": result.get("outcome"),
                      "metrics": result.get("metrics"), "gen_dir": str(workdir)}, 400)

    async def _api_project_pipeline_suggest(self, request):
        """'AI, build my pipeline': suggest a full pipeline for an existing
        project from its baseline + goal. The user validates, then applies."""
        pid = request.match_info["pid"]
        try:
            p = self._registry_for(pid)[0]
        except KeyError:
            return _json({"ok": False, "error": "project not found"}, 404)
        spec = p.spec
        base_name = str((spec.get("data") or {}).get("baseline_source", "") or "")
        baseline = p.path / base_name if base_name else None
        if not baseline or not baseline.is_file():
            return _json({"ok": False, "error": "baseline file missing — set data.baseline_source"}, 400)
        code = baseline.read_text(encoding="utf-8")
        goal = str((spec.get("prompts") or {}).get("goal", "") or spec.get("description", "") or "Improve the program.")
        language = str(spec.get("language", "c"))
        result = await self._run_suggest(goal, code, None, language)
        if result.get("ok"):
            return _json({
                "ok": True,
                "suggested_spec": result["suggested_spec"],
                "validation": {"rounds": result["rounds"], "ok": True, "notes": result.get("notes", [])},
            })
        return _json({"ok": False, "error": result.get("error", "suggest failed")}, result.get("status", 502))

    # ------------------------------------------------------------------ #
    # swarm
    # ------------------------------------------------------------------ #
    async def _api_swarm_start(self, request):
        data = await request.json()
        kind = str(data.get("kind", "answer"))
        req_text = str(data.get("request", ""))
        project_id = data.get("project_id")
        if kind in ("code_forge", "pipeline") and not project_id:
            if self.engine is not None:
                project_id = self.engine.project.id
            else:
                return _json({"ok": False, "error": "project_id required (no engine running)"}, 400)
        try:
            n = int(data.get("n", 3))
            max_concurrent = int(data.get("max_concurrent", 4))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "n/max_concurrent must be numbers"}, 400)
        job = await asyncio.to_thread(
            self._swarm_coord().start,
            kind, req_text, project_id, n, max_concurrent,
            min_tier=str(data.get("min_tier", "tiny")),
        )
        return _json({"ok": True, "job_id": job.id})

    async def _api_swarm_list(self, request):
        return _json({"jobs": self._swarm_coord().list_jobs()})

    async def _api_swarm_status(self, request):
        job = self._swarm_coord().get(request.match_info["job_id"])
        if job is None:
            return _json({"error": "job not found"}, 404)
        return _json({"job": job.snapshot()})

    async def _api_swarm_cancel(self, request):
        ok = self._swarm_coord().cancel(request.match_info["job_id"])
        return _json({"ok": ok}, 404 if not ok else 200)

    async def _api_suggest_status(self, request):
        with self._suggest_lock:
            st = dict(self._suggest_state)
            if st.get("running") and st.get("_t0"):
                st["elapsed"] = round(time.time() - st["_t0"], 1)
            return _json(st)

    async def _api_project_best(self, request):
        """Champion source + metrics for a project — real OR temp.  Temp
        projects live under temp/ (not projects/), so the champion is read
        from the project's own state.json + baseline, never a hardcoded
        path.  This is what KAI `BEST` uses, and what an agent can poll to
        fish a temp run's current best while it evolves."""
        pid = request.match_info["pid"]
        try:
            p = self._registry_for(pid)[0]
        except KeyError:
            return _json({"error": "project not found"}, 404)
        state_file = p.path / "state.json"
        best = None
        if state_file.is_file():
            try:
                best = (load_json(state_file, {}) or {}).get("best")
            except Exception:
                best = None
        code_path = None
        metrics, generation = {}, None
        if best and best.get("code_path") and Path(best["code_path"]).is_file():
            code_path = Path(best["code_path"])
            metrics, generation = best.get("metrics") or {}, best.get("generation")
        if not code_path:
            base = str((p.spec.get("data") or {}).get("baseline_source", "") or "")
            fallback = p.path / base if base else None
            if fallback and fallback.is_file():
                code_path, metrics, generation = fallback, {}, None
        if not code_path:
            return _json({"ok": False, "error": f"no champion or baseline source for '{pid}' — run the engine first"}, 404)
        try:
            code = code_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return _json({"ok": False, "error": f"cannot read source: {e}"}, 500)
        return _json({
            "ok": True,
            "project_id": pid,
            "language": p.spec.get("language", "c"),
            "source_path": str(code_path),
            "generation": generation,
            "metrics": metrics,
            "code": code[:40000],
        })

    async def _api_project_spec(self, request):
        pid = request.match_info["pid"]
        try:
            p = self._registry_for(pid)[0]
        except KeyError:
            return _json({"error": "project not found"}, 404)
        return _json({"spec": p.spec})

    async def _api_project_spec_update(self, request):
        pid = request.match_info["pid"]
        data = await request.json()
        spec = data.get("spec") or {}
        files = dict(spec.pop("files", {}) or {})
        bad = self._scan_spec_commands(spec)
        if bad:
            return _json({"ok": False, "error": f"guardrail blocked pipeline: {bad}"}, 400)
        try:
            p = self.registry.update_spec(pid, spec)
            warnings = self._write_spec_files(p, files, spec) if files else []
            return _json({"ok": True, "spec": p.spec, "warnings": warnings})
        except ValueError as e:
            return _json({"ok": False, "error": str(e)}, 400)

    @staticmethod
    def _write_spec_files(p, files: Dict[str, str], spec: Dict[str, Any]) -> List[str]:
        """Write harness/baseline files carried by a suggested spec (with
        path + content guardrails); never persisted inside project.json."""
        from .suggest import _safe_rel_path, _scan_script_content, ensure_harness_ready
        warnings: List[str] = []
        for path, content in files.items():
            safe, why = _safe_rel_path(path)
            if not safe:
                warnings.append(f"skipped {path}: {why}")
                continue
            if path.endswith(".py"):
                warnings.extend(_scan_script_content(path, content))
            target = p.path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        ensure_harness_ready(p.path, {**spec, "files": files})
        return warnings

    async def _api_project_activate(self, request):
        pid = request.match_info["pid"]
        try:
            p = self._registry_for(pid)[0]
        except KeyError:
            return _json({"error": "project not found"}, 404)
        return _json({"ok": True, "active_id": p.id})

    def _scan_spec_commands(self, spec: Dict[str, Any]) -> str:
        """Check every pipeline command in a spec against guardrails,
        resolving relative program paths against the project directory
        (matching what the pipeline does at execution time)."""
        from pathlib import Path
        proj_dir = Path(spec.get("dir") or (PROJECTS_DIR / str(spec.get("id", ""))))
        for stage in ("build", "verify", "score"):
            steps = spec.get("steps", {}).get(stage, [])
            if stage == "build":
                steps = [steps] if steps else []
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                inline = step.get("inline")
                if isinstance(inline, dict) and str(inline.get("code", "")).strip():
                    # Inline scripts: scan the content with the RIGHT rule
                    # set for its language (python vs shell hatches).
                    from .suggest import _scan_script_content
                    ilang = str(inline.get("lang", "python")).lower()
                    errs = _scan_script_content(f"inline-{stage}{i}.{ilang[:2]}", str(inline["code"]), lang=ilang)
                    if errs:
                        return f"steps.{stage}[{i}]: " + errs[0]
                    continue
                prog = str(step.get("program", ""))
                if not prog:
                    return f"steps.{stage}[{i}]: program or inline script required"
                p = Path(prog)
                if not p.is_absolute():
                    p = (proj_dir / p).resolve()
                args = [str(a) for a in step.get("args", [])]
                ok, reason = check_command([str(p)] + args, project=spec, project_dir=proj_dir)
                if not ok:
                    return f"steps.{stage}[{i}]: {reason}"
        return ""

    async def _api_project_smoke(self, request):
        """Run the project's full pipeline on its baseline, in a temp copy
        (nothing touches the real project) — the "Test pipeline" button."""
        pid = request.match_info["pid"]
        try:
            p = self._registry_for(pid)[0]
        except KeyError:
            return _json({"ok": False, "error": "project not found"}, 404)
        return _json(self._smoke_project(p))

    def _smoke_project(self, p) -> Dict[str, Any]:
        """Shared smoke runner (endpoint + agent + config agent)."""
        import shutil as _shutil
        import tempfile as _tempfile
        from pathlib import Path as _Path
        from .pipeline import run_pipeline
        tmp = _Path(_tempfile.mkdtemp(prefix="kaisen-smoke-"))
        try:
            def _ignore(d, names):
                return {n for n in names if n in ("runs", "best", "state.json", "results.csv", "seen_hashes.json", ".kaisen_scripts")}
            _shutil.copytree(p.path, tmp / p.id, ignore=_ignore)
            from .projects import Project as _Project
            from .languages import ext_from_lang as _ext_from_lang
            tp = _Project(tmp / p.id)
            lang = str(tp.spec.get("language", "c"))
            baseline = tp.path / str((tp.spec.get("data") or {}).get("baseline_source", f"original{_ext_from_lang(lang)}"))
            if not baseline.exists():
                return {"ok": False, "error": f"baseline file not found: {baseline.name}"}
            res = run_pipeline(tp, baseline, tmp / p.id / "smoke")
            out = {
                "ok": bool(res.get("ok")),
                "stage": res.get("stage"),
                "outcome": res.get("outcome"),
                "reason": (res.get("reason") or "")[:1500],
                "metrics": res.get("metrics"),
                "stdout_tail": (res.get("stdout_tail") or "")[:1500],
                "stderr_tail": (res.get("stderr_tail") or "")[:1500],
            }
            # Persist the outcome independent of the HTTP response — a smoke
            # that outlives the client read timeout must still land somewhere
            # the operator can read later (project dir, capped history).
            try:
                rows = load_json(p.path / "smoke_results.json", []) or []
                rows.append({
                    "time": time.time(),
                    "project_id": p.id,
                    "ok": out["ok"],
                    "stage": out["stage"],
                    "outcome": out["outcome"],
                    "metrics": res.get("metrics"),
                    "timings": res.get("timings"),
                })
                save_json(p.path / "smoke_results.json", rows[-50:])
            except Exception:
                pass
            print(f"[KAISEN] smoke {p.id}: "
                  f"{'PASS' if out['ok'] else 'FAIL'} stage={out['stage']} "
                  f"metrics={res.get('metrics')}")
            return out
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            _shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # project agent
    # ------------------------------------------------------------------ #
    def _agent_tools(self, project) -> Dict[str, Any]:
        from . import snapshots
        from .agent import deep_merge
        from .languages import ext_from_lang

        def read_spec(args):
            s = project.spec
            compact = {
                "id": s.get("id"), "name": s.get("name"), "language": s.get("language"),
                "artifact_name": s.get("artifact_name"), "metrics": s.get("metrics"),
                "prompts": s.get("prompts"), "data": s.get("data"),
                "steps_summary": {
                    stage: [{
                        "program": st.get("program"),
                        "inline_lang": (st.get("inline") or {}).get("lang"),
                        "inline_head": ((st.get("inline") or {}).get("code") or "")[:500],
                        "args": st.get("args"),
                        "parse": st.get("parse"),
                    } for st in (steps if isinstance(steps, list) else [steps])]
                    for stage, steps in (s.get("steps") or {}).items()
                },
            }
            return json.dumps(compact, indent=1)

        def read_history(args):
            n = min(50, int(args.get("n", 10)))
            from .state import ProjectState
            st = ProjectState(project)
            hist = st.data.get("history", [])[-n:]
            return "\n".join(
                f"gen {h.get('generation')}: {h.get('outcome')} — {(h.get('detail') or '')[:140]}"
                for h in hist
            ) or "(no history yet)"

        def read_champion(args):
            path = project.best_dir / f"program{ext_from_lang(project.spec.get('language', 'c'))}"
            if not path.exists():
                base = project.path / str((project.spec.get("data") or {}).get("baseline_source", ""))
                path = base if base.exists() else None
            if not path or not path.exists():
                return "(no champion/baseline)"
            return path.read_text(encoding="utf-8")[:8000]

        def read_file(args):
            path = str(args.get("path", "")).strip()
            if not path or ".." in path or path.startswith("/"):
                return "ERROR: path must be relative inside the project"
            p = (project.path / path).resolve()
            if not str(p).startswith(str(project.path.resolve())):
                return "ERROR: path escapes the project"
            if not p.is_file():
                return f"ERROR: no such file: {path}"
            if p.stat().st_size > 8192:
                return f"ERROR: file too large to read ({p.stat().st_size} bytes > 8192)"
            return p.read_text(encoding="utf-8", errors="replace")

        def read_lesson(args):
            p = project.path / "lessons.txt"
            return p.read_text(encoding="utf-8")[:2000] if p.exists() else "(no lesson yet)"

        def run_smoke(args):
            res = self._smoke_project(project)
            return json.dumps({
                "ok": res.get("ok"), "stage": res.get("stage"),
                "reason": (res.get("reason") or "")[:300], "metrics": res.get("metrics"),
            })

        def update_spec(args):
            changes = args.get("changes")
            if not isinstance(changes, dict):
                return "ERROR: changes must be an object"
            import copy as _copy
            new_spec = _copy.deepcopy(project.spec)
            new_spec.pop("dir", None)
            deep_merge(new_spec, changes)
            new_spec["id"] = project.id
            bad = self._scan_spec_commands(new_spec)
            if bad:
                return f"REJECTED by guardrails: {bad}"
            snapshots.take_project_snapshot(project.path, "project agent update_spec")
            try:
                self.registry.update_spec(project.id, new_spec)
                self.registry.scan()
            except ValueError as e:
                return f"REJECTED: {e}"
            return "spec updated OK (snapshot taken)"

        return {
            "read_spec": read_spec,
            "read_history": read_history,
            "read_champion": read_champion,
            "read_lesson": read_lesson,
            "run_smoke": run_smoke,
            "update_spec": update_spec,
            "read_file": read_file,
        }

    async def _api_project_agent_start(self, request):
        pid = request.match_info["pid"]
        try:
            project = self._registry_for(pid)[0]
        except KeyError:
            return _json({"ok": False, "error": "project not found"}, 404)
        with self._suggest_lock:
            if self._agent_state.get("running"):
                return _json({"ok": False, "error": "an agent is already running"}, 409)
        data = await request.json() if request.can_read_body else {}
        mission = str(data.get("mission", ""))
        from . import snapshots
        snapshots.take_project_snapshot(project.path, f"agent mission: {mission[:80] or 'default'}")
        self._agent_state = {"running": True, "project_id": pid, "turns": [], "summary": "", "tokens": "", "started": time.time()}
        self._agent_cancel = threading.Event()
        thread = threading.Thread(target=self._run_agent, args=(project, mission), daemon=True)
        thread.start()
        return _json({"ok": True, "pid": pid})

    def _run_agent(self, project, mission: str) -> None:
        from . import promptlib
        from .agent import ProjectAgent
        orch = self._orch()
        tier = promptlib.detect_tier(orch.active_config)
        goal = str((project.spec.get("prompts") or {}).get("goal", "") or "Improve the project.")

        def sink(token: str, n: int = 1) -> None:
            self._agent_state["tokens"] = (self._agent_state.get("tokens", "") + token)[-6000:]

        def req(prompt: str) -> str:
            text, sid = orch.request_stream(prompt, on_token=sink,
                                            cancel_event=self._agent_cancel,
                                            skill="agent")
            self._agent_state["_sid"] = sid
            return text

        agent = ProjectAgent(
            request=req, tier=tier,
            project_name=str(project.spec.get("name", project.id)),
            language=str(project.spec.get("language", "c")),
            goal=goal, tools=self._agent_tools(project),
            mission=mission, cancel=self._agent_cancel,
        )
        summary = agent.run()
        self._agent_state["turns"] = agent.turns
        self._agent_state["summary"] = summary
        self._agent_state["running"] = False
        # Scoreboard: a completed (non-failed) mission is a one-shot win
        # for the model that drove it.
        sid = self._agent_state.get("_sid")
        if sid and not summary.startswith(("cancelled", "agent request failed",
                                           "agent produced no valid actions",
                                           "agent hit the turn limit")):
            orch.record_outcome(sid, "agent", "oneshot")
        self.registry.scan()

    async def _api_agent_status(self, request):
        st = dict(self._agent_state)
        st["elapsed"] = round(time.time() - st.get("started", time.time()), 1) if st.get("started") else 0
        return _json(st)

    async def _api_agent_cancel(self, request):
        self._agent_cancel.set()
        return _json({"ok": True})

    # ------------------------------------------------------------------ #
    # snapshots + UI prefs
    # ------------------------------------------------------------------ #
    async def _api_snapshots_take(self, request):
        from . import snapshots
        data = await request.json() if request.can_read_body else {}
        reason = str(data.get("reason", "manual"))[:120]
        pid = data.get("project_id")
        if pid:
            try:
                p = self._registry_for(pid)[0]
            except KeyError:
                return _json({"ok": False, "error": "project not found"}, 404)
            snap_id = snapshots.take_project_snapshot(p.path, reason)
            return _json({"ok": True, "id": snap_id, "kind": "project"})
        snap_id = snapshots.take_config_snapshot(reason)
        return _json({"ok": True, "id": snap_id, "kind": "config"})

    async def _api_snapshots_list(self, request):
        from . import snapshots
        pid = request.query.get("project_id")
        if pid:
            return _json({"snapshots": snapshots.list_project_snapshots(pid)})
        return _json({"snapshots": snapshots.list_config_snapshots()})

    async def _api_snapshots_restore(self, request):
        from . import snapshots
        data = await request.json()
        snap_id = str(data.get("id", ""))
        pid = data.get("project_id")
        if pid:
            try:
                p = self._registry_for(pid)[0]
            except KeyError:
                return _json({"ok": False, "error": "project not found"}, 404)
            if self.engine is not None and getattr(self.engine.project, "id", None) == pid:
                if self.engine.engine_state not in (STATE_STOPPED, STATE_STOPPING):
                    return _json({"ok": False, "error": "stop the engine before restoring a project snapshot"}, 409)
            ok = snapshots.restore_project_snapshot(p.path, snap_id)
            if ok:
                self.registry.scan()
            return _json({"ok": ok})
        ok = snapshots.restore_config_snapshot(snap_id)
        if ok:
            # Reload config into memory + the orchestrator's server registry.
            fresh = FrameworkConfig(self.cfg.path)
            self.cfg.data = fresh.data
            self._base_orchestrator = None
            if self.engine is not None and hasattr(self.engine, "orchestrator"):
                self.engine.orchestrator._reload_servers()
        return _json({"ok": ok})

    async def _api_config_agent(self, request):
        """Natural-language reconfiguration: one validated action per call.
        The LLM maps the user's words onto the action schema; the framework
        validates and applies it, snapshotting first so it is revertible."""
        from . import promptlib, snapshots, ui_prefs
        from .agent import deep_merge, extract_json_actions
        data = await request.json()
        user_request = str(data.get("request", ""))
        if not user_request.strip():
            return _json({"ok": False, "error": "empty request"}, 400)
        orch = self._orch()
        tier = promptlib.detect_tier(orch.active_config)
        pid = data.get("project_id") or (self.engine.project.id if self.engine else None)
        project = self._registry_for(pid)[0] if pid else None
        ctx_parts = [f"active_project: {pid or 'none'}"]
        if project:
            ctx_parts.append(f"project language: {project.spec.get('language')}")
            ctx_parts.append(f"metrics: {list((project.spec.get('metrics') or {}).keys())}")
        ctx_parts.append("prefs: " + json.dumps(ui_prefs.load_prefs())[:800])
        prompt = promptlib.config_agent_prompt(tier, user_request, "\n".join(ctx_parts))
        try:
            raw = await asyncio.to_thread(lambda: orch.request_stream(prompt)[0])
        except Exception as e:
            return _json({"ok": False, "error": f"LLM call failed: {e}"}, 502)
        actions = extract_json_actions(raw, key="action")
        if not actions:
            return _json({"ok": False, "error": "the model produced no parseable action", "raw": raw[-500:]}, 502)
        act = actions[0]
        action = str(act.get("action", ""))
        if action == "set_pref":
            err = ui_prefs.set_pref(str(act.get("path", "")), act.get("value"))
            if err:
                return _json({"ok": False, "error": err}, 400)
            snapshots.take_config_snapshot(f"config-agent pref: {act.get('path')}")
            return _json({"ok": True, "action": action, "result": "preference applied",
                          "prefs": ui_prefs.load_prefs(), "css_vars": ui_prefs.apply_theme_to_css_vars(),
                          "reply": f"✓ {act.get('path')} set"})
        if action in ("edit_project", "add_metric"):
            if not project:
                return _json({"ok": False, "error": "no active project to edit"}, 400)
            if action == "add_metric":
                key = str(act.get("key", "")).strip()
                if not key:
                    return _json({"ok": False, "error": "add_metric needs a key"}, 400)
                changes = {"metrics": {key: {
                    "label": str(act.get("label", key)),
                    "unit": str(act.get("unit", "")),
                    "direction": str(act.get("direction", "lower")),
                    "weight": float(act.get("weight", 1)),
                }}}
            else:
                changes = act.get("changes")
                if not isinstance(changes, dict):
                    return _json({"ok": False, "error": "edit_project needs changes"}, 400)
            import copy as _copy
            new_spec = _copy.deepcopy(project.spec)
            new_spec.pop("dir", None)
            deep_merge(new_spec, changes)
            new_spec["id"] = project.id
            bad = self._scan_spec_commands(new_spec)
            if bad:
                return _json({"ok": False, "error": f"guardrail blocked: {bad}"}, 400)
            snapshots.take_project_snapshot(project.path, f"config-agent {action}")
            try:
                self.registry.update_spec(project.id, new_spec)
                self.registry.scan()
            except ValueError as e:
                return _json({"ok": False, "error": str(e)}, 400)
            return _json({"ok": True, "action": action, "result": "project updated", "reply": f"✓ project {project.id} updated"})
        if action == "run_smoke":
            if not project:
                return _json({"ok": False, "error": "no active project"}, 400)
            res = self._smoke_project(project)
            return _json({"ok": res.get("ok"), "action": action, "result": res,
                          "reply": f"smoke: {'PASS' if res.get('ok') else 'FAIL'} at {res.get('stage')}"})
        # answer / unknown → plain text reply
        return _json({"ok": True, "action": "answer", "result": str(act.get("text", raw[:400])), "reply": str(act.get("text", raw[:400]))})

    async def _api_prefs_get(self, request):
        from . import ui_prefs
        return _json({"prefs": ui_prefs.load_prefs(), "defaults": ui_prefs.DEFAULTS, "css_vars": ui_prefs.apply_theme_to_css_vars()})

    async def _api_prefs_set(self, request):
        from . import ui_prefs, snapshots
        data = await request.json()
        path = str(data.get("path", ""))
        err = ui_prefs.set_pref(path, data.get("value"))
        if err:
            return _json({"ok": False, "error": err}, 400)
        snapshots.take_config_snapshot(f"pref change: {path}")
        return _json({"ok": True, "prefs": ui_prefs.load_prefs(), "css_vars": ui_prefs.apply_theme_to_css_vars()})

    async def _api_prefs_reset(self, request):
        from . import ui_prefs, snapshots
        snapshots.take_config_snapshot("prefs reset to defaults")
        ui_prefs.reset_prefs()
        return _json({"ok": True, "prefs": ui_prefs.load_prefs(), "css_vars": ui_prefs.apply_theme_to_css_vars()})



    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        text = text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # engine controls
    # ------------------------------------------------------------------ #
    def _require_engine(self):
        if self.engine is None:
            raise web.HTTPBadRequest(text="no engine running")
        return self.engine

    async def _api_engine_start(self, request):
        eng = self._require_engine()
        data = await request.json() if request.can_read_body else {}
        multi = int(data.get("multi", 1)) if isinstance(data, dict) else 1
        eng.start(multi=multi)
        return _json({"ok": True})
    async def _api_engine_autofix(self, request):
        """Per-engine compile-loop knobs (KAI AUTOFIX): deterministic
        autofix turn cap and LLM repair attempt cap. Scoped by pid."""
        data = await request.json() if request.can_read_body else {}
        pid = str(data.get("project_id") or "") if isinstance(data, dict) else ""
        eng = self._engine_for(pid or None)
        if eng is None:
            return _json({"ok": False, "error": "no engine running"}, 400)
        def _num(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        tries = _num(data.get("tries")) if isinstance(data, dict) else None
        repair = _num(data.get("repair")) if isinstance(data, dict) else None
        settings = eng.set_autofix_settings(max_tries=tries, repair_max=repair)
        return _json({"ok": True, "project_id": eng.project.id,
                      "settings": settings, "effective": eng._autofix_effective()})

    async def _api_custom_code(self, request):
        data = await request.json()
        pid = str(data.get("project_id") or "") if isinstance(data, dict) else ""
        eng = self._engine_for(pid or None)
        if eng is None:
            return _json({"ok": False, "error": "no engine running on that project"}, 400)
        code = data.get("code", "")
        if not code.strip():
            return _json({"ok": False, "error": "empty code"}, 400)
        try:
            out = eng.submit_custom_code(code, source=data.get("source", "user"), language=data.get("language"))
            return _json({"ok": True, **out})
        except ValueError as e:
            return _json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return _json({"ok": False, "error": str(e)}, 500)

    async def _api_engine_switch(self, request):
        """Select a project's engine — starting one (paused) if the pool
        does not have it yet. Other pool engines keep running untouched."""
        data = await request.json()
        pid = data.get("project_id", "")
        try:
            project, reg = self._registry_for(pid)
        except KeyError:
            return _json({"ok": False, "error": f"project '{pid}' not found"}, 404)
        started = False
        eng = self.engines.get(pid)
        if eng is None:
            from .engine import EngineEvent, ProjectEngine
            from .llm import ModelOrchestrator
            orchestrator = ModelOrchestrator(self.cfg)
            events = EngineEvent()
            # Use the project's OWN registry (temp registry for temp
            # projects) so its workers resolve candidates/data under the
            # same root the engine was created with — temp projects must
            # never leak into the real projects/ tree.
            eng = ProjectEngine(project, orchestrator, reg,
                                worker_count=project.default_workers, events=events)
            eng.start(multi=project.default_multi, paused=self.cfg.engine_start_paused)
            self.engines[pid] = eng
            started = True
            self._persist_engine_pool()
        self._selected_project_id = pid
        return _json({"ok": True, "active_id": pid, "started": started})

    async def _api_engine_fuzzy(self, request):
        """Per-engine prompt-diversity knob (KAI FUZZY): 0 = champion always;
        N > 0 = random top-N scored basis per generation.  Runtime only."""
        data = await request.json()
        pid = str(data.get("project_id") or "") if isinstance(data, dict) else ""
        eng = self._engine_for(pid or None)
        if eng is None:
            return _json({"ok": False, "error": "no engine running"}, 400)
        try:
            n = int(data.get("top_n", 0))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "top_n must be a number"}, 400)
        n = eng.set_fuzzy(n)
        return _json({"ok": True, "project_id": eng.project.id, "top_n": n})

    async def _api_engine_stop(self, request):
        data = await request.json() if request.can_read_body else {}
        pid = str(data.get("project_id") or "") if isinstance(data, dict) else ""
        eng = self._engine_for(pid or None)
        if eng is None:
            return _json({"ok": False, "error": "no engine running"}, 400)
        eng.stop()
        stopped_pid = eng.project.id
        self.engines.pop(stopped_pid, None)
        if self._selected_project_id == stopped_pid:
            self._selected_project_id = None
        self._persist_engine_pool()
        return _json({"ok": True, "stopped": stopped_pid})

    async def _api_engine_pause(self, request):
        data = await request.json() if request.can_read_body else {}
        paused = bool(data.get("paused", True)) if isinstance(data, dict) else True
        pid = str(data.get("project_id") or "") if isinstance(data, dict) else ""
        eng = self._engine_for(pid or None)
        if eng is None:
            return _json({"ok": False, "error": "no engine running"}, 400)
        if paused:
            eng.request_pause()
        else:
            eng.request_resume()
        return _json({"ok": True, "state": eng.engine_state, "project_id": eng.project.id})

    async def _api_active(self, request):
        eng = self._require_engine()
        snap = eng.snapshot()
        snap["engines"] = self._engines_summary()
        return _json(snap)



    async def _api_worker_add(self, request):
        eng = self._require_engine()
        w = eng.add_worker()
        return _json({"ok": True, "worker": w})

    async def _api_worker_remove(self, request):
        eng = self._require_engine()
        wid = int(request.match_info["wid"])
        return _json({"ok": eng.remove_worker(wid, kill=False)})

    async def _api_worker_kill(self, request):
        eng = self._require_engine()
        wid = int(request.match_info["wid"])
        return _json({"ok": eng.kill_worker(wid)})

    # ------------------------------------------------------------------ #
    # servers
    # ------------------------------------------------------------------ #
    async def _api_server_add(self, request):
        data = await request.json()
        spec = dict(data)
        spec["label"] = str(data.get("label", "") or "")
        # API keys go to the local secrets file (0600, gitignored), never
        # config.json. Env vars (KAISEN_SERVER_<ID>_API_KEY) still win.
        # The key ALSO rides in-memory on the spec so the just-created
        # server can answer probes immediately (persist() strips it).
        api_key = str(spec.pop("api_key", "") or "").strip()
        if api_key:
            spec["api_key"] = api_key
        try:
            out = self._orch().add_server(spec)
        except ValueError as e:
            return _json({"ok": False, "error": str(e)}, 400)
        if api_key:
            save_secret("llm", str(out.get("id", "")), api_key)
        return _json({"ok": True, "server": out})

    async def _api_server_remove(self, request):
        sid = request.match_info["sid"]
        self._orch().remove_server(sid)
        save_secret("llm", sid, "")
        return _json({"ok": True})

    async def _api_server_active(self, request):
        data = await request.json()
        self._orch().set_active(data.get("ids", []))
        return _json({"ok": True})

    async def _api_server_health(self, request):
        sid = request.match_info["sid"]
        result = await asyncio.to_thread(self._orch().check_health, sid)
        return _json(result)

    async def _api_server_label(self, request):
        data = await request.json()
        try:
            out = self._orch().set_label(str(data.get("id", "")), str(data.get("label", "")))
            return _json({"ok": True, "server": out})
        except ValueError as e:
            return _json({"ok": False, "error": str(e)}, 400)

    # ------------------------------------------------------------------ #
    # config
    # ------------------------------------------------------------------ #
    async def _api_config_get(self, request):
        cfg = self.cfg.data
        # Never expose secrets plainly to the GUI.
        public = json.loads(json.dumps(cfg))
        public["safety"] = guardrail_state(self.cfg)
        # Telegram token: show masked + source, never the value.
        if self.cfg.telegram.get("token"):
            public["telegram"]["token"] = "********"
            public["telegram"]["token_source"] = "config.json"
        for s in public.get("llm", {}).get("servers", []):
            source = self.cfg.api_key_source(s.get("id", ""))
            s["api_key"] = "********" if (s.get("api_key") or source != "unset") else ""
            s["api_key_source"] = source
        public["telegram"]["token_set"] = bool(self.cfg.telegram_token)


        return _json(public)

    async def _api_config_put(self, request):
        data = await request.json()
        # The global safety switch cannot be changed via the GUI.
        if "safety" in data:
            data["safety"] = self.cfg.safety  # keep as-is
        # Telegram token: "********" or empty means "keep what's configured".
        tg = data.get("telegram")
        if isinstance(tg, dict) and tg.get("token") in ("", "********"):
            tg["token"] = self.cfg.telegram.get("token", "")
        self.cfg.data.update(data)
        self.cfg.save()
        return _json({"ok": True})

    async def _api_guardrails(self, request):
        return _json(guardrail_state(self.cfg))

    # ------------------------------------------------------------------ #
    # autofix (compiler-suggestion build fixer)
    # ------------------------------------------------------------------ #
    async def _api_autofix_get(self, request):
        projects = {}
        for p in self.registry.list():
            pid = p["id"]
            spec = self._registry_for(pid)[0].spec
            projects[pid] = (spec.get("skills") or {}).get("autofix_build", True)
        return _json({"default": self.cfg.autofix_build_enabled, "projects": projects})

    async def _api_autofix_set(self, request):
        data = await request.json()
        self.cfg.autofix["build_enabled"] = bool(data.get("enabled", True))
        self.cfg.save()
        return _json({"ok": True, "default": self.cfg.autofix_build_enabled})

    async def _api_autofix_apply(self, request):
        """Bulk-set skills.autofix_build on existing projects.
        Accepts {"projects": {"<pid>": true|false|"<custom path>"}}."""
        data = await request.json()
        projects = data.get("projects") or {}
        results = {}
        for pid, value in projects.items():
            try:
                p = self._registry_for(pid)[0]
                spec = p.spec
                spec.setdefault("skills", {})["autofix_build"] = value
                self.registry.update_spec(pid, spec)
                results[pid] = True
            except Exception as e:
                results[pid] = str(e)
        return _json({"ok": True, "results": results})

    # ------------------------------------------------------------------ #
    # notes (kept from the original dashboard)
    # ------------------------------------------------------------------ #
    def _notes_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "notes.json"

    def _load_notes(self):
        return load_json(self._notes_path(), {"notes": []})

    def _save_notes(self, data):
        save_json(self._notes_path(), data)

    async def _api_notes_get(self, request):
        return _json(self._load_notes())

    async def _api_notes_create(self, request):
        data = await request.json()
        notes = self._load_notes()
        import uuid
        note = {
            "id": str(uuid.uuid4()),
            "title": data.get("title", ""),
            "text": data.get("text", ""),
            "color": data.get("color", ""),
            "created_at": time.time(),
            "updated_at": time.time(),
            "archived": False,
            "comments": [],
        }
        notes["notes"].append(note)
        self._save_notes(notes)
        return _json(note, 201)

    async def _api_notes_update(self, request):
        nid = request.match_info["nid"]
        data = await request.json()
        notes = self._load_notes()
        for n in notes["notes"]:
            if n["id"] == nid:
                for k in ("title", "text", "color", "archived"):
                    if k in data:
                        n[k] = data[k]
                n["updated_at"] = time.time()
                self._save_notes(notes)
                return _json(n)
        return _json({"error": "not found"}, 404)

    async def _api_notes_delete(self, request):
        nid = request.match_info["nid"]
        notes = self._load_notes()
        notes["notes"] = [n for n in notes["notes"] if n["id"] != nid]
        self._save_notes(notes)
        return _json({"ok": True})

    async def _api_notes_reorder(self, request):
        data = await request.json()
        order = data.get("ids", [])
        notes = self._load_notes()
        by_id = {n["id"]: n for n in notes["notes"]}
        notes["notes"] = [by_id[i] for i in order if i in by_id] + [n for n in notes["notes"] if n["id"] not in order]
        self._save_notes(notes)
        return _json({"ok": True})

    # ------------------------------------------------------------------ #
    # legacy-shape endpoints (original dashboard UI)
    # ------------------------------------------------------------------ #
    async def _api_workers_legacy(self, request):
        eng = self._require_engine()
        spec = eng.project.spec
        # Which LLM served each generation (for the Model Routed card).
        gen_server: Dict[int, str] = {}
        for s in eng.sessions.snapshot():
            if s.get("server_id") and s.get("gen") is not None:
                gen_server[int(s["gen"])] = s["server_id"]
        out = {}
        for w in eng.pool.list_workers():
            stage = w.get("stage", "idle")
            running = stage != "idle"
            result = w.get("result") or {}
            out[w["worker_id"]] = {
                "status": "running" if running else "idle",
                "current_stage": stage,
                "generation": w.get("generation"),
                "temp_dir": w.get("temp_dir") or "",
                "model": gen_server.get(w.get("generation"), "—"),
                "live": w.get("live") or {},
                "elapsed": w.get("elapsed"),
                "current_ram": w.get("rss") or 0,
                "outcome": result.get("outcome"),
                "result_ok": result.get("ok"),
                "child_pid": w.get("child_pid"),
                "result_metrics": result.get("metrics") or {},
                "result_timings": result.get("timings") or {},
            }
        return _json({
            "workers": out,
            "schema": spec.get("metrics", {}),
            "telemetry": spec.get("telemetry") or {},
            "multi": getattr(eng, "_multi", 1),
        })

    async def _api_state_legacy(self, request):
        eng = self._require_engine()
        st = eng.state
        best = st.best
        metrics = best.get("metrics", {}) or {}
        best_size = metrics.get("compressed_size")
        if best_size is None and best.get("fitness") is not None:
            best_size = round(float(best["fitness"]), 5)
        return _json({
            "best_size": best_size,
            "generation": st.generation,
            "last_improvement_gen": st.last_improvement_gen,
            "stagnation": st.stagnation,
            "best_metrics": {"position_in_large_text_compression_benchmark": None, **{k: v for k, v in metrics.items()}},
            "history": st.history[-40:],
        })

    async def _api_modelstats(self, request):
        """Per-(server, skill) scoreboard: attempts / one-shots / wins /
        accumulated $ — which model does which skill best, and what is
        better to just not use."""
        skill = request.query.get("skill")
        rows = self._orch().model_stats()
        if skill:
            rows = [r for r in rows if r["skill"] == skill]
        return _json({"rows": rows})

    async def _api_llm_status_legacy(self, request):
        pool = list(self.engines.values())
        if not pool:
            # No engine: report "down" cleanly (200) instead of 400 spam —
            # the pill shows SYSTEM DOWN, the LLM list renders empty.
            return _json({
                "no_engine": True, "status": "down", "engine_state": "down",
                "generating": False, "tps": 0, "agg_tps": 0,
                "servers": [], "model_id": None, "engines": [],
            })
        eng = self.engine or pool[0]
        llm = eng.orchestrator.status()
        # Aggregate across EVERY pool engine: sessions, per-server stats,
        # and tps are summed so the pill/KAI report the whole pool load.
        sessions_all: List[Dict[str, Any]] = []
        stat_sum: Dict[str, Dict[str, Any]] = {}
        for e in pool:
            sessions_all.extend(e.sessions.snapshot())
            for sv in e.orchestrator.status().get("servers", []):
                sid = sv["id"]
                agg_row = stat_sum.setdefault(sid, {"requests": 0, "failures": 0, "busy": False,
                                                    "banned": False, "online": None, "tps": 0.0})
                st = sv.get("stats") or {}
                agg_row["requests"] += int(st.get("requests", 0) or 0)
                agg_row["failures"] += int(st.get("failures", 0) or 0)
                agg_row["busy"] = agg_row["busy"] or bool(sv.get("busy", False))
                agg_row["banned"] = agg_row["banned"] or bool(sv.get("banned", False))
                if sv.get("online") is False or agg_row["online"] is False:
                    agg_row["online"] = False
                elif agg_row["online"] is None:
                    agg_row["online"] = sv.get("online")
                agg_row["tps"] += float(st.get("last_tps", 0) or 0)
        active_tps: Dict[str, float] = {}
        agg = 0.0
        active = [s for s in sessions_all if s.get("status") == "generating"]
        by_server: Dict[str, List[Dict[str, Any]]] = {}
        for s in active:
            by_server.setdefault(s.get("server_id") or "", []).append(s)
            sid = s.get("server_id") or ""
            active_tps[sid] = (active_tps.get(sid) or 0.0) + float(s.get("tps") or 0)
        agg = sum(active_tps.values())
        if not agg:
            agg = sum(r["tps"] for r in stat_sum.values())
        active_ids = set(llm.get("active_ids", []))
        rows = []
        for sv in llm.get("servers", []):
            sid = sv["id"]
            st = sv.get("stats") or {}
            ast = stat_sum.get(sid, {"requests": 0, "failures": 0, "busy": False,
                                     "banned": False, "online": None, "tps": 0.0})
            name = (sv.get("label") or "").strip() or sv.get("url") or sid
            chats = by_server.get(sid, [])
            if chats:
                for s in chats:
                    rows.append({
                        "id": sid,
                        "chat_slot": s.get("slot"),
                        "label": sv.get("label") or "",
                        "display": name + (f" {s.get('slot')}" if len(chats) > 1 else ""),
                        "type": sv.get("type"),
                        "model": sv.get("model"),
                        "enabled": sv.get("enabled", True),
                        "active": sid in active_ids,
                        "busy": ast["busy"],
                        "inflight": len(chats),
                        "max_concurrent": sv.get("max_concurrent", 1),
                        "banned": ast["banned"],
                        "online": ast["online"],
                        "requests": ast["requests"],
                        "failures": ast["failures"],
                        "avg_seconds": st.get("avg_seconds"),
                        "last_error": st.get("last_error"),
                        "tps": round(s.get("tps") or 0, 1),
                        "streaming": 1 if (s.get("tps") or 0) > 0 else 0,
                    })
            else:
                rows.append({
                    "id": sid,
                    "chat_slot": None,
                    "label": sv.get("label") or "",
                    "display": name,
                    "type": sv.get("type"),
                    "model": sv.get("model"),
                    "enabled": sv.get("enabled", True),
                    "active": sid in active_ids,
                    "busy": ast["busy"],
                    "inflight": 0,
                    "max_concurrent": sv.get("max_concurrent", 1),
                    "banned": ast["banned"],
                    "online": ast["online"],
                    "requests": ast["requests"],
                    "failures": ast["failures"],
                    "avg_seconds": st.get("avg_seconds"),
                    "last_error": st.get("last_error"),
                    "tps": round(active_tps.get(sid) or ast["tps"] or 0, 1),
                    "streaming": 0,
                })

        # Reachability is probed ONCE per activation: servers with unknown
        # state get a single background probe; the next poll reflects it.
        for sv in llm.get("servers", []):
            sid = sv["id"]
            if sid in active_ids and sv.get("online") is None and sid not in self._probe_inflight:
                self._probe_inflight.add(sid)
                asyncio.create_task(self._probe_server(sid))


        # The pill's status mirrors the pool: green 'generating' when ANY
        # engine streams, else the selected engine's state.
        if any(e.is_generating for e in pool):
            status = "generating"
        elif eng.engine_state == STATE_PAUSED:
            status = "paused"
        elif eng.engine_state in (STATE_STOPPED, STATE_STOPPING):
            status = "stopped"
        else:
            status = "idle"
        return _json({
            "status": status,
            "engine_state": eng.engine_state,
            "generating": any(e.is_generating for e in pool),
            "tps": round(agg, 1),
            "agg_tps": round(agg, 1),
            "servers": rows,
            "model_id": None,
            "engines": self._engines_summary(),
        })

    async def _probe_server(self, sid: str) -> None:
        """One background reachability check; result lands in the next poll."""
        try:
            await asyncio.to_thread(self._orch().check_health, sid)
        except Exception:
            pass
        finally:
            self._probe_inflight.discard(sid)


    async def _api_llm_live_legacy(self, request):
        eng = self._require_engine()
        gen = request.query.get("gen")
        if gen:
            # Archive mode: a finished generation's prompt + raw LLM reply.
            prompt = ""
            output = ""
            try:
                target = eng.project.runs_dir / f"gen_{int(gen):06d}"
                p = target / "prompt.txt"
                o = target / "llm_raw.txt"
                if p.exists():
                    prompt = p.read_text(encoding="utf-8", errors="replace")[-8000:]
                if o.exists():
                    output = o.read_text(encoding="utf-8", errors="replace")[-20000:]
            except Exception:
                pass
            return _json({"mode": "archive", "gen": gen, "prompt": prompt, "output": output})
        # Live mode: the active LLM sessions, each streaming its code.
        llm = eng.orchestrator.status()
        tps = max([s["stats"]["last_tps"] for s in llm.get("servers", []) if s["stats"].get("last_tps")] or [0])
        by_id = {s["id"]: s for s in llm.get("servers", [])}
        sessions = eng.sessions.snapshot()
        server = request.query.get("server")
        slot = request.query.get("slot")
        for s in sessions:
            sv = by_id.get(s.get("server_id")) or {}
            s["model"] = sv.get("model") or s.get("server_id")
            s["display"] = (sv.get("label") or "").strip() or sv.get("url") or s.get("server_id")
            if s.get("status") == "generating":
                tps = max(tps, s.get("tps") or 0)
        if server:
            server = server.lower()
            sessions = [
                s for s in sessions
                if (s.get("server_id") or "").lower() == server
                or server in (s.get("display") or "").lower()
                or server in (s.get("model") or "").lower()
            ]
        if slot is not None:
            sessions = [s for s in sessions if str(s.get("slot") or "") == slot]
        return _json({
            "mode": "live",
            "generating": eng.is_generating,
            "tps": tps,
            "sessions": sessions,
        })
    async def _api_debug_logs_legacy(self, request):
        eng = self._require_engine()
        return _json({"logs": eng._last_log[-100:]})

    async def _api_iterations_legacy(self, request):
        pid = request.query.get("project_id")
        eng = self._engine_for(pid)
        if eng is None:
            return _json({"iterations": []})
        # Source of truth: the engine history — EVERY generation lands
        # there (ok / valid / no_code / duplicate_skip / rejected_dangerous
        # / cancelled / stage failures).  results.csv only carries scored
        # rows and used to hide the rest from the table.
        rows = {int(r.get("generation", 0)): r for r in eng.results.read()}
        items = []
        for h in list(eng.state.history):
            try:
                gen = int(h.get("generation", 0))
            except (TypeError, ValueError):
                continue
            r = rows.get(gen, {})
            outcome = h.get("outcome") or r.get("outcome") or "?"
            if outcome == "ok":
                outcome = "NEW_BEST"
            metrics = dict(h.get("metrics") or {})
            if not metrics:
                metrics = {k: v for k, v in r.items() if k not in ("generation", "outcome", "fitness")}
            try:
                gen_time = float(r.get("score_time") or 0)
            except (TypeError, ValueError):
                gen_time = 0.0
            items.append({
                "iteration": gen,
                "outcome": outcome,
                "gen_time": gen_time,
                "prompt_snippet": json.dumps(metrics)[:120],
                "metrics": metrics,
                "detail": (h.get("detail") or "")[:600],
                "fitness": h.get("fitness"),
            })
        return _json({"iterations": items})


    async def _api_worker_kill_process_legacy(self, request):
        eng = self._require_engine()
        wid = int(request.match_info["wid"])
        ok = eng.kill_worker_process(wid)
        return _json({"ok": ok, "message": f"Worker {wid} process killed." if ok else f"Worker {wid} has no running process."})

    async def _api_engine_multi(self, request):
        data = await request.json() if request.can_read_body else {}
        pid = str(data.get("project_id") or "") if isinstance(data, dict) else ""
        eng = self._engine_for(pid or None)
        if eng is None:
            return _json({"ok": False, "error": "no engine running"}, 400)
        try:
            n = int(data.get("multi", 1) or 1)
        except (TypeError, ValueError):
            n = 1
        n = eng.set_multi(n)
        self._persist_engine_pool()
        return _json({"ok": True, "multi": n})

    async def _api_worker_kill_legacy(self, request):
        eng = self._require_engine()
        wid = int(request.match_info["wid"])
        ok = eng.kill_worker(wid)
        return _json({"ok": ok, "message": f"Worker {wid} terminated." if ok else f"Worker {wid} not found."})

    async def _api_file_size(self, request):
        p = request.query.get("path", "")
        root = Path(__file__).resolve().parent.parent
        try:
            full = (root / p).resolve()
            if full.exists() and full.is_file() and (full == root or root in full.parents):
                return _json({"ok": True, "size": full.stat().st_size})
        except Exception:
            pass
        return _json({"ok": False, "size": 0})

    async def _api_open_folder(self, request):
        path = request.match_info.get("path", "")
        import subprocess
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return _json({"ok": True})
        except Exception as e:
            return _json({"ok": False, "error": str(e)})

    async def _api_model_status_legacy(self, request):
        eng = self._require_engine()
        timeout = float(self.cfg.llm.get("read_timeout", 1200))
        return _json({"is_busy": eng.orchestrator.is_busy, "llm_read_timeout": timeout})

    async def _api_models_legacy(self, request):
        eng = self._require_engine()
        models = []
        for s in eng.orchestrator.list_servers():
            try:
                port = int(s.get("url", "").split(":")[-1].split("/")[0]) if ":" in s.get("url", "") else None
            except Exception:
                port = None
            models.append({"id": s["id"], "type": s["type"], "port": port, "model": s.get("model")})
        return _json({"models": models})

    async def _api_prompts_legacy(self, request):
        eng = self._require_engine()
        files = []
        blocks_dir = eng.project.prompts_dir / "blocks"
        if blocks_dir.exists():
            files = sorted(p.stem for p in blocks_dir.glob("*.md"))
        return _json({"files": files})

    async def _api_config_post_legacy(self, request):
        return await self._api_config_put(request)

    async def _api_llm_pause_legacy(self, request):
        eng = self._require_engine()
        body = await request.text()
        paused = body.strip().lower() in ("true", "1", "yes")
        if paused:
            eng.request_pause()
        else:
            eng.request_resume()
        return _json({"ok": True, "state": eng.engine_state})

    async def _api_llm_stop_legacy(self, request):
        eng = self._require_engine()
        eng.request_stop()
        return _json({"ok": True, "state": eng.engine_state})

    async def _api_llm_resume_legacy(self, request):
        eng = self._require_engine()
        eng.request_resume()
        return _json({"ok": True, "state": eng.engine_state})

    async def _api_override_status_legacy(self, request):
        eng = self._require_engine()
        return _json({
            "model_override_active": False,
            "prompt_override_active": eng.prompt_override_active,
            "llm_paused": eng.state.paused,
        })

    async def _api_override_model_toggle_legacy(self, request):
        return _json({"ok": True})

    async def _api_override_model_legacy(self, request):
        """Map 'override model' to the active-server selection."""
        eng = self._require_engine()
        data = await request.json()
        value = data.get("value")
        if isinstance(value, dict):
            spec = dict(value)
            spec.setdefault("id", f"custom-{int(time.time())}")
            eng.orchestrator.add_server(spec)
            eng.orchestrator.set_active([spec["id"]])
        elif isinstance(value, str) and value:
            eng.orchestrator.set_active([value])
        return _json({"ok": True})

    async def _api_override_model_clear_legacy(self, request):
        eng = self._require_engine()
        eng.orchestrator.set_active([s["id"] for s in eng.orchestrator.list_servers() if s["enabled"]])
        return _json({"ok": True})

    async def _api_override_prompt_toggle_legacy(self, request):
        return _json({"ok": True})

    async def _api_override_prompt_legacy(self, request):
        eng = self._require_engine()
        data = await request.json()
        value = data.get("value", "")
        if data.get("type") == "file" and value:
            block = eng.project.prompts_dir / "blocks" / f"{value}.md"
            if block.exists():
                value = block.read_text(encoding="utf-8")
            else:
                return _json({"ok": False, "error": f"prompt file '{value}' not found"}, 404)
        eng.set_prompt_override(value)
        return _json({"ok": True})

    async def _api_override_prompt_clear_legacy(self, request):
        eng = self._require_engine()
        eng.clear_prompt_override()
        return _json({"ok": True})

    # ------------------------------------------------------------------ #
    # notes extras (original dashboard)
    # ------------------------------------------------------------------ #
    async def _api_notes_colors(self, request):
        data = await request.json()
        notes = self._load_notes()
        notes["color_order"] = data.get("color_order", [])
        self._save_notes(notes)
        return _json({"ok": True})

    async def _api_notes_similarity(self, request):
        data = await request.json()
        text = (data.get("text") or "").lower().strip()
        exclude = data.get("exclude_id")
        notes = self._load_notes().get("notes", [])
        similar = []
        import difflib
        for n in notes:
            if exclude and n.get("id") == exclude:
                continue
            candidate = ((n.get("title") or "") + " " + (n.get("text") or "")).lower()
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, text, candidate).ratio()
            if ratio >= 0.8:
                similar.append({"id": n.get("id"), "title": n.get("title") or "Untitled", "similarity": round(ratio, 3)})
        return _json({"similar_notes": similar[:5]})

    async def _api_notes_comment_add(self, request):
        nid = request.match_info["nid"]
        data = await request.json()
        notes = self._load_notes()
        for n in notes["notes"]:
            if n["id"] == nid:
                n.setdefault("comments", []).append({"text": data.get("text", ""), "timestamp": time.time()})
                n["updated_at"] = time.time()
                self._save_notes(notes)
                return _json(n)
        return _json({"error": "not found"}, 404)

    async def _api_notes_comment_delete(self, request):
        nid = request.match_info["nid"]
        data = await request.json()
        text = data.get("text")
        ts = data.get("timestamp")
        notes = self._load_notes()
        for n in notes["notes"]:
            if n["id"] == nid:
                before = len(n.get("comments", []))
                n["comments"] = [c for c in n.get("comments", []) if not (c.get("text") == text and c.get("timestamp") == ts)]
                if len(n["comments"]) < before:
                    n["updated_at"] = time.time()
                    self._save_notes(notes)
                    return _json({"ok": True})
                return _json({"ok": False, "error": "comment not found"}, 404)
        return _json({"error": "not found"}, 404)

    async def _api_notes_archive(self, request):
        nid = request.match_info["nid"]
        data = await request.json()
        notes = self._load_notes()
        for n in notes["notes"]:
            if n["id"] == nid:
                n["archived"] = bool(data.get("archived", True))
                n["updated_at"] = time.time()
                self._save_notes(notes)
                return _json(n)
        return _json({"error": "not found"}, 404)

    # ------------------------------------------------------------------ #
    # system
    # ------------------------------------------------------------------ #
    async def _api_system(self, request):
        try:
            import psutil
        except ImportError:
            return _json({
                "cpu_percent": None, "ram_total": None, "ram_used": None,
                "ram_percent": None, "load_avg": None, "uptime": None,
                "gpu_vram_mb": None, "gpu_power_w": None, "gpu_temp_c": None,
                "cpu_temp_c": None, "cpu_power_w": None, "ram_usage_mb": None,
                "ram_usage_pct": None, "ram_temp_c": None, "ram_power_w": None,
                "generation_duration_s": 0,
            })
        vm = psutil.virtual_memory()
        gpu = self._gpu_metrics()
        cpu_temp = self._cpu_temp()
        return _json({
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_total": vm.total,
            "ram_used": vm.used,
            "ram_percent": vm.percent,
            "load_avg": list(psutil.getloadavg()),
            "uptime": time.time() - psutil.boot_time(),
            # legacy keys used by the original status pill
            "gpu_vram_mb": gpu.get("vram_used_mb"),
            "gpu_power_w": gpu.get("power_w"),
            "gpu_temp_c": gpu.get("temp_c"),
            "cpu_temp_c": cpu_temp,
            "cpu_power_w": None,
            "ram_usage_mb": vm.used,
            "ram_usage_pct": vm.percent,
            "ram_temp_c": None,
            "ram_power_w": None,
            "generation_duration_s": 0,
        })

    @staticmethod
    def _gpu_metrics() -> Dict[str, Any]:
        import subprocess
        now = time.time()
        if getattr(DashboardServer, "_gpu_cache_ts", 0) and now - DashboardServer._gpu_cache_ts < 2:
            return DashboardServer._gpu_cache
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                print(f"[gpu] nvidia-smi rc={r.returncode} err={r.stderr[:120]!r}")
                DashboardServer._gpu_cache, DashboardServer._gpu_cache_ts = {}, now
                return {}
            lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
            if not lines:
                DashboardServer._gpu_cache, DashboardServer._gpu_cache_ts = {}, now
                return {}
            # Aggregate over all GPUs: sum memory/power, max temperature.
            vram_used = vram_total = 0
            temp_c = 0.0
            power_w = 0.0
            for ln in lines:
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) < 4:
                    continue
                try:
                    vram_used += int(float(parts[0]))
                    vram_total += int(float(parts[1]))
                    temp_c = max(temp_c, float(parts[2]))
                    power_w += float(parts[3])
                except (ValueError, IndexError):
                    continue
            out = {"vram_used_mb": vram_used, "vram_total_mb": vram_total,
                   "temp_c": temp_c, "power_w": power_w}
            DashboardServer._gpu_cache, DashboardServer._gpu_cache_ts = out, now
            return out
        except Exception as e:
            print(f"[gpu] query failed: {e!r}")
            DashboardServer._gpu_cache, DashboardServer._gpu_cache_ts = {}, now
            return {}

    @staticmethod
    def _cpu_temp() -> Optional[float]:
        try:
            import psutil
            temps = psutil.sensors_temperatures()
            for key in ("coretemp", "k10temp", "cpu_thermal"):
                if key in temps:
                    vals = [t.current for t in temps[key] if t.current]
                    if vals:
                        return max(vals)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_async())
        self._loop.run_forever()

    async def _start_async(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

    def stop(self) -> None:
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
