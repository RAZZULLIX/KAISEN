#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KAISEN Agent Protocol (KAI) — the LLM-facing API of the framework.

Deliberately NOT MCP. MCP makes models do protocol gymnastics (JSON-RPC
handshakes, schema round-trips, content arrays) before any work happens.
KAI is a line-oriented, stateful, tolerant protocol: an LLM connects,
sets a project once, and drives evolution with one-word commands.
Responses are compact text blocks that small models can re-feed into
their next turn without summarization loss.

Grammar (case-insensitive, trailing punctuation ignored):

    PROJECT <id>             set session project
    STATUS                   engine + project status (lists all active projects)
    SPEC                     pipeline/metrics/goal summary
    RUN [<n>] [FOR <secs>] [ON <pid>] [WITH <k>]
                             start evolving in the background — forever by
                             default; <n> / FOR <secs> are optional goals;
                             WITH <k> drives k LLM pipelines in parallel
    FORGE [<n>] [ON <pid>] [GOAL <words...>]
                             generate n parallel drafts (default 3, max 12),
                             each pipeline-scored and ranked
    WAIT [<secs>]            block until the in-flight run finishes
    PAUSE | RESUME | STOP    engine control
    BEST                     champion source code + metrics
    SMOKE                    run the pipeline once on the baseline
    BASELINE [lang]          stage starting code; lines until END
    CANDIDATE [lang]         queue YOUR code; lines until END
    SNAPSHOT [LIST|TAKE|RESTORE <id>] [ON <pid>]
    SERVERS                  LLM servers + active set
    GOAL <words...>          AI designs + smoke-validates a whole project
                             (blocking, minutes on small models); uses the
                             staged BASELINE; returns the spec JSON
    ACCEPT <id>              create the project from the last GOAL spec
    CREATE <id> <spec-json>  create a project from a hand-written spec
    HELP                     this reference
    QUIT                     end session

Transports:
- stdio : python main.py --kai        (spawns the dashboard if needed)
- HTTP  : POST /kai on the dashboard (text/plain in, text/plain out)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from .util import load_json, save_json

REPO_ROOT = Path(__file__).resolve().parent.parent
__version__ = "0.1.1-alpha"
_RUNS_FILE = REPO_ROOT / "kai_runs.json"   # persistent run goals (gitignored)


# ---------------------------------------------------------------------------
# dashboard client (same-machine HTTP + project file access)
# ---------------------------------------------------------------------------


class KaiError(Exception):
    pass


class KaiClient:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        # One persistent session: cookies (including the KAI sticky-project
        # session cookie) survive across requests, so a CLI client keeps its
        # selected project between calls — just like curl -c/-b would.
        self._session = requests.Session()
        # The dashboard may require a server password. The client reads
        # the same env var the server does (KAISEN_API_KEY) — set it
        # here and both sides agree; empty = no auth.
        key = os.environ.get("KAISEN_API_KEY", "").strip()
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             read_timeout: float = 120.0) -> Dict[str, Any]:
        url = self.base + path
        try:
            if method == "GET":
                resp = self._session.get(url, headers=self.headers, timeout=(3.0, read_timeout))
            elif method == "POST":
                resp = self._session.post(url, json=body, headers=self.headers,
                                          timeout=(3.0, read_timeout))
            elif method == "PUT":
                resp = self._session.put(url, json=body, headers=self.headers,
                                         timeout=(3.0, read_timeout))
            else:
                raise KaiError(f"unsupported method {method}")
        except requests.exceptions.ConnectionError as e:
            raise KaiError(f"dashboard unreachable at {self.base}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise KaiError(f"dashboard timed out ({path}): {e}") from e
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "error": f"non-JSON response ({resp.status_code})"}
        if resp.status_code >= 400 and "error" not in data:
            data["error"] = f"HTTP {resp.status_code}"
        return data

    def alive(self) -> bool:
        try:
            self.call("GET", "/api/projects", read_timeout=3.0)
            return True
        except KaiError:
            return False


