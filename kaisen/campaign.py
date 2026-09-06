# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Campaign driver — run every project in the pool to N generations each,
respecting parallel-engine capacity, with resumable state and bug capture.

Semantics match the KAI command `RUN <n> ON <pid>`: progress is counted in
iteration-history entries (every generation lands there — scored or not),
so "50 generations" means 50 new history rows, exactly what RUN 50 would
deliver.

State: <root>/campaign.json
    {
      "target_gens": 50,
      "max_parallel": 4,
      "created": ts,
      "projects": {
        "<pid>": {"state": "pending|running|done|failed",
                  "start_hist": N,     # history length at start (resume anchor)
                  "scored": N,         # completed history rows
                  "best_ms": X|null,   # champion time_ms (when available)
                  "captured_up_to": N, # last bug-captured iteration
                  "started": ts, "finished": ts}
      }
    }

Resumability: on startup reconcile() re-derives reality from the server —
engines survive restarts via engine_pool.json, so a driver crash mid-run
loses nothing: a "running" project whose engine has stopped goes back to
"pending" and resumes from its start_hist anchor (no double counting).

Bug capture: every tick, new history entries with stage-failure outcomes
for running projects are appended to <root>/campaign_bugs.jsonl —
machine-readable rows whose `detail` carries the FUZZ MISMATCH / build /
score diagnostics for the triage loop.

CLI:  python3 -m kaisen.campaign [TARGET n] [PARALLEL k] [POLL s]
      python3 -m kaisen.campaign STATUS     # one-line progress report
      python3 -m kaisen.campaign STOP       # pause every campaign engine
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import FRAMEWORK_ROOT

STATE_FILE = FRAMEWORK_ROOT / "campaign.json"
BUGS_FILE = FRAMEWORK_ROOT / "campaign_bugs.jsonl"

# history outcomes that are stage failures worth capturing for triage
FAILURE_OUTCOMES = {
    "build_fail", "verify_fail", "score_fail", "rejected_dangerous",
    "no_code", "cancelled",
}


