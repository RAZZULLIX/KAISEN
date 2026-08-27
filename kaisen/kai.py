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
__version__ = "0.1.2-alpha"
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
                             after n SCORED generations; FOR <secs> = time
                             budget (paused time excluded — only burns while
                             the engine runs); WITH <k> = k LLM pipelines.
                             Both given? The run ends at whichever comes first.
  RUN ALL [FOR <secs>] [WITH <k>]
                             start every pool member at once, same budget and
                             pipeline count each — optional: everything about
                             multi-engine mode is opt-in
  BUDGET                      in-flight run's budget: scored so far + time left
  FORGE [<n>] [TIER <tiny|small|large>] [ON <pid>] [GOAL <words...>]
                             n parallel drafts, each pipeline-scored
  SCORE <path> [ON <pid>]     score any file through build+verify+score —
                             no engine, no run, no ceremony (audit in runs/)
  FUZZY <n> [ON <pid>]        opt-in diversity: seed each prompt with a random
                             one of the top N scored iterations instead of the
                             champion (0 = off; also feeds the mule its own
                             recent outcomes). Resets on engine restart.
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
    "SCORE": ["SCORE", "EVAL", "TESTFILE"],
    "FUZZY": ["FUZZY", "VARY", "DIVERSE"],
    "PAUSE": ["PAUSE", "HALT", "FREEZE"],
    "WAIT": ["WAIT", "SYNC", "AWAIT", "JOIN"],
    "BUDGET": ["BUDGET", "TIME", "REMAINING", "LEFT"],
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
        # A session carries EITHER one single-project goal (RUN) or a list of
        # them (RUN ALL) — never both.
        self._baseline_code: Optional[str] = None
        self._baseline_lang: Optional[str] = None
        self._last_spec: Optional[Dict[str, Any]] = None
        self._last_temp: bool = False
        self._run_goal: Optional[Dict[str, Any]] = None
        self._run_goals: List[Dict[str, Any]] = []
        loaded = self._load_run_state()
        if isinstance(loaded, list):
            self._run_goals = loaded
        elif isinstance(loaded, dict):
            self._run_goal = loaded

    def _load_run_state(self):
        """Persisted run goals: either a single dict (RUN) or a list (RUN ALL)."""
        data = load_json(_RUNS_FILE, None)
        if isinstance(data, dict):
            goals = data.get("goals")
            if isinstance(goals, list) and goals:
                return goals
            if data.get("pid"):
                return data
        return None

    def _save_run_state(self) -> None:
        if self._run_goals:
            save_json(_RUNS_FILE, {"goals": self._run_goals})
        elif self._run_goal:
            save_json(_RUNS_FILE, self._run_goal)
        else:
            try:
                _RUNS_FILE.unlink(missing_ok=True)
            except Exception:
                pass

    def _load_run_goal(self) -> Optional[Dict[str, Any]]:
        data = self._load_run_state()
        return data if isinstance(data, dict) else None

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
        entry = self._engine_entry(eng_pid) if eng_pid else None
        extras = []
        vr = (entry or {}).get("valid_rate") or {}
        if vr.get("valid_rate") is not None:
            extras.append(f"valid={vr['valid_rate'] * 100:.0f}%")
        af = (entry or {}).get("autofix") or {}
        if af:
            extras.append(f"autofix={af['max_tries']}/{af['repair_max']}")
        if (entry or {}).get("workers") is not None:
            extras.append(f"workers={entry['workers']}")
        if extras:
            extras = " (" + " ".join(extras) + ")"
        else:
            extras = ""
        lines.append(f"ENGINE {eng} gen={st.get('generation', '-')} paused={st.get('paused', '-')} project={eng_pid or '-'}{extras}")
        if (entry or {}).get("spec_revision"):
            lines[-1] += f" spec={entry['spec_revision']}"
        # Utilization: how much of the active LLM capacity the pool is using.
        # SERVERS shows per-server slots; this line sums it so a glance tells
        # you "you're running 3 of 12 possible pipelines" without doing math.
        try:
            llm_status = self.client.call("GET", "/api/llm/status", read_timeout=10.0)
            servers = llm_status.get("servers") or []
            total = sum(max(1, int(s.get("max_concurrent", 1) or 1))
                        for s in servers if s.get("enabled", True) and s.get("online") is not False)
            inflight = sum(int(s.get("inflight", 0) or 0) for s in servers if s.get("enabled", True))
            active_pipelines = sum(int(e.get("multi", 0) or 0)
                                   for e in (act.get("engines") or [])
                                   if e.get("engine_state") == "running")
            lines.append(f"LLM PIPELINES {active_pipelines}/{total} ({inflight} in flight)")
        except KaiError:
            pass
        best = st.get("best") or {}
        if best:
            lines.append(f"BEST fitness={best.get('fitness')} metrics: " +
                         " ".join(f"{k}={v}" for k, v in (best.get('metrics') or {}).items()))
        if self._run_goal:
            goal = self._run_goal
            prog = self._run_progress(goal, self._engine_entry(goal["pid"]))
            if goal["ts_deadline"]:
                prog += f", {max(0.0, goal['ts_deadline'] - time.time()):.0f}s left"
            lines.append("RUN " + prog + " (in flight — WAIT/BUDGET/STOP)")
        elif self._run_goals:
            lines.append(f"RUN ALL in flight: {len(self._run_goals)} projects")
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
        optional flags: RUN <n> = stop after n scored generations,
        RUN FOR <secs> = time budget (paused time excluded — the budget
        only burns while the engine runs), RUN WITH <k> = k parallel LLM
        pipelines.  When BOTH a count and a budget are given, the run ends
        at whichever comes first.  RUN ALL [FOR <secs>] [WITH <k>] starts
        every pool member at once.  Always returns immediately — use WAIT
        to synchronize, STATUS/BUDGET to watch, STOP to end."""
        tokens = arg.split()
        if tokens and tokens[0].strip().upper().rstrip(":,") in ("ALL", "EVERY", "EVERYTHING"):
            return self.cmd_run_all(" ".join(tokens[1:]))
        gen_target: Optional[int] = None
        budget: Optional[float] = None
        multi_k: Optional[int] = None
        pid: Optional[str] = None
        # One pass: flags may appear in any order; the budget value must
        # NOT also parse as a generation count ("RUN FOR 21600" = 21600s
        # budget, not 21600 generations).
        i = 0
        while i < len(tokens):
            t = tokens[i]
            u = t.upper().rstrip(":,s")
            if u in ("FOR", "BUDGET", "TIME"):
                if i + 1 < len(tokens) and tokens[i + 1].isdigit():
                    budget = float(tokens[i + 1])
                    i += 2
                    continue
                i += 1
                continue
            if u in ("WITH", "USING"):
                if i + 1 < len(tokens) and tokens[i + 1].isdigit():
                    multi_k = max(1, int(tokens[i + 1]))
                    i += 2
                    continue
                i += 1
                continue
            if u == "ON":
                if i + 1 < len(tokens):
                    pid = tokens[i + 1].strip().lower()
                    i += 2
                    continue
                i += 1
                continue
            if t.isdigit() and gen_target is None:
                gen_target = int(t)
            i += 1
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
        self._run_goals = []
        self._save_run_state()
        desc_parts = []
        desc_parts.append(f"{gen_target} generations" if gen_target else "forever")
        if budget:
            desc_parts.append(f"budget {budget:.0f}s")
        if multi_k:
            desc_parts.append(f"with {multi_k} LLMs")
        return (f"OK running {', '.join(desc_parts)} on {pid} in the background — WAIT to synchronize, "
                f"STATUS/BUDGET to watch, STOP to end")

    def cmd_run_all(self, arg: str) -> str:
        """Start every pool member at once: RUN ALL [FOR <secs>] [WITH <k>].
        Each project gets the same time budget (paused time excluded) and
        pipeline count; no per-project generation targets — keep it simple.
        Optional: only the pool is touched, nothing else is required."""
        tokens = arg.split()
        budget: Optional[float] = None
        multi_k: Optional[int] = None
        i = 0
        while i < len(tokens):
            t = tokens[i]
            u = t.upper().rstrip(":,s")
            if u in ("FOR", "BUDGET", "TIME"):
                if i + 1 < len(tokens) and tokens[i + 1].isdigit():
                    budget = float(tokens[i + 1])
                    i += 2
                    continue
                i += 1
                continue
            if u in ("WITH", "USING"):
                if i + 1 < len(tokens) and tokens[i + 1].isdigit():
                    multi_k = max(1, int(tokens[i + 1]))
                    i += 2
                    continue
                i += 1
                continue
            i += 1
        act = self._active_state()
        pool = act.get("engines") or []
        if not pool:
            raise KaiError("no engines in the pool — start projects first (RUN <pid> for each)")
        now = time.time()
        goals: List[Dict[str, Any]] = []
        for e in pool:
            pid = e.get("project_id")
            self.client.call("POST", "/api/engine/switch", {"project_id": pid}, read_timeout=60.0)
            if multi_k:
                self.client.call("POST", "/api/engine/multi", {"multi": multi_k, "project_id": pid}, read_timeout=60.0)
            self.client.call("POST", "/api/engine/pause", {"paused": False, "project_id": pid}, read_timeout=60.0)
            entry = self._engine_entry(pid)
            goals.append({
                "pid": pid,
                "gen_target": None,
                "ts_deadline": (now + budget) if budget else None,
                "start_gen": int(entry.get("generation", 0)),
                "start_hist": self._hist_len(pid),
                "start_best": (entry.get("best") or {}).get("fitness"),
            })
        self._run_goals = goals
        self._run_goal = None
        self._save_run_state()
        desc = f"{len(goals)} pool projects"
        if budget:
            desc += f", budget {budget:.0f}s each"
        if multi_k:
            desc += f", {multi_k} LLMs each"
        return (f"OK running all {desc} in the background — WAIT to synchronize, "
                f"STATUS/BUDGET to watch, STOP to end")

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
                        "best": {"fitness": e.get("best_fitness")},
                        "spec_revision": e.get("spec_revision"),
                        "autofix": e.get("autofix"),
                        "valid_rate": e.get("valid_rate"),
                        "fuzzy_top_n": e.get("fuzzy_top_n"),
                        "workers": e.get("workers")}
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
        if self._run_goals:
            return self._wait_multi(arg)
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
                self._run_goals = []
                self._save_run_state()
                return self._run_summary(goal, done=True)
            if st.get("paused"):
                # Paused time does not burn the wall-clock budget — slide the
                # deadline forward so the remaining budget survives the pause.
                if goal["ts_deadline"]:
                    goal["ts_deadline"] += 2.0
                    self._save_run_state()
                break
            if time.time() >= deadline:
                break
            time.sleep(2.0)
        st = self._engine_entry(goal["pid"])
        state = "paused externally" if st.get("paused") else "still running"
        return (f"OK {state} — " + self._run_progress(goal, st)
                + f", best fitness {(st.get('best') or {}).get('fitness')} — WAIT again or STOP")

    def _wait_multi(self, arg: str) -> str:
        """Block until EVERY pool member in the RUN ALL batch finishes (or
        the client-side timeout).  Paused engines don't burn their budget."""
        goals = list(self._run_goals)
        tokens = arg.split()
        secs = float(tokens[0]) if tokens and tokens[0].isdigit() else None
        has_target = any(g["ts_deadline"] for g in goals)
        timeout = secs or (3600.0 if has_target else 60.0)
        deadline = time.time() + timeout
        finished: List[Dict[str, Any]] = []
        while time.time() < deadline:
            for g in goals:
                st = self._engine_entry(g["pid"])
                if self._run_finished(g, st):
                    if g not in finished:
                        finished.append(g)
                elif st.get("paused") and g["ts_deadline"]:
                    g["ts_deadline"] += 2.0
                    self._save_run_state()
            if len(finished) == len(goals):
                for g in finished:
                    self.client.call("POST", "/api/engine/pause",
                                     {"paused": True, "project_id": g["pid"]}, read_timeout=60.0)
                self._run_goals = []
                self._run_goal = None
                self._save_run_state()
                rows = "; ".join(
                    f"{g['pid']} finished ({max(0, self._hist_len(g['pid']) - g['start_hist'])} scored)"
                    for g in finished)
                return f"OK all {len(goals)} runs complete — {rows}"
            time.sleep(2.0)
        return f"OK {len(finished)}/{len(goals)} finished — WAIT again or STOP"

    def cmd_budget(self, arg: str) -> str:
        """Show the session's in-flight run budget: generations scored so far
        vs target, and time remaining (paused time excluded via WAIT)."""
        if self._run_goals:
            lines = [f"OK {len(self._run_goals)} runs in flight:"]
            for g in self._run_goals:
                done = max(0, self._hist_len(g["pid"]) - g["start_hist"])
                rem = max(0.0, g["ts_deadline"] - time.time()) if g["ts_deadline"] else None
                lines.append(
                    f"  {g['pid']}: {done} scored"
                    + (f", {rem:.0f}s left" if rem is not None else ", no limit"))
            return "\n".join(lines)
        goal = self._run_goal
        if not goal:
            raise KaiError("no run in progress — RUN first (STOP ended the last one?)")
        done = max(0, self._hist_len(goal["pid"]) - goal["start_hist"])
        parts = []
        if goal["gen_target"]:
            parts.append(f"{done}/{goal['gen_target']} generations scored")
        else:
            parts.append("forever (no generation limit)")
        if goal["ts_deadline"]:
            parts.append(f"{max(0.0, goal['ts_deadline'] - time.time()):.0f}s remaining")
        return f"OK budget on {goal['pid']}: " + ", ".join(parts)

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
        if self._run_goals:
            # RUN ALL: stop every project in the batch, not just the selected one.
            for g in self._run_goals:
                self.client.call("POST", "/api/engine/stop",
                                 {"project_id": g["pid"]}, read_timeout=60.0)
            n = len(self._run_goals)
            self._run_goals = []
            self._run_goal = None
            self._save_run_state()
            return f"OK stopped all {n} run-goal engines"
        self.client.call("POST", "/api/engine/stop", body, read_timeout=60.0)
        self._run_goal = None
        self._run_goals = []
        self._save_run_state()
        target = pid or self.project
        if target:
            return f"OK engine for project {target} stopped"
        return "OK engine stopped"

    def cmd_best(self, arg: str) -> str:
        pid = arg.strip() or self._need_project()
        # Route through the API so temp projects (under temp/) resolve too —
        # the old filesystem path only knew projects/<id> and would miss them.
        res = self.client.call("GET", f"/api/projects/{pid}/best", read_timeout=30.0)
        if not res.get("ok"):
            raise KaiError(res.get("error", "best unavailable"))
        head = (f"OK {res['project_id']} gen={res.get('generation')} lang={res.get('language')} "
                f"metrics: {' '.join(f'{k}={v}' for k, v in (res.get('metrics') or {}).items()) or '-'}\n"
                f"PATH {res.get('source_path')}")
        return head + "\n" + (res.get("code") or "")

    def cmd_fuzzy(self, arg: str) -> str:
        """Opt-in prompt diversity (KAI FUZZY <n>): n = 0 keeps the champion
        as the prompt basis (default); n > 0 seeds each generation's prompt
        with a RANDOM one of the top n scored iterations.  Also feeds the
        mule its own recent outcomes.  Runtime only — resets on restart."""
        tokens = arg.split()
        n = 0
        for t in tokens:
            if t.isdigit():
                n = int(t)
                break
        pid = self._parse_on(arg) or self._need_project()
        res = self.client.call("POST", "/api/engine/fuzzy",
                               {"project_id": pid, "top_n": n}, read_timeout=30.0)
        if not res.get("ok"):
            raise KaiError(res.get("error", "fuzzy failed"))
        eff = int(res.get("top_n", 0))
        state = f"ON (top {eff})" if eff else "OFF (champion only)"
        return f"OK {pid} fuzzy prompt basis: {state}"

    def cmd_score(self, arg: str) -> str:
        """Score an arbitrary file through the project's full
        build+verify+score pipeline — no engine, no run, no ceremony.
        Returns metrics + timings; the audit copy lands under runs/score_*."""
        tokens = arg.split()
        if not tokens:
            raise KaiError("SCORE <path> [ON <pid>] — the source file to score")
        path = tokens[0]
        pid = self._parse_on(" ".join(tokens[1:])) or self._need_project()
        res = self.client.call("POST", f"/api/projects/{pid}/score",
                               {"path": path}, read_timeout=600.0)
        if not res.get("ok"):
            raise KaiError(res.get("error", "score failed"))
        metrics = res.get("metrics") or {}
        timings = res.get("timings") or {}
        parts = " ".join(f"{k}={v}" for k, v in metrics.items())
        tstr = f" ({', '.join(f'{k}={v:.1f}s' for k, v in timings.items())})" if timings else ""
        return f"OK {pid} scored {path}: {parts}{tstr} — audit: {res.get('gen_dir')}"

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
        writes the baseline from scratch).  The staged language is
        remembered and forwarded to the project builder — a staged python
        baseline must never silently become a C project."""
        code = "\n".join(lines)
        if not code.strip():
            raise KaiError("BASELINE needs code lines ending with END")
        self._baseline_code = code
        self._baseline_lang = arg.strip() or None
        return f"OK baseline staged ({len(code)} chars" + (f", lang {self._baseline_lang}" if self._baseline_lang else "") + ") — GOAL <words> next"

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
        if self._baseline_lang:
            body["language"] = self._baseline_lang
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
            if cmd == "SCORE":
                return self.cmd_score(rest)
            if cmd == "FUZZY":
                return self.cmd_fuzzy(rest)
            if cmd == "WAIT":
                return self.cmd_wait(rest)
            if cmd == "BUDGET":
                return self.cmd_budget(rest)
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
