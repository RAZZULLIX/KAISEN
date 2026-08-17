# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Unified score model.

Whatever harness programs a project runs, their outputs are parsed into a
unified metric map {metric_key: value} using per-program parse rules.  The
project spec declares the metric schema (label, unit, direction, weight).
Fitness = weighted sum of per-metric goodness, where goodness normalizes a
value against a baseline (champion value, or 1.0 before a baseline exists):

    lower-better : goodness = baseline / value
    higher-better: goodness = value   / baseline

The champion is the candidate with the highest composite fitness, so every
project reduces to "maximize fitness" regardless of the underlying goal.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Parse rules
# ---------------------------------------------------------------------------


def parse_metrics(output: str, rules: List[Dict[str, Any]]) -> Dict[str, float]:
    """Apply a list of parse rules to harness output.

    Rule shapes (list in project spec `score[].parse`):
      {"type": "regex", "pattern": "size=(?P<compressed_size>\\d+)",
       "scale": {"compressed_size": 1.0}}
      {"type": "regex", "pattern": "speed=(?P<speed>[\\d.]+)x", "group": "speed",
       "metric": "speed", "scale": 1.0}
      {"type": "json"}                                  # output is JSON {metric: value}
      {"type": "line", "sep": "="}                      # key=value lines
    A single rule may yield multiple metrics (named groups map 1:1 to
    metric keys unless `group`+`metric` override is given).
    """
    metrics: Dict[str, float] = {}
    for rule in rules or []:
        rtype = rule.get("type", "regex")
        try:
            if rtype == "regex":
                pattern = rule["pattern"]
                # LLM-written specs over-escape character classes
                # (`[\\d.]+` instead of `[\d.]+`) — collapse double
                # backslashes so the pattern actually matches digits.
                if "\\\\" in pattern:
                    pattern = pattern.replace("\\\\", "\\")
                for m in re.finditer(pattern, output):
                    if "metric" in rule and rule.get("group"):
                        g = m.group(rule["group"])
                        if g is not None:
                            metrics[rule["metric"]] = _to_float(g, rule.get("scale"))
                    elif m.groupdict():
                        for key, val in m.groupdict().items():
                            if val is not None:
                                metrics[key] = _to_float(val, (rule.get("scale") or {}).get(key, 1.0))
                    elif m.groups():
                        metrics[rule.get("metric", "value")] = _to_float(m.group(1), rule.get("scale"))
            elif rtype == "json":
                data = json.loads(output.strip())
                if isinstance(data, dict):
                    for key, val in data.items():
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            metrics[key] = float(val)
            elif rtype == "line":
                sep = rule.get("sep", "=")
                for line in output.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if sep in line:
                        key, _, val = line.partition(sep)
                        key = key.strip()
                        val = val.strip()
                        if key and _is_number(val):
                            metrics[key] = float(val)
        except Exception:
            continue
    return metrics


def _to_float(val: str, scale: Any = None) -> float:
    v = float(val)
    if scale:
        v *= float(scale)
    return v


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Metric schema / fitness
# ---------------------------------------------------------------------------


def validate_metric_schema(schema: Dict[str, Any]) -> List[str]:
    """Return a list of errors for an invalid metric schema."""
    errors: List[str] = []
    if not isinstance(schema, dict) or not schema:
        return ["metrics: at least one metric required"]
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            errors.append(f"metrics.{key}: spec must be an object")
            continue
        if spec.get("direction") not in ("lower", "higher"):
            errors.append(f"metrics.{key}: direction must be 'lower' or 'higher'")
        try:
            float(spec.get("weight", 1.0))
        except (TypeError, ValueError):
            errors.append(f"metrics.{key}: weight must be numeric")
        if spec.get("constraint") is not None:
            try:
                float(spec["constraint"])
            except (TypeError, ValueError):
                errors.append(f"metrics.{key}: constraint must be numeric")
    return errors


