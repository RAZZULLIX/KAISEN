# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Per-project runtime state: champion, generations, history, baselines.

State lives in projects/<id>/state.json and is mutated ONLY in the main
process (workers return results; the main process applies selection).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import goals
from .projects import Project
from .util import load_json, save_json

MAX_HISTORY = 500


class ProjectState:
    def __init__(self, project: Project):
        self.project = project
        self.data: Dict[str, Any] = load_json(project.state_file, {})
        self.data.setdefault("best", {})        # {fitness, metrics, code_path, artifact, generation}
        self.data.setdefault("baselines", {})   # {metric_key: value} (champion values)
        self.data.setdefault("history", [])
        self.data.setdefault("generation", 0)
        self.data.setdefault("last_improvement_gen", 0)
        self.data.setdefault("started_at", None)
        self.data.setdefault("llm_paused", False)
        self.data.setdefault("active", False)
        self.data.setdefault("baseline_code_path", None)
        self.data.setdefault("baseline_source_hash", None)  # drift guard
        # Goal latch: {signature, met, met_at, met_generation, detail}.
        # `signature` fingerprints the goal that fired, so EDITING the goal
        # re-arms a project instead of leaving it marked done forever.
        self.data.setdefault("goal", {})

    # -- accessors --------------------------------------------------------

    @property
    def best(self) -> Dict[str, Any]:
        return self.data["best"]

    @property
    def baselines(self) -> Dict[str, float]:
        return self.data["baselines"]

    @property
    def history(self) -> List[Dict[str, Any]]:
        return self.data["history"]

    @property
    def generation(self) -> int:
        return int(self.data["generation"])

    @property
    def last_improvement_gen(self) -> int:
        return int(self.data["last_improvement_gen"])

    @property
    def stagnation(self) -> int:
        return max(0, self.generation - self.last_improvement_gen)

    @property
    def paused(self) -> bool:
        return bool(self.data.get("llm_paused"))

    def set_paused(self, paused: bool) -> None:
        self.data["llm_paused"] = bool(paused)
        self.save()

    # -- goal -------------------------------------------------------------

    def goal_met(self, signature: str) -> bool:
        """True when THIS goal already fired (latched by signature)."""
        fired = self.data.get("goal") or {}
        return bool(fired.get("met")) and fired.get("signature") == signature

    def set_goal_met(self, signature: str, descriptor: Dict[str, Any], detail: str) -> None:
        self.data["goal"] = {
            "signature": signature,
            "met": True,
            "met_at": time.time(),
            "met_generation": int(descriptor.get("generation") or self.generation),
            "detail": detail,
        }

    def goal_done(self) -> bool:
        """True when the goal in the CURRENT spec has already fired.

        The restart paths consult this: a project whose goal is met is
        finished, so it must not silently start running again.
        """
        goal = (self.project.spec or {}).get("goal") or {}
        if not goals.when_of(goal):
            return False
        return self.goal_met(goals.signature(goal))

    def goal_snapshot(self) -> Dict[str, Any]:
        """The project's goal as the API/GUI sees it ({}, when it has none).

        A latch from an older goal is never reported as met: the signature
        must match the goal in the spec right now.
        """
        goal = (self.project.spec or {}).get("goal") or {}
        when = goals.when_of(goal)
        if not when:
            return {}
        sig = goals.signature(goal)
        fired = self.data.get("goal") or {}
        live = bool(fired.get("met")) and fired.get("signature") == sig
        return {
            "when": when,
            "then": goals.actions_of(goal),
            "met": live,
            "met_at": fired.get("met_at") if live else None,
            "met_generation": fired.get("met_generation") if live else None,
            "detail": fired.get("detail") if live else None,
        }

    def next_generation(self) -> int:
        self.data["generation"] = int(self.data.get("generation", 0)) + 1
        return int(self.data["generation"])

    def append_history(self, entry: Dict[str, Any]) -> None:
        self.data["history"].append(entry)
        if len(self.data["history"]) > MAX_HISTORY:
            self.data["history"] = self.data["history"][-MAX_HISTORY:]

    def set_best(self, entry: Dict[str, Any]) -> None:
        self.data["best"] = entry
        self.data["last_improvement_gen"] = int(self.data.get("generation", 0))
        # Refresh baselines from the new champion's metrics.
        if entry.get("metrics"):
            self.data["baselines"].update(
                {k: float(v) for k, v in entry["metrics"].items() if isinstance(v, (int, float))}
            )

    def save(self) -> None:
        save_json(self.project.state_file, self.data)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "generation": self.generation,
            "stagnation": self.stagnation,
            "last_improvement_gen": self.last_improvement_gen,
            "best": self.best,
            "baselines": self.baselines,
            "history": self.history[-40:],
            "paused": self.paused,
            "active": bool(self.data.get("active")),
            "started_at": self.data.get("started_at"),
            "goal": self.goal_snapshot(),
        }