def _spawn_dashboard(host: str, port: int) -> None:
    log_path = Path(os.environ.get("KAISEN_KAI_LOG", str(REPO_ROOT / "kai-dashboard.log")))
    with open(log_path, "a", encoding="utf-8") as log:
        subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "main.py"), "--host", host, "--port", str(port)],
            cwd=str(REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def connect(host: str, port: int, auto_start: bool = True, wait_s: float = 60.0) -> KaiClient:
    client = KaiClient(f"http://{host}:{port}")
    if client.alive():
        return client
    if not auto_start:
        raise KaiError(
            f"dashboard not running at http://{host}:{port} and auto-start is disabled "
            "(KAISEN_KAI_NO_AUTOSTART=1). Start it with: python main.py"
        )
    _spawn_dashboard(host, port)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(1.0)
        if client.alive():
            return client
    raise KaiError(f"dashboard did not become ready at http://{host}:{port} within {wait_s:.0f}s")


def _read_best(project_id: str) -> Dict[str, Any]:
    proj_dir = REPO_ROOT / "projects" / project_id
    spec_file = proj_dir / "project.json"
    if not spec_file.is_file():
        raise KaiError(f"project '{project_id}' not found")
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise KaiError(f"cannot read project spec: {e}") from e
    best = None
    state_file = proj_dir / "state.json"
    if state_file.is_file():
        try:
            best = json.loads(state_file.read_text(encoding="utf-8")).get("best")
        except (OSError, ValueError):
            best = None
    code_path, metrics, generation = None, {}, None
    if best and best.get("code_path"):
        code_path = Path(best["code_path"])
        metrics, generation = best.get("metrics", {}) or {}, best.get("generation")
    if not code_path or not code_path.is_file():
        base = str((spec.get("data") or {}).get("baseline_source", "") or "")
        fallback = proj_dir / base if base else None
        if fallback and fallback.is_file():
            code_path, metrics, generation = fallback, {}, None
    if not code_path or not code_path.is_file():
        raise KaiError(f"no champion or baseline source for '{project_id}' — run the engine first")
    try:
        code = code_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise KaiError(f"cannot read source: {e}") from e
    return {
        "project_id": project_id,
        "language": spec.get("language", "c"),
        "source_path": str(code_path),
        "generation": generation,
        "metrics": metrics,
        "code": code[:40000],
    }


# ---------------------------------------------------------------------------
# the protocol
# ---------------------------------------------------------------------------

HELP_TEXT = """KAI protocol — the SERVER's replies start with OK or ERR; you send
BARE command lines, never prefixed with OK. Commands (case-insensitive):
  PROJECT <id>                set session project (STICKY for this session)
  STATUS                      engine + project status (lists all active projects)
  SPEC                        pipeline steps, metrics, goal
  RUN [<n>] [FOR <secs>] [ON <pid>] [WITH <k>]
                             start evolving NOW (background).
                             No goal = run FOREVER until STOP. <n> = stop
                             after n generations; FOR <secs> = time budget;
                             WITH <k> = drive k LLM pipelines in parallel.
  FORGE [<n>] [TIER <tiny|small|large>] [ON <pid>] [GOAL <words...>]
                             n parallel drafts, each pipeline-scored
  ESTIMATE <in> [<out>]      per-server cost/time for a call of that size
  WAIT [<secs>]               block until the in-flight run finishes
  PAUSE | RESUME | STOP [ON <pid>]
                             engine control — ON <pid> targets another pool
                             member without re-selecting it
  BEST                        champion source code
  SMOKE [pid] | SMOKE ON <pid>
                             run pipeline once on the baseline
  BASELINE [lang]             stage starting code: lines until END
  CANDIDATE [lang]            queue code into evolution: lines until END
  SNAPSHOT [LIST|TAKE|RESTORE <id>] [ON <pid>]
  SERVERS                     LLM servers + active set
  GOAL <words...> [TEMP]     AI designs + validates a project (blocking);
                             uses the staged BASELINE as the program.
                             TEMP: the project lands in the temp/ root and
                             everything it writes is wiped at server close
                             or next startup — a quick run leaves no trace
                             and the real setup is NEVER touched.

  ACCEPT <id>                 create the project from the last GOAL spec
  CREATE <id> [TEMP] <spec-json>
                             create a project from a hand-written spec
                             (TEMP: lives in temp/, wiped on close/restart)
  AUTOFIX [tries <n>] [repair <n|off>]
                             compile-loop knobs for the session project:
                             deterministic autofix turns (default 5) and
                             LLM repair attempts (default 3; off = 0)
  HELP                        this text
  QUIT                        end session
NOTES: HTTP clients are FRESH per request — send PROJECT <id> + the command
in ONE body, or pass a cookie (curl -c/-b) so the server remembers the
project. Run budgets survive daemon restarts (kai_runs.json).
Examples:
  PROJECT md5-speed
  RUN
  WAIT 300
  BASELINE c
  int main(){return 0;}
  END
  GOAL make it as fast as possible, output must stay identical
  ACCEPT md5-speed-2
  RUN 50
  BEST"""


# command word -> aliases. First word of the line is matched (case-insensitive,
# trailing punctuation stripped). Several aliases per command so small models
# that paraphrase the verb still get through.
ALIASES: Dict[str, List[str]] = {
    "PROJECT": ["PROJECT", "USE", "SET", "PICK"],
    "STATUS": ["STATUS", "STATE", "ST"],
    "SPEC": ["SPEC", "DESCRIBE", "SHOW", "INSPECT", "INFO"],
    "RUN": ["RUN", "ITERATE", "EVOLVE", "OPTIMIZE", "GO"],
    "FORGE": ["FORGE", "SMITH", "DRAFTS"],
    "PAUSE": ["PAUSE", "HALT", "FREEZE"],
    "WAIT": ["WAIT", "SYNC", "AWAIT", "JOIN"],
    "SERVERS": ["SERVERS", "LLM", "BACKENDS"],
    "MODELS": ["MODELS", "SCOREBOARD", "RANKINGS"],
    "AUTOFIX": ["AUTOFIX", "FIXER"],
    "RESUME": ["RESUME", "UNPAUSE", "CONTINUE", "PLAY"],
    "STOP": ["STOP", "KILL", "OFF"],
    "BEST": ["BEST", "CHAMPION", "CHAMP", "WINNER"],
    "SMOKE": ["SMOKE", "TEST", "CHECK"],
    "CANDIDATE": ["CANDIDATE", "SUBMIT", "INJECT", "CODE", "PATCH"],
    "SNAPSHOT": ["SNAPSHOT", "SNAP", "SAVE", "RESTORE", "ROLLBACK", "UNDO"],
    "SERVERS": ["SERVERS", "LLM", "BACKENDS"],
    "MODELS": ["MODELS", "SCOREBOARD", "RANKINGS"],
    "GOAL": ["GOAL", "SUGGEST", "DESIGN", "BUILD", "NEW"],
    "CREATE": ["CREATE", "MAKE"],
    "ESTIMATE": ["ESTIMATE", "COST", "PRICE", "BUDGET"],
    "ACCEPT": ["ACCEPT", "ADOPT"],
    "HELP": ["HELP", "H", "?"],
    "QUIT": ["QUIT", "EXIT", "BYE", "DONE", "END-SESSION"],
}

_ALIAS_INDEX: Dict[str, str] = {w.lower(): cmd for cmd, ws in ALIASES.items() for w in ws}


def _split(line: str) -> Tuple[str, str]:
    line = line.strip()
    # Small models decorate their output: strip quotes/backticks/markdown,
    # command labels, and the server-side OK/ERR convention they imitate.
    line = line.strip('"\'`*').strip()
    line = re.sub(r"^(command|cmd)\s*[:=]?\s*", "", line, flags=re.I)
    line = re.sub(r"^ok\??[\s:,\/]*", "", line, flags=re.I)
    line = re.sub(r"^err(or)?[\s:,\/]*", "", line, flags=re.I)
    word, _, rest = line.partition(" ")
    word = re.sub(r"[:.,;!?]+$", "", word.strip()).lower()
    return word, rest.strip()


class KaiSession:
    """One LLM session: project context + backend client."""

    def __init__(self, client: KaiClient):
        self.client = client
        self.project: Optional[str] = None
        # Sidecar state: baseline code staged for GOAL, last suggested spec,
        # and the in-flight run goal (None targets = run forever).  The run
        # goal persists to disk so a daemon restart doesn't lose the budget.
        self._baseline_code: Optional[str] = None
        self._last_spec: Optional[Dict[str, Any]] = None
        self._last_temp: bool = False
        self._run_goal: Optional[Dict[str, Any]] = self._load_run_goal()

    def _load_run_goal(self) -> Optional[Dict[str, Any]]:
        data = load_json(_RUNS_FILE, None)
        if isinstance(data, dict) and data.get("pid"):
            return data
        return None

    def _save_run_goal(self) -> None:
        if self._run_goal:
            save_json(_RUNS_FILE, self._run_goal)
        else:
            try:
                _RUNS_FILE.unlink(missing_ok=True)
            except Exception:
                pass

    def _parse_on(self, arg: str) -> Optional[str]:
        """Extract `ON <pid>` from a command tail, or None.  Lets STOP/SMOKE/
        PAUSE/RESUME target another pool member without re-selecting it."""
        tokens = arg.split()
        for i, t in enumerate(tokens):
            if t.upper().rstrip(":,s") == "ON" and i + 1 < len(tokens):
                return tokens[i + 1].strip().lower()
        return None

    def _need_project(self) -> str:
        if not self.project:
            raise KaiError("no project set — use PROJECT <id> (see SERVERS/HELP)")
        return self.project

    def _active_state(self) -> Dict[str, Any]:
        try:
            return self.client.call("GET", "/api/active", read_timeout=10.0)
        except KaiError:
            return {"engine_state": "down", "state": {}}

    # -- commands ----------------------------------------------------------

    def cmd_project(self, arg: str) -> str:
        pid = arg.strip().lower()
        if not pid:
            raise KaiError("PROJECT needs an id — e.g. PROJECT md5-speed")
        out = self.client.call("GET", "/api/projects", read_timeout=10.0)
        ids = [p["id"] for p in out.get("projects", [])]
        if pid not in ids:
            raise KaiError(f"project '{pid}' not found — known: {', '.join(ids) or 'none'}")
        self.project = pid
        name = next(p["name"] for p in out["projects"] if p["id"] == pid)
        return f"OK project={pid} ({name})"

    def cmd_status(self, arg: str) -> str:
        act = self._active_state()
        st = act.get("state", {})
        lines = ["OK"]
        eng_pid = act.get("project_id")
        eng = act.get("engine_state", "down")
        lines.append(f"ENGINE {eng} gen={st.get('generation', '-')} paused={st.get('paused', '-')} project={eng_pid or '-'}")
        best = st.get("best") or {}
        if best:
            lines.append(f"BEST fitness={best.get('fitness')} metrics: " +
                         " ".join(f"{k}={v}" for k, v in (best.get('metrics') or {}).items()))
        if self._run_goal:
            lines.append("RUN " + self._run_progress(self._run_goal, self._engine_entry(self._run_goal["pid"])) + " (in flight — WAIT/STOP)")
        if self.project:
            spec = self.client.call("GET", f"/api/projects/{self.project}/spec", read_timeout=10.0).get("spec")
            if spec:
                lines.append(f"SESSION PROJECT {spec.get('id')} \"{spec.get('name')}\" ({spec.get('language')})")
        engines = act.get("engines")
        lines.append("ACTIVE PROJECTS")
        if engines:
            for e in engines:
                mark = "*" if e.get("project_id") == self.project else " "
                lines.append(f"  {mark}{e.get('project_id')} {e.get('engine_state')} gen={e.get('generation')} paused={e.get('paused')} best={e.get('best_fitness')}")
        else:
            lines.append("  (none)")
        return "\n".join(lines)

    def cmd_spec(self, arg: str) -> str:
        pid = arg.strip() or self._need_project()
        spec = self.client.call("GET", f"/api/projects/{pid}/spec", read_timeout=10.0).get("spec")
        if not spec:
            raise KaiError(f"project '{pid}' not found")
        lines = [f"OK {spec.get('id')} \"{spec.get('name')}\" ({spec.get('language')}, artifact {spec.get('artifact_name')})"]
        goal = (spec.get("prompts") or {}).get("goal", "")
        if goal:
            lines.append(f"GOAL {goal[:200]}")
        steps = spec.get("steps", {})
        for stage in ("build", "verify", "score"):
            s = steps.get(stage, [])
            if stage == "build":
                s = [s] if s else []
            for i, st in enumerate(s if isinstance(s, list) else [s]):
                if (st.get("inline") or {}).get("code"):
                    kind = f"inline {st['inline'].get('lang', 'python')}"
                else:
                    kind = st.get("program", "?")
                n = f"{stage}[{i}]" if stage != "build" else "build"
                lines.append(f"STEP {n} {kind} args={st.get('args')} timeout={st.get('timeout')}")
        metrics = spec.get("metrics", {})
        lines.append("METRICS " + (", ".join(
            f"{k}({v.get('direction', 'lower')},w={v.get('weight', 1)}"
            + (f",c={v.get('constraint')}" if v.get("constraint") is not None else "")
            + ")" for k, v in metrics.items()
        ) or "none"))
        return "\n".join(lines)
    def cmd_run(self, arg: str) -> str:
        """Start the sidecar. KAISEN evolves FOREVER by default; goals are
        optional flags: RUN <n> (stop after n generations), RUN FOR <secs>
        (stop after a time budget), RUN WITH <k> (drive k LLM pipelines in
        parallel). Always returns immediately — use WAIT to synchronize,
        STATUS to watch, STOP to end."""
        tokens = arg.split()
        gen_target: Optional[int] = None
        budget: Optional[float] = None
        multi_k: Optional[int] = None
        pid: Optional[str] = None
        # One pass: flags may appear in any order and the gen count must
        # not shadow an ON <pid> that follows it.
        for i, t in enumerate(tokens):
            u = t.upper().rstrip(":,s")
            if u in ("FOR", "BUDGET", "TIME") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
                budget = float(tokens[i + 1])
            elif u in ("WITH", "USING") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
                multi_k = max(1, int(tokens[i + 1]))
            elif u == "ON" and i + 1 < len(tokens):
                pid = tokens[i + 1].strip().lower()
            elif t.isdigit() and gen_target is None:
                gen_target = int(t)
        pid = pid or self._need_project()

        sw = self.client.call("POST", "/api/engine/switch", {"project_id": pid}, read_timeout=60.0)
        if not sw.get("ok"):
            raise KaiError(f"engine switch failed: {sw.get('error')}")
        if multi_k:
            res = self.client.call("POST", "/api/engine/multi", {"multi": multi_k, "project_id": pid}, read_timeout=60.0)
            multi_k = int(res.get("multi", multi_k))
        entry = self._engine_entry(pid)
        start_gen = int(entry.get("generation", 0))
        start_best = (entry.get("best") or {}).get("fitness")
        # Progress is measured in SCORED outcomes (iteration history), not
        # the engine's generation counter — that increments when the
        # producer QUEUES a candidate, before it is ever scored.
        iterations = self.client.call("GET", f"/api/iterations?project_id={pid}", read_timeout=10.0)
        hist0 = iterations if isinstance(iterations, list) else iterations.get("iterations", [])
        start_hist = len(hist0)
        self.client.call("POST", "/api/engine/pause", {"paused": False, "project_id": pid}, read_timeout=60.0)
        self._run_goal = {
            "pid": pid,
            "gen_target": gen_target,
            "ts_deadline": (time.time() + budget) if budget else None,
            "start_gen": start_gen,
            "start_hist": start_hist,
            "start_best": start_best,
        }
        self._save_run_goal()
        desc = (f"{gen_target} generations" if gen_target else "forever")
        if budget:
            desc += f" (budget {budget:.0f}s)"
        if multi_k:
            desc += f" with {multi_k} LLMs"
        return (f"OK running {desc} on {pid} in the background — WAIT to synchronize, "
                f"STATUS to watch, STOP to end")

    def _hist_len(self, pid: Optional[str] = None) -> int:
        path = f"/api/iterations?project_id={pid}" if pid else "/api/iterations"
        iterations = self.client.call("GET", path, read_timeout=10.0)
        hist = iterations if isinstance(iterations, list) else iterations.get("iterations", [])
        return len(hist)

    def _run_progress(self, goal: Dict[str, Any], st: Dict[str, Any]) -> str:
        done = max(0, self._hist_len(goal.get("pid")) - goal["start_hist"])
        if goal["gen_target"]:
            return f"{done}/{goal['gen_target']} generations scored"
        return f"{done} generations scored since gen {goal['start_gen']}"

    def _run_finished(self, goal: Dict[str, Any], st: Dict[str, Any]) -> bool:
        if goal["gen_target"] and self._hist_len(goal.get("pid")) >= goal["start_hist"] + goal["gen_target"]:
            return True
        if goal["ts_deadline"] and time.time() >= goal["ts_deadline"]:
            return True
        return False

    def _engine_entry(self, pid: str) -> Dict[str, Any]:
        """The engine pool entry for one project (multi-engine safe): the
        selected-engine snapshot only describes ONE engine — always read
        the goal's own row."""
        act = self._active_state()
        for e in act.get("engines", []) or []:
            if e.get("project_id") == pid:
                return {"generation": e.get("generation", 0),
                        "paused": e.get("paused", False),
                        "best": {"fitness": e.get("best_fitness")}}
        return {"generation": 0, "paused": False, "best": {}}

    def _run_summary(self, goal: Dict[str, Any], done: bool) -> str:
        st = self._engine_entry(goal.get("pid"))
        completed = max(0, self._hist_len(goal.get("pid")) - goal["start_hist"])
        best = st.get("best") or {}
        target = goal["gen_target"]
        head = (f"OK {'reached ' + str(target) + ' generations' if done and target else 'budget elapsed' if done else 'progress'} "
                f"— {completed} generation(s) scored "
                f"(best fitness {goal['start_best']} -> {best.get('fitness')})")
        lines = [head]
        if best.get("metrics"):
            lines.append("BEST METRICS " + " ".join(f"{k}={v}" for k, v in best["metrics"].items()))
        iterations = self.client.call("GET", f"/api/iterations?project_id={goal.get('pid')}", read_timeout=10.0)
        hist = iterations if isinstance(iterations, list) else iterations.get("iterations", [])
        if hist:
            counts: Dict[str, int] = {}
            tail = []
            for h in hist[-8:]:
                oc = str(h.get("outcome", "?"))
                counts[oc] = counts.get(oc, 0) + 1
                tail.append(f"  gen{h.get('iteration')}: {oc} {str(h.get('detail', ''))[:80]}")
            lines.append("OUTCOMES " + " ".join(f"{k}x{v}" for k, v in counts.items()))
            lines.extend(tail[-4:])
        return "\n".join(lines)

    def cmd_wait(self, arg: str) -> str:
        """Block until the in-flight run reaches its goal (or the timeout);
        on a forever run, snapshots progress after <secs> (default 60)."""
        goal = self._run_goal
        if not goal:
            raise KaiError("no run in progress — RUN first (STOP ended the last one?)")
        tokens = arg.split()
        secs = float(tokens[0]) if tokens and tokens[0].isdigit() else None
        has_target = bool(goal["gen_target"] or goal["ts_deadline"])
        timeout = secs or (3600.0 if has_target else 60.0)
        deadline = time.time() + timeout
        while True:
            st = self._engine_entry(goal["pid"])
            if self._run_finished(goal, st):
                self.client.call("POST", "/api/engine/pause",
                                 {"paused": True, "project_id": goal["pid"]}, read_timeout=60.0)
                self._run_goal = None
                self._save_run_goal()
                return self._run_summary(goal, done=True)
            if st.get("paused") or time.time() >= deadline:
                break
            time.sleep(2.0)
        st = self._engine_entry(goal["pid"])
        state = "paused externally" if st.get("paused") else "still running"
        return (f"OK {state} — " + self._run_progress(goal, st)
                + f", best fitness {(st.get('best') or {}).get('fitness')} — WAIT again or STOP")

    def cmd_pause(self, arg: str) -> str:
        body: Dict[str, Any] = {"paused": True}
        pid = self._parse_on(arg)
        if pid:
            body["project_id"] = pid
        elif self.project:
            body["project_id"] = self.project
        self.client.call("POST", "/api/engine/pause", body, read_timeout=60.0)
        return f"OK engine paused ({pid or self.project or 'all'})"

    def cmd_resume(self, arg: str) -> str:
        body: Dict[str, Any] = {"paused": False}
        pid = self._parse_on(arg)
        if pid:
            body["project_id"] = pid
        elif self.project:
            body["project_id"] = self.project
        self.client.call("POST", "/api/engine/pause", body, read_timeout=60.0)
        return f"OK engine running ({pid or self.project or 'all'})"

    def cmd_stop(self, arg: str) -> str:
        body: Dict[str, Any] = {}
        pid = self._parse_on(arg)
        if pid:
            body["project_id"] = pid
        elif self.project:
            body["project_id"] = self.project
        self.client.call("POST", "/api/engine/stop", body, read_timeout=60.0)
        self._run_goal = None
        self._save_run_goal()
        target = pid or self.project
        if target:
            return f"OK engine for project {target} stopped"
        return "OK engine stopped"

    def cmd_best(self, arg: str) -> str:
        pid = arg.strip() or self._need_project()
        b = _read_best(pid)
        head = f"OK {pid} gen={b['generation']} lang={b['language']} " \
               f"metrics: {' '.join(f'{k}={v}' for k, v in b['metrics'].items()) or '-'}\n" \
               f"PATH {b['source_path']}"
        return head + "\n" + b["code"]

    def cmd_smoke(self, arg: str) -> str:
        pid = self._parse_on(arg) or arg.strip() or self._need_project()
        res = self.client.call("POST", f"/api/projects/{pid}/smoke", read_timeout=600.0)
        if not res.get("ok"):
            reason = (res.get("reason") or res.get("error") or "unknown")[:400]
            tail = (res.get("stderr_tail") or "")[:500]
            return f"ERR smoke {res.get('stage', '?')}: {reason}\nSTDERR {tail}".rstrip()
        return f"OK smoke passed metrics: {' '.join(f'{k}={v}' for k, v in (res.get('metrics') or {}).items())}"

    def cmd_candidate(self, arg: str, lines: List[str]) -> str:
        lang = arg.strip() or None
        code = "\n".join(lines)
        if not code.strip():
            raise KaiError("CANDIDATE needs code lines ending with END")
        pid = self._need_project()
        act = self._active_state()
        if act.get("project_id") != pid:
            sw = self.client.call("POST", "/api/engine/switch", {"project_id": pid}, read_timeout=60.0)
            if not sw.get("ok"):
                raise KaiError(f"engine switch failed: {sw.get('error')}")
        body: Dict[str, Any] = {"code": code, "source": "kai", "project_id": pid}
        if lang:
            body["language"] = lang
        res = self.client.call("POST", "/api/queue/custom_code", body, read_timeout=60.0)
        if not res.get("ok"):
            raise KaiError(res.get("error", "queue failed"))
        return f"OK queued as generation {res.get('generation')} on {pid} — RESUME or RUN to execute"

    def cmd_snapshot(self, arg: str) -> str:
        tokens = arg.split()
        action = tokens[0].lower().rstrip(":,") if tokens else "list"
        pid: Optional[str] = None
        for i, t in enumerate(tokens):
            if t.upper() == "ON" and i + 1 < len(tokens):
                pid = tokens[i + 1].strip().lower()
        if action in ("list", "ls"):
            path = f"/api/snapshots?project_id={pid}" if pid else "/api/snapshots"
            res = self.client.call("GET", path, read_timeout=10.0)
            snaps = res.get("snapshots", [])
            if not snaps:
                return "OK no snapshots"
            rows = "\n".join(f"  {s.get('id')} {s.get('created', s.get('time', '?'))} {s.get('reason', '')}"
                             for s in snaps[:20])
            return f"OK {len(snaps)} snapshot(s):\n" + rows
        if action in ("take", "save"):
            body: Dict[str, Any] = {"reason": "kai"}
            if pid:
                body["project_id"] = pid
            res = self.client.call("POST", "/api/snapshots", body, read_timeout=30.0)
            if not res.get("ok"):
                raise KaiError(res.get("error", "snapshot failed"))
            return f"OK snapshot {res.get('id')}"
        if action in ("restore", "undo", "rollback"):
            sid = None
            for i, t in enumerate(tokens):
                if t.lower().rstrip(":,") in ("restore", "undo", "rollback") and i + 1 < len(tokens):
                    sid = tokens[i + 1].strip()
            if not sid:
                raise KaiError("SNAPSHOT RESTORE <id> — list snapshots first")
            body: Dict[str, Any] = {"id": sid}
            if pid:
                body["project_id"] = pid
            res = self.client.call("POST", "/api/snapshots/restore", body, read_timeout=30.0)
            if not res.get("ok"):
                raise KaiError(res.get("error", "restore failed"))
            return "OK restored"
        raise KaiError("SNAPSHOT [LIST|TAKE|RESTORE <id>] [ON <pid>]")

    def cmd_servers(self, arg: str) -> str:
        cfg = self.client.call("GET", "/api/config", read_timeout=10.0)
        llm = cfg.get("llm", {})
        servers = llm.get("servers", [])
        active = llm.get("active_ids", [])
        if not servers:
            return "OK no LLM servers configured (dashboard -> Settings -> LLM Servers)"
        # Live load when engines are up (per-server inflight/tps, aggregated
        # across every running project engine).
        load: Dict[str, Dict[str, Any]] = {}
        try:
            st = self.client.call("GET", "/api/llm/status", read_timeout=10.0)
            for r in st.get("servers") or []:
                load[str(r.get("id"))] = r
        except KaiError:
            pass
        free_total = 0
        lines = [f"OK {len(servers)} server(s), active: {' '.join(active) or '-'}"]
        for s in servers:
            sid = str(s.get("id"))
            row = load.get(sid) or {}
            inflight = int(row.get("inflight", 0) or 0)
            maxc = int(s.get("max_concurrent", 1) or 1)
            free = max(0, maxc - inflight)
            free_total += free if s.get("enabled", True) and sid in active else 0
            tps = row.get("tps")
            tier = row.get("tier") or s.get("tier") or "small"
            smart = row.get("smartness") or s.get("smartness") or {"tiny": 2.0, "small": 5.0, "large": 8.0}.get(tier, 5.0)
            cost_in = float(s.get("cost_in", 0) or 0)
            cost_out = float(s.get("cost_out", 0) or 0)
            ctx = s.get("context_window") or "?"
            cost = f"${cost_in}/{cost_out}" if (cost_in or cost_out) else "free"
            flags = []
            if not s.get("enabled", True):
                flags.append("disabled")
            elif sid not in active:
                flags.append("inactive")
            if row.get("banned"):
                flags.append("banned")
            if row.get("online") is False:
                flags.append("offline")
            lines.append(f"  {sid} | {s.get('label') or '-'} | {s.get('type')} | "
                         f"{s.get('url') or s.get('base_url') or '-'} | "
                         f"tier {tier} smart {smart} ctx {ctx} | $/Mtok {cost} | "
                         f"free {free}/{maxc} | tps {tps if tps is not None else '-'}"
                         + (f" | {' '.join(flags)}" if flags else ""))
        lines.append(f"FREE SLOTS {free_total}")
        return "\n".join(lines)

    def cmd_models(self, arg: str) -> str:
        """MODELS [skill] — the per-(model, skill) scoreboard: attempts /
        one-shot successes / wins / accumulated $, so an agent can see
        which model does which skill best (and what to stop using)."""
        skill = arg.strip().lower()
        data = self.client.call("GET", "/api/llm/modelstats", read_timeout=10.0)
        rows = [r for r in (data.get("rows") or [])
                if not skill or str(r.get("skill")) == skill]
        if not rows:
            return ("OK no stats yet — models earn them by working "
                    f"({'skill ' + skill if skill else 'any skill'})")
        lines = [f"OK {len(rows)} row(s)" + (f" for skill {skill}" if skill else "")
                 + " — cols: skill model tier attempts oneshots wins $"]
        for r in sorted(rows, key=lambda r: (-(r.get("oneshots") or 0), str(r.get("server_id")))):
            o = r.get("oneshots") or 0
            w = r.get("wins") or 0
            a = r.get("attempts") or 0
            lines.append(f"  {r.get('skill')} | {r.get('server_id')} | {r.get('tier')} | "
                         f"att={a} oneshot={o} win={w} | ${r.get('cost_usd', 0):.4f}"
                         + (f" | rate {r['oneshot_rate']}" if r.get("oneshot_rate") is not None else ""))
        return "\n".join(lines)

    def cmd_autofix(self, arg: str) -> str:
        """AUTOFIX [tries <n>] [repair <n|off>] — per-run compile-loop
        knobs for the session's project engine: how many deterministic
        autofix turns before giving up, and how many LLM repair attempts
        (0/off = deterministic autofix only, then fail). No args = show
        the current effective caps."""
        pid = self._need_project()
        tokens = arg.split()
        tries: Optional[int] = None
        repair: Optional[int] = None
        for i, t in enumerate(tokens):
            u = t.upper().rstrip(":,")
            if u in ("TRIES", "TURNS") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
                tries = int(tokens[i + 1])
            elif u == "REPAIR" and i + 1 < len(tokens):
                v = tokens[i + 1]
                if v.isdigit():
                    repair = int(v)
                elif v.upper().rstrip(":,") in ("OFF", "NO", "NONE"):
                    repair = 0
                elif v.upper().rstrip(":,") in ("ON", "YES"):
                    repair = None  # keep current/default
        body: Dict[str, Any] = {"project_id": pid}
        if tries is not None:
            body["tries"] = tries
        if repair is not None:
            body["repair"] = repair
        res = self.client.call("POST", "/api/engine/autofix", body, read_timeout=30.0)
        if not res.get("ok"):
            raise KaiError(res.get("error", "autofix settings failed"))
        eff = res.get("effective", {})
        repair_s = "OFF (deterministic only)" if eff.get("repair_max") == 0 \
            else f"{eff.get('repair_max')} LLM repair attempt(s)"
        return (f"OK {pid} compile loop: autofix turns {eff.get('max_tries')}, "
                f"{repair_s}")

    def cmd_estimate(self, arg: str) -> str:
        """ESTIMATE <in_tokens> [<out_tokens>]: per-server cost/time for one
        call of that size, ranked by smartness — pick the cheapest that can
        do the job."""
        tokens = arg.split()
        if not tokens or not tokens[0].isdigit():
            raise KaiError("ESTIMATE <in_tokens> [<out_tokens>]")
        tin = int(tokens[0])
        tout = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else 0
        cfg = self.client.call("GET", "/api/config", read_timeout=10.0)
        servers = (cfg.get("llm") or {}).get("servers", [])
        if not servers:
            return "OK no LLM servers configured"
        tps_map: Dict[str, float] = {}
        try:
            st = self.client.call("GET", "/api/llm/status", read_timeout=10.0)
            for r in st.get("servers") or []:
                if r.get("tps"):
                    tps_map[str(r.get("id"))] = float(r["tps"])
        except KaiError:
            pass
        rows = []
        for s in servers:
            sid = str(s.get("id"))
            tps = tps_map.get(sid, 10.0)
            out_t = tout or max(tin // 2, 1)
            secs = (tin + out_t) / max(tps, 0.1)
            usd = (tin / 1e6) * float(s.get("cost_in", 0) or 0) + (out_t / 1e6) * float(s.get("cost_out", 0) or 0)
            rows.append((s.get("tier") or "small", sid, secs, usd))
        tier_rank = {"tiny": 0, "small": 1, "large": 2}
        rows.sort(key=lambda r: (tier_rank.get(r[0], 1), r[2]))
        lines = [f"OK estimate for {tin} in / {out_t or max(tin // 2, 1)} out tokens (ranked by tier, then time):"]
        for tier, sid, secs, usd in rows:
            lines.append(f"  {sid} tier={tier} ~{secs:.0f}s ${usd:.5f}")
        best = rows[0]
        lines.append(f"CHEAPEST SMART-ENOUGH {best[1]} (tier {best[0]}, ~{best[2]:.0f}s, ${best[3]:.5f})")
        return "\n".join(lines)
    def cmd_baseline(self, arg: str, lines: List[str]) -> str:
        """Stage the starting program: BASELINE [lang] + code lines + END.
        GOAL then uses it as the baseline to improve (no code = the AI
        writes the baseline from scratch)."""
        code = "\n".join(lines)
        if not code.strip():
            raise KaiError("BASELINE needs code lines ending with END")
        self._baseline_code = code
        lang = arg.strip() or None
        return f"OK baseline staged ({len(code)} chars" + (f", lang {lang}" if lang else "") + ") — GOAL <words> next"

    def cmd_goal(self, arg: str) -> str:
        goal = arg.strip()
        if not goal:
            raise KaiError("GOAL <words...> [TEMP] — describe the program the AI must build")
        temp = False
        if goal.rsplit(" ", 1)[-1].strip().upper() == "TEMP":
            goal = goal.rsplit(" ", 1)[0].strip()
            temp = True
        if not goal:
            raise KaiError("GOAL <words...> [TEMP]")
        body: Dict[str, Any] = {"goal": goal}
        if self._baseline_code:
            body["code"] = self._baseline_code
        res = self.client.call("POST", "/api/projects/suggest", body, read_timeout=3600.0)
        if not res.get("ok"):
            raise KaiError(res.get("error", "suggest failed"))
        self._last_spec = res.get("suggested_spec", {})
        self._last_temp = temp
        rounds = (res.get("validation") or {}).get("rounds", 1)
        hint = "review the spec JSON, then ACCEPT <id> (or CREATE <id> <spec-json> to edit it first)"
        return (f"OK validated in {rounds} round(s){' — TEMP: the project lands in temp/ and is wiped at server close/next start' if temp else ''} — {hint}\n"
                + json.dumps(self._last_spec, indent=1))

    def cmd_accept(self, arg: str) -> str:
        pid = arg.strip().lower()
        if not pid:
            raise KaiError("ACCEPT <id> — the project id for the last GOAL result")
        if not self._last_spec:
            raise KaiError("no suggested spec — GOAL first")
        body: Dict[str, Any] = {"id": pid, "spec": self._last_spec}
        if getattr(self, "_last_temp", False):
            body["temp"] = True
        res = self.client.call("POST", "/api/projects", body, read_timeout=60.0)
        if not res.get("ok"):
            raise KaiError(res.get("error", "create failed"))
        warns = " ".join(res.get("warnings", []))
        self.project = pid
        return (f"OK created {pid}" + (f" WARNINGS {warns}" if warns else "")
                + (" — TEMP (wiped at server close/next start)" if body.get("temp") else "")
                + " — RUN to start evolving")

    def cmd_create(self, arg: str) -> str:
        pid, _, rest = arg.partition(" ")
        pid = pid.strip().lower()
        if not pid or not rest.strip():
            raise KaiError("CREATE <id> [TEMP] <spec-json>")
        temp = False
        if rest.strip().upper().startswith("TEMP "):
            temp = True
            rest = rest.strip()[5:]
        try:
            spec = json.loads(rest.strip())
        except ValueError as e:
            raise KaiError(f"spec is not valid JSON: {e}") from e
        body: Dict[str, Any] = {"id": pid, "spec": spec}
        if temp:
            body["temp"] = True
        res = self.client.call("POST", "/api/projects", body, read_timeout=60.0)
        if not res.get("ok"):
            raise KaiError(res.get("error", "create failed"))
        warns = " ".join(res.get("warnings", []))
        self.project = pid
        return (f"OK created {pid}" + (f" WARNINGS {warns}" if warns else "")
                + (" — TEMP (wiped at server close/next start)" if temp else ""))

    def cmd_forge(self, arg: str) -> str:
        """Generate n parallel drafts (default 3, max 12), each scored by the
        pipeline and ranked: FORGE [<n>] [ON <pid>] [GOAL <words...>]."""
        tokens = arg.split()
        goal_idx = None
        for i, t in enumerate(tokens):
            if t.upper().rstrip(":,") == "GOAL":
                goal_idx = i
                break
        if goal_idx is not None:
            request_words = tokens[goal_idx + 1:]
            tokens = tokens[:goal_idx]
        else:
            request_words = []
        n = 3
        pid: Optional[str] = None
        min_tier = "tiny"
        for i, t in enumerate(tokens):
            u = t.upper().rstrip(":,s")
            if u == "ON" and i + 1 < len(tokens):
                pid = tokens[i + 1].strip().lower()
            elif u == "TIER" and i + 1 < len(tokens) and tokens[i + 1].lower() in ("tiny", "small", "large"):
                min_tier = tokens[i + 1].lower()
            elif t.isdigit():
                n = int(t)
        n = max(1, min(n, 12))
        pid = pid or self._need_project()
        request = " ".join(request_words).strip()
        if not request:
            spec = self.client.call("GET", f"/api/projects/{pid}/spec", read_timeout=10.0).get("spec") or {}
            request = (spec.get("prompts") or {}).get("goal") or spec.get("description") or "Improve the program."
        res = self.client.call(
            "POST", "/api/swarm/start",
            {"kind": "code_forge", "project_id": pid, "request": request,
             "n": n, "max_concurrent": n, "min_tier": min_tier},
            read_timeout=60.0,
        )
        job_id = res.get("job_id")
        if not job_id:
            raise KaiError(res.get("error", "swarm start failed"))
        deadline = time.time() + 1200.0
        state = None
        job: Dict[str, Any] = {}
        while True:
            resp = self.client.call("GET", f"/api/swarm/{job_id}", read_timeout=10.0)
            job = resp.get("job", {})
            state = job.get("state")
            if state in ("done", "failed", "cancelled") or time.time() >= deadline:
                break
            time.sleep(2.0)
        if state != "done":
            return f"ERR forge {state}: {job.get('error') or 'no results'}"
        results = job.get("results", [])
        lines = [f"OK {len(results)} draft(s) ranked (pipeline-scored):"]
        for d in results:
            rank = d.get("rank")
            status = "ok" if d.get("ok") else f"failed({d.get('stage', '?')})"
            metrics = d.get("metrics") or {}
            reason = str(d.get("reason") or "")[:120]
            m = " ".join(f"{k}={v}" for k, v in metrics.items())
            line = f"  #{rank} {status} metrics: {m}"
            if reason:
                line += f" reason: {reason}"
            lines.append(line)
        for d in results:
            rank = d.get("rank")
            lines.append(f"===== DRAFT {rank} =====")
            lines.append(d.get("code") or "")
        return "\n".join(lines)

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, line: str, code_lines: Optional[List[str]] = None) -> str:
        word, rest = _split(line)
        cmd = _ALIAS_INDEX.get(word)
        if cmd is None:
            return f"ERR unknown command '{word}' — HELP for the reference"
        try:
            if cmd == "HELP":
                return "OK\n" + HELP_TEXT
            if cmd == "QUIT":
                return "OK bye"
            if cmd == "PROJECT":
                return self.cmd_project(rest)
            if cmd == "STATUS":
                return self.cmd_status(rest)
            if cmd == "MODELS":
                return self.cmd_models(rest)
            if cmd == "AUTOFIX":
                return self.cmd_autofix(rest)
            if cmd == "SPEC":
                return self.cmd_spec(rest)
            if cmd == "RUN":
                return self.cmd_run(rest)
            if cmd == "FORGE":
                return self.cmd_forge(rest)
            if cmd == "WAIT":
                return self.cmd_wait(rest)
            if cmd == "PAUSE":
                return self.cmd_pause(rest)
            if cmd == "RESUME":
                return self.cmd_resume(rest)
            if cmd == "STOP":
                return self.cmd_stop(rest)
            if cmd == "BEST":
                return self.cmd_best(rest)
            if cmd == "SMOKE":
                return self.cmd_smoke(rest)
            if cmd == "CANDIDATE":
                return self.cmd_candidate(rest, code_lines or [])
            if cmd == "BASELINE":
                return self.cmd_baseline(rest, code_lines or [])
            if cmd == "SNAPSHOT":
                return self.cmd_snapshot(rest)
            if cmd == "SERVERS":
                return self.cmd_servers(rest)
            if cmd == "MODELS":
                return self.cmd_models(rest)
            if cmd == "GOAL":
                return self.cmd_goal(rest)
            if cmd == "ESTIMATE":
                return self.cmd_estimate(rest)
            if cmd == "ACCEPT":
                return self.cmd_accept(rest)
            if cmd == "CREATE":
                return self.cmd_create(rest)
            return f"ERR unimplemented command '{cmd}'"
        except KaiError as e:
            return f"ERR {e}"
        except Exception as e:  # never crash the session on a command bug
            return f"ERR {type(e).__name__}: {e}"


def run_lines(session: KaiSession, lines: List[str]) -> str:
    """Execute a list of command lines, gathering CANDIDATE blocks.

    Returns the concatenated replies, one block per command."""
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r\n")
        word, _ = _split(line)
        if _ALIAS_INDEX.get(word) in ("CANDIDATE", "BASELINE"):
            code: List[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "END":
                code.append(lines[i].rstrip("\r\n"))
                i += 1
            i += 1  # skip END
            out.append(session.dispatch(line, code_lines=code))
        else:
            out.append(session.dispatch(line))
            i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------


def serve_stdio(host: str, port: int, auto_start: bool = True) -> None:
    """stdio transport: one command per line; CANDIDATE/BASELINE blocks end with END."""
    client = connect(host, port, auto_start=auto_start)
    session = KaiSession(client)
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        word, _ = _split(line)
        if _ALIAS_INDEX.get(word) in ("CANDIDATE", "BASELINE"):
            code: List[str] = []
            for raw2 in sys.stdin:
                line2 = raw2.rstrip("\r\n")
                if line2.strip() == "END":
                    break
                code.append(line2)
            reply = session.dispatch(line, code_lines=code)
        else:
            reply = session.dispatch(line)
        sys.stdout.write(reply + "\n")
        sys.stdout.flush()
        if _ALIAS_INDEX.get(word) == "QUIT":
            break


def handle_text(text: str, host: str = "127.0.0.1", port: int = 8080,
                auto_start: bool = False, session: Optional[KaiSession] = None) -> str:
    """HTTP transport: whole body as command lines.  `session` (when given)
    carries the caller's sticky session — the dashboard passes one so PROJECT
    selection persists across requests; stdio clients keep their own."""
    client = connect(host, port, auto_start=auto_start)
    sess = session or KaiSession(client)
    return run_lines(sess, text.splitlines())


def main(host: Optional[str] = None, port: Optional[int] = None) -> None:
    host = host or os.environ.get("KAISEN_KAI_HOST", "127.0.0.1")
    port = int(port or os.environ.get("KAISEN_KAI_PORT", 8080))
    auto_start = os.environ.get("KAISEN_KAI_NO_AUTOSTART") != "1"
    try:
        serve_stdio(host, port, auto_start=auto_start)
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        pass
    except KaiError as e:
        print(f"ERR {e}")


if __name__ == "__main__":
    main()
