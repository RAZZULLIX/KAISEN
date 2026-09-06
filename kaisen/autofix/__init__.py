# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Auto-fix build failures from compiler suggestions — per language.

A package, not a module: each language's compiler-message parser lives in
its own file (c_family.py, rust.py, go.py, …), and this __init__ wires the
public API:
  - C-family:   parse gcc's own "did you forget…?" / "did you mean…?" hints.
  - every other: the per-language nudge backends, which parse that
    compiler's diagnostics and do exactly what it suggests.

Both run on the BUILD step, one fix at a time, rebuilding after each,
reverting any fix that breaks a previously-working build.  None of this
replaces the compiler — it just does what the compiler told us to do.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .c_family import (
    MAX_FIX_TRIES,
    _AVX_INCLUDE_RE,
    _AVX512_HINT_RE,
    _IDENT_RE,
    _INCLUDE_HINT_RE,
    _LITERAL_NL_RE,
    _MEAN_HINT_RE,
    _POSIX_CLOCK_RE,
    _PRAGMA_AVX2,
    _PRAGMA_AVX512,
    _Q,
    _TARGET_MISMATCH_RE,
    _VEC_CONST_INIT_RE,
    _add_gnu_source,
    _add_include,
    _has_include,
    _replace_token,
    apply_fix,
    autofix_build,
    parse_hints,
)
from .engine import autofix_nudge, register_parser, _NUDGE_PARSERS
from . import rust, go, perl, shell, javascript, python

# Register each language's nudge backend with the shared engine.
register_parser("rust", rust.parse)
register_parser("go", go.parse)
register_parser("perl", perl.parse)
register_parser("shell", shell.parse)
register_parser("javascript", javascript.parse)
register_parser("typescript", javascript.parse)
register_parser("python", python.parse)

# Languages with a per-compiler nudge backend.
NUDGE_AVAILABLE = frozenset(_NUDGE_PARSERS)

_C_FAMILY = ("c", "cpp", "cc", "cxx", "cuda")


def resolve_mode(spec: Dict[str, Any], default: bool = True):
    """How autofix runs for a project.

    spec.skills.autofix_build may be:
      True / absent  -> the built-in default fixer
      False          -> disabled
      "path" (str)   -> a CUSTOM fixer script, project-relative
                        (e.g. "harness/autofix.py"), replacing the default
                        for this project only.
    Returns "off" | "default" | "python" | "nudge" | ("custom", path)."""
    try:
        af = (spec.get("skills") or {}).get("autofix_build", default)
    except Exception:
        af = default
    if af is None or af is False or af is True:
        mode = ("off" if af is False else "default")
    else:
        af = str(af).strip()
        if not af or af.lower() in ("default", "builtin", "true"):
            mode = "default"
        elif af.lower() in ("off", "false", "none", "no"):
            mode = "off"
        else:
            return ("custom", af)
    if mode == "default":
        from ..languages import normalize_lang
        lang = normalize_lang((spec.get("language") or ""))
        if lang == "python":
            return "python"
        if lang in _C_FAMILY:
            return "default"   # gcc-message fixer (c_family.py)
        return "nudge" if lang in NUDGE_AVAILABLE else "off"
    return mode