def check_constraints(
    metrics: Dict[str, Any],
    schema: Dict[str, Any],
) -> List[str]:
    """Hard constraint violations: a metric with a `constraint` REJECTS
    the candidate outright — no fitness weighting can compensate.
    lower-better: value must be <= constraint. higher-better: >=."""
    violations: List[str] = []
    for key, spec in (schema or {}).items():
        if not isinstance(spec, dict) or spec.get("constraint") is None:
            continue
        value = metrics.get(key)
        if value is None:
            continue  # absent metric is the pipeline's problem, not a violation
        try:
            v = float(value)
            limit = float(spec["constraint"])
        except (TypeError, ValueError):
            continue
        if spec.get("direction") == "higher" and v < limit:
            violations.append(f"{key}={v} < constraint {limit}")
        elif spec.get("direction", "lower") == "lower" and v > limit:
            violations.append(f"{key}={v} > constraint {limit}")
    return violations


def compute_fitness(
    scores: Dict[str, float],
    schema: Dict[str, Any],
    baselines: Optional[Dict[str, float]] = None,
) -> Optional[float]:
    """Weighted composite fitness.  Returns None when no weighted metric has
    a score (project has nothing to optimize yet)."""
    baselines = baselines or {}
    total = 0.0
    any_used = False
    for key, spec in schema.items():
        if key not in scores:
            continue
        weight = float(spec.get("weight", 1.0))
        if weight == 0:
            continue
        value = float(scores[key])
        base = baselines.get(key)
        direction = spec.get("direction", "higher")
        if base is None or base <= 0:
            base = value
        if direction == "lower":
            goodness = base / value if value > 0 else 0.0
        else:
            goodness = value / base
        total += weight * goodness
        any_used = True
    return total if any_used else None


def metric_goodness(scores: Dict[str, float], schema: Dict[str, Any], baselines: Dict[str, float]) -> Dict[str, float]:
    """Per-metric goodness (for display in the metrics card)."""
    out: Dict[str, float] = {}
    for key, spec in schema.items():
        if key not in scores:
            continue
        value = float(scores[key])
        base = baselines.get(key) or value
        if spec.get("direction", "higher") == "lower":
            out[key] = base / value if value > 0 else 0.0
        else:
            out[key] = value / base
    return out


# ---------------------------------------------------------------------------
# Score types (selectable composite views — yelook-style)
#
# A project may declare multiple score types; the active one drives evolution
# and the GUI can switch which one is displayed.  Compose modes:
#   "product"       — product of the listed metrics (yelook global_product /
#                     distribution_product). Missing metric → no score.
#   "single"        — one metric's value.
#   "weighted_sum"  — sum of weight * value over the declared weights.
# Direction ("higher" | "lower") tells selection which way is better.
# ---------------------------------------------------------------------------


def evaluate_score_type(metrics: Dict[str, Any], type_spec: Dict[str, Any]) -> Optional[float]:
    """Compute a score from parsed metrics for one score-type spec."""
    compose = type_spec.get("compose", "weighted_sum")
    try:
        if compose == "product":
            keys = type_spec.get("metrics", [])
            vals = []
            for k in keys:
                if k not in metrics or metrics[k] is None:
                    return None
                vals.append(float(metrics[k]))
            if not vals:
                return None
            score = 1.0
            for v in vals:
                score *= v
        elif compose == "single":
            k = type_spec.get("metric")
            if k not in metrics or metrics[k] is None:
                return None
            score = float(metrics[k])
        else:  # weighted_sum
            total = 0.0
            used = False
            for k, w in (type_spec.get("weights") or {}).items():
                if k not in metrics or metrics[k] is None:
                    continue
                weight = float(w.get("weight", 1.0)) if isinstance(w, dict) else float(w)
                if weight == 0:
                    continue
                total += weight * float(metrics[k])
                used = True
            if not used:
                return None
            score = total
    except (TypeError, ValueError):
        return None
    scale = type_spec.get("scale")
    if scale:
        try:
            score *= float(scale)
        except (TypeError, ValueError):
            pass
    return score


def synthesize_score_type(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Default score type when a project declares no 'scores' section:
    weighted_sum over the metric schema."""
    weights = {}
    directions = []
    for key, spec in schema.items():
        weights[key] = {"weight": float(spec.get("weight", 1.0))}
        directions.append(spec.get("direction", "higher"))
    direction = "lower" if directions and all(d == "lower" for d in directions) else "higher"
    return {
        "compose": "weighted_sum",
        "weights": weights,
        "direction": direction,
    }
