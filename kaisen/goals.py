# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Project goals: a success condition plus the actions that fire when it is
met.

A spec may declare::

    "goal": {
        "when": {"metric": "proved_open", "op": ">=", "value": 2},
        "then": ["stop", "ping"]        # default when `then` is omitted
    }

`when` is ONE comparison against the champion's metrics, or against the
reserved `fitness` / `generation`.  An absent (or empty) `when` means the
project has no goal and nothing ever fires.

`then` is an ordered list of action names.  Today:

* ``stop`` — the project is done: the engine stops it and it is NOT
  resumed on the next start (the run is cleared, not snapshotted).
* ``ping`` — say so: always a log line, plus Telegram when that channel is
  enabled.

The mechanic is declarative on purpose.  `ACTIONS` is the registry a spec
is validated against, so a half-typed goal fails loudly instead of
silently doing nothing, and a new action is one entry here plus its
handler at the fire site — never a special case in the engine loop.
"""

from __future__ import annotations

import hashlib
import json
import operator
from typing import Any, Dict, List, Optional

# Comparison operators a `when` clause may use.
OPS = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}

# Names that resolve outside the project's own metrics dict.
FITNESS = "fitness"
GENERATION = "generation"
RESERVED_METRICS = (FITNESS, GENERATION)

# Actions a goal may fire.  `stop` ends the project (the default), `ping`
# reports it.  Adding one means adding it here and handling it in
# Engine._fire_goal().
STOP = "stop"
PING = "ping"
ACTIONS = (STOP, PING)
DEFAULT_ACTIONS: List[str] = [STOP, PING]


def when_of(goal: Any) -> Optional[Dict[str, Any]]:
    """The condition of a goal dict, or None when the project has no goal."""
    if not isinstance(goal, dict):
        return None
    when = goal.get("when")
    return when if isinstance(when, dict) and when else None


def actions_of(goal: Any) -> List[str]:
    """The actions to fire for a goal ([] when the project has no goal).

    An absent — or empty — `then` means the default pair: stop the project
    and ping.  Keeping the empty list equivalent to omitting it means a
    spec that was written and re-read through a UI (where defaults appear
    as literal values) can never silently lose its stop action.
    """
    if not isinstance(goal, dict) or not when_of(goal):
        return []
    then = goal.get("then")
    if not then:
        return list(DEFAULT_ACTIONS)
    if isinstance(then, str):
        return [then]
    if isinstance(then, list):
        return [str(a) for a in then]
    return []


def signature(goal: Any) -> str:
    """Stable fingerprint of a goal.

    Stored with the latch, so EDITING the goal (a higher target, another
    metric) re-arms a project that already fired instead of leaving it
    marked done forever.
    """
    payload = json.dumps(goal or {}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def read_metric(metric: str, metrics: Dict[str, Any], fitness: Optional[float],
                generation: int) -> Optional[float]:
    """Current value of the goal's metric, or None when unmeasured yet.

    An unmeasured metric is simply not met — never an error: the goal is
    checked after every evaluation, and the first evaluations of a project
    may not have produced that metric yet.
    """
    if metric == FITNESS:
        return None if fitness is None else float(fitness)
    if metric == GENERATION:
        return float(generation)
    value = (metrics or {}).get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def evaluate(goal: Any, metrics: Dict[str, Any], fitness: Optional[float],
             generation: int) -> Optional[Dict[str, Any]]:
    """Return a descriptor when the goal is met, else None.

    The descriptor carries what was compared and what was seen, so the
    history entry, the log line and the ping all say the same true thing.
    """
    when = when_of(goal)
    if not when:
        return None
    metric = str(when.get("metric") or "")
    op = str(when.get("op") or "")
    cmp = OPS.get(op)
    if cmp is None:
        return None
    target = when.get("value")
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        return None
    seen = read_metric(metric, metrics, fitness, generation)
    if seen is None:
        return None
    try:
        met = bool(cmp(seen, float(target)))
    except TypeError:
        return None
    if not met:
        return None
    return {"metric": metric, "op": op, "value": float(target), "seen": seen,
            "generation": int(generation)}


def describe(met: Dict[str, Any]) -> str:
    """Human phrasing of a met goal, e.g. ``proved_open >= 2 (seen 3)``."""
    seen = met.get("seen")
    if isinstance(seen, float) and seen.is_integer():
        seen = int(seen)
    return f"{met.get('metric')} {met.get('op')} {met.get('value'):g} (seen {seen})"


def validate_goal(goal: Any, metrics: Dict[str, Any]) -> List[str]:
    """Structural validation of a spec's `goal` block.

    Same contract as validate_spec(): a list of human-readable errors,
    empty == valid.
    """
    errors: List[str] = []
    if goal is None:
        return errors
    if not isinstance(goal, dict):
        return ["goal: must be an object"]
    when = goal.get("when")
    then = goal.get("then")
    if when is None:
        if then:
            errors.append("goal.then: needs goal.when (a goal with no condition never fires)")
        return errors
    if not isinstance(when, dict):
        return ["goal.when: must be an object"]
    metric = when.get("metric")
    if not isinstance(metric, str) or not metric:
        errors.append("goal.when.metric: required (a metric key, or 'fitness' / 'generation')")
    elif metric not in RESERVED_METRICS and metric not in (metrics or {}):
        declared = ", ".join(sorted(metrics or {})) or "none"
        errors.append(f"goal.when.metric: unknown metric '{metric}' (declared: {declared}; "
                      f"or use 'fitness' / 'generation')")
    op = when.get("op")
    if op not in OPS:
        errors.append(f"goal.when.op: must be one of {', '.join(OPS)}")
    value = when.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append("goal.when.value: must be a number (the target to compare against)")
    if then is not None:
        if isinstance(then, str):
            names = [then]
        elif isinstance(then, list):
            names = then
        else:
            errors.append("goal.then: must be a list of action names")
            names = []
        for name in names:
            if not isinstance(name, str) or name not in ACTIONS:
                errors.append(f"goal.then: unknown action {name!r} (supported: {', '.join(ACTIONS)})")
    return errors