class CampaignDriver:
    def __init__(self, client, state_path: Optional[Path] = None,
                 target_gens: int = 50, max_parallel: int = 4,
                 poll_s: float = 10.0):
        self.client = client
        self.state_path = Path(state_path) if state_path else STATE_FILE
        self.poll_s = poll_s
        self.state: Dict[str, Any] = {
            "target_gens": target_gens,
            "max_parallel": max_parallel,
            "created": time.time(),
            "projects": {},
        }
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and "projects" in loaded:
                    self.state.update(loaded)
            except (ValueError, OSError):
                pass  # corrupt state -> start fresh; server is source of truth

    # -- persistence ------------------------------------------------------- #

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    # -- server access ------------------------------------------------------ #

    def _active(self) -> Dict[str, Any]:
        res = self.client.call("GET", "/api/active", read_timeout=15.0)
        return res if isinstance(res, dict) else {}

    def _engines(self) -> Dict[str, Dict[str, Any]]:
        act = self._active()
        out: Dict[str, Dict[str, Any]] = {}
        for e in act.get("engines") or []:
            pid = str(e.get("project_id") or "")
            if pid:
                out[pid] = e
        return out

    def _iterations(self, pid: str) -> List[Dict[str, Any]]:
        res = self.client.call(
            "GET", f"/api/iterations?project_id={pid}", read_timeout=15.0)
        if isinstance(res, list):
            return res
        return (res or {}).get("iterations") or []

    def _best_ms(self, pid: str) -> Optional[float]:
        try:
            res = self.client.call(
                "GET", f"/api/projects/{pid}/best", read_timeout=15.0)
        except Exception:
            return None
        metrics = (res or {}).get("metrics") or {}
        v = metrics.get("time_ms")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # -- lifecycle ---------------------------------------------------------- #

    def register(self, pids: List[str]) -> int:
        """Add projects to the campaign (existing entries are kept)."""
        added = 0
        for pid in pids:
            if pid not in self.state["projects"]:
                self.state["projects"][pid] = {
                    "state": "pending", "start_hist": 0, "scored": 0,
                    "best_ms": None, "captured_up_to": 0,
                    "started": None, "finished": None,
                }
                added += 1
        self.save()
        return added

    def reconcile(self) -> None:
        """Re-derive state from the server (call at startup)."""
        engines = self._engines()
        for pid, p in self.state["projects"].items():
            if p["state"] == "done":
                continue
            eng = engines.get(pid)
            running = bool(eng and eng.get("engine_state") == "running"
                           and not eng.get("paused"))
            if p["state"] == "running" and not running:
                # engine died/stopped mid-run -> resumable from the anchor
                p["state"] = "pending"
            elif p["state"] == "pending" and running:
                # someone started it out-of-band; adopt with current anchor
                p["state"] = "running"
                if not p.get("start_hist"):
                    p["start_hist"] = len(self._iterations(pid))
        self.save()

    def _start(self, pid: str) -> None:
        p = self.state["projects"][pid]
        # Anchor BEFORE the engine starts: nothing can score in between, so
        # progress accounting is exact (and a resume keeps the old anchor).
        if not p.get("start_hist") and p.get("scored", 0) == 0:
            p["start_hist"] = len(self._iterations(pid))
        res = self.client.call(
            "POST", "/api/engine/switch", {"project_id": pid}, read_timeout=60.0)
        if not (res or {}).get("ok"):
            raise RuntimeError(f"engine switch failed for {pid}: {res}")
        self.client.call(
            "POST", "/api/engine/pause", {"paused": False, "project_id": pid},
            read_timeout=60.0)
        p["state"] = "running"
        p["started"] = time.time()

    def _finish(self, pid: str, state: str) -> None:
        p = self.state["projects"][pid]
        try:
            self.client.call(
                "POST", "/api/engine/pause", {"paused": True, "project_id": pid},
                read_timeout=60.0)
        except Exception:
            pass
        p["state"] = state
        p["finished"] = time.time()
        if p.get("best_ms") is None:
            p["best_ms"] = self._best_ms(pid)

    # -- bug capture -------------------------------------------------------- #

    def _capture_bugs(self, pid: str, iters: Optional[List[Dict[str, Any]]] = None) -> None:
        p = self.state["projects"][pid]
        if iters is None:
            iters = self._iterations(pid)
        upto = int(p.get("captured_up_to") or 0)
        rows: List[str] = []
        for it in iters[upto:]:
            outcome = str(it.get("outcome") or "")
            if outcome in FAILURE_OUTCOMES:
                row = {
                    "ts": time.time(),
                    "project": pid,
                    "iteration": it.get("iteration"),
                    "outcome": outcome,
                    "detail": (it.get("detail") or "")[:1000],
                }
                rows.append(json.dumps(row))
        if rows:
            with open(BUGS_FILE, "a", encoding="utf-8") as f:
                f.write("\n".join(rows) + "\n")
        p["captured_up_to"] = len(iters)

    # -- main loop ----------------------------------------------------------- #

    def tick(self) -> None:
        """One poll cycle: finalize finished runs, capture bugs, fill slots."""
        engines = self._engines()
        target = int(self.state.get("target_gens") or 50)
        running = 0
        for pid, p in self.state["projects"].items():
            if p["state"] == "running":
                eng = engines.get(pid)
                alive = bool(eng and eng.get("engine_state") == "running"
                             and not eng.get("paused"))
                if not alive:
                    p["state"] = "pending"  # crashed/stopped -> resumable
                    continue
                running += 1
                iters = self._iterations(pid)
                start = int(p.get("start_hist") or 0)
                # Progress = SCORED generations only (fitness measured).
                # Failed iterations still land in history, but burning the
                # budget on failures is exactly what RUN <n> semantics forbid.
                p["scored"] = max(0, sum(1 for it in iters[start:]
                                         if it.get("fitness") is not None))
                self._capture_bugs(pid, iters)
                if p["scored"] >= target:
                    self._finish(pid, "done")
                    running -= 1

        slots = int(self.state.get("max_parallel") or 4) - running
        for pid, p in self.state["projects"].items():
            if slots <= 0:
                break
            if p["state"] != "pending":
                continue
            try:
                self._start(pid)
                slots -= 1
            except Exception as e:
                p["state"] = "failed"
                p["error"] = str(e)[:300]
                p["finished"] = time.time()
        self.save()

    def run(self, quiet: bool = False) -> Dict[str, Any]:
        """Run until every project is done/failed (or Ctrl-C)."""
        self.reconcile()
        try:
            while True:
                self.tick()
                if not quiet:
                    print(self.report(), flush=True)
                if all(p["state"] in ("done", "failed")
                       for p in self.state["projects"].values()):
                    break
                time.sleep(self.poll_s)
        except KeyboardInterrupt:
            if not quiet:
                print("campaign interrupted — state saved; rerun to resume")
        return self.summary()

    # -- reporting ------------------------------------------------------------ #

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for p in self.state["projects"].values():
            counts[p["state"]] = counts.get(p["state"], 0) + 1
        return {
            "target_gens": self.state.get("target_gens"),
            "projects": len(self.state["projects"]),
            "counts": counts,
            "best_ms": {pid: p.get("best_ms")
                        for pid, p in sorted(self.state["projects"].items())
                        if p.get("best_ms") is not None},
        }

    def report(self) -> str:
        s = self.summary()
        c = s["counts"]
        return (f"campaign {s['target_gens']} gens x {s['projects']} projects — "
                f"running={c.get('running', 0)} pending={c.get('pending', 0)} "
                f"done={c.get('done', 0)} failed={c.get('failed', 0)}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _client():
    from .kai import KaiClient
    cfg_port = 8080
    cfg_path = FRAMEWORK_ROOT / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg_port = int((cfg.get("server") or {}).get("port", 8080))
        except (ValueError, OSError):
            pass
    return KaiClient(f"http://127.0.0.1:{cfg_port}")


def main(argv: List[str]) -> int:
    tokens = [a for a in argv if a]
    mode = "run"
    if tokens and tokens[0].upper() in ("STATUS", "REPORT"):
        mode = "status"
        tokens = tokens[1:]
    elif tokens and tokens[0].upper() == "STOP":
        mode = "stop"
        tokens = tokens[1:]
    elif tokens and tokens[0].upper() == "RUN":
        tokens = tokens[1:]

    target, parallel, poll = 50, 4, 10.0
    i = 0
    while i < len(tokens):
        u = tokens[i].upper().rstrip(":,")
        if u == "TARGET" and i + 1 < len(tokens):
            target = int(tokens[i + 1]); i += 2
        elif u == "PARALLEL" and i + 1 < len(tokens):
            parallel = int(tokens[i + 1]); i += 2
        elif u == "POLL" and i + 1 < len(tokens):
            poll = float(tokens[i + 1]); i += 2
        else:
            print(f"ERR unknown token {tokens[i]!r} — "
                  f"usage: campaign [TARGET n] [PARALLEL k] [POLL s] | STATUS | STOP")
            return 2

    client = _client()
    drv = CampaignDriver(client, target_gens=target, max_parallel=parallel,
                         poll_s=poll)

    if mode == "status":
        drv.reconcile()
        print(drv.report())
        for pid, p in sorted(drv.state["projects"].items()):
            print(f"  {pid:28s} {p['state']:8s} scored={p.get('scored', 0):3d} "
                  f"best_ms={p.get('best_ms')}")
        return 0

    if mode == "stop":
        drv.reconcile()
        for pid, p in drv.state["projects"].items():
            if p["state"] == "running":
                try:
                    drv.client.call("POST", "/api/engine/pause",
                                    {"paused": True, "project_id": pid},
                                    read_timeout=30.0)
                except Exception as e:
                    print(f"WARN pause {pid}: {e}")
        drv.save()
        print("OK all campaign engines paused (state kept — rerun to resume)")
        return 0

    # register every non-temp project in the pool that isn't already tracked
    res = client.call("GET", "/api/projects", read_timeout=15.0)
    pids = [str(p.get("id")) for p in (res or {}).get("projects") or []
            if p.get("id") and not p.get("temp")]
    drv.register(pids)
    print(f"campaign: {len(drv.state['projects'])} projects, "
          f"target={target}, parallel={parallel}", flush=True)
    drv.run(quiet=False)
    s = drv.summary()
    print("\nFINAL " + json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
