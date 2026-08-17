# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""UI preferences — the user's GUI shape, editable by hand or by the
config agent, with a single committed defaults standard to revert to."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .config import FRAMEWORK_ROOT

PREFS_FILE = FRAMEWORK_ROOT / "ui_prefs.json"

DEFAULTS: Dict[str, Any] = {
    "theme": {
        "accent": "#00e87c",
        "accent2": "#00c468",
        "danger": "#ff4b6a",
        "warning": "#ffbe3d",
        "density": "comfortable",       # compact | comfortable | spacious
        "radius": 12,
    },
    "layout": {
        "show_kpi_pills": True,
        "show_iteration_dashboard": True,
        "compact_worker_cards": False,
    },
    "onboarding": {"skipped_at": None},
}

_ALLOWED: Dict[str, set] = {
    "theme.density": {"compact", "comfortable", "spacious"},
    "theme.radius": {8, 12, 16, 20},
    "layout.show_kpi_pills": {True, False},
    "layout.show_iteration_dashboard": {True, False},
    "layout.compact_worker_cards": {True, False},
}


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_prefs() -> Dict[str, Any]:
    prefs = json.loads(json.dumps(DEFAULTS))
    _deep_merge(prefs, _load())
    return prefs


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def save_prefs(prefs: Dict[str, Any]) -> None:
    PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def get_pref(path: str) -> Any:
    prefs = load_prefs()
    node: Any = prefs
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_pref(path: str, value: Any) -> str:
    """Set one preference, validated. Returns "" or an error string."""
    if path in _ALLOWED and value not in _ALLOWED[path]:
        return f"value '{value}' not allowed for {path} (allowed: {sorted(str(v) for v in _ALLOWED[path])})"
    if path.startswith("theme.accent") or path.startswith("theme.") and path.endswith(("accent", "accent2", "danger", "warning")):
        if not isinstance(value, str) or not (len(value) == 7 and value.startswith("#")):
            return f"{path} must be a #RRGGBB color"
    parts = path.split(".")
    if not parts or parts[0] not in ("theme", "layout", "onboarding"):
        return f"unknown preference namespace: {parts[0] if parts else path}"
    prefs = _load()
    node: Any = prefs
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            return f"path conflict at '{part}'"
    node[parts[-1]] = value
    save_prefs(prefs)
    return ""


def reset_prefs() -> None:
    save_prefs(json.loads(json.dumps(DEFAULTS)))


def apply_theme_to_css_vars() -> Dict[str, str]:
    """Return {css_var: value} the GUI applies to :root at load."""
    prefs = load_prefs()
    t = prefs.get("theme", {})
    return {
        "--accent": str(t.get("accent", DEFAULTS["theme"]["accent"])),
        "--accent2": str(t.get("accent2", DEFAULTS["theme"]["accent2"])),
        "--danger": str(t.get("danger", DEFAULTS["theme"]["danger"])),
        "--warning": str(t.get("warning", DEFAULTS["theme"]["warning"])),
        "--radius-lg": f"{int(t.get('radius', 12)) + 4}px",
    }
