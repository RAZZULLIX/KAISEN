# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Auto-fix build failures from compiler suggestions.

gcc tells us exactly how to fix many "easy" errors:

  - "did you forget to include <stdio.h>?"   -> add the missing include
  - "did you mean 'printf'?"                 -> replace the offending token

The fixer applies one hint at a time, rebuilds after each, and stops as
soon as the build succeeds.  A hint that does not help is not re-applied;
the last failure (with everything applied) is reported so the pipeline can
fail with the real remaining error.  All fixes mutate the candidate source
in place, so the artifact (and the champion, if selected) is the fixed
code — exactly what compiled.

This is the "did you forget this? / did you mean that?" step.  It runs on
the BUILD step of every project unless the project (or the framework
default) turns it off.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_INCLUDE_HINT_RE = re.compile(r"did you forget to[^<]*include\s*<([^>]+)>", re.I)
# gcc quotes identifiers with ASCII or typographic quotes depending on locale.
_Q = "[‘'’]"
_MEAN_HINT_RE = re.compile(rf"{_Q}([A-Za-z_]\w*){_Q}[^\n]*?did you mean {_Q}([A-Za-z_]\w*){_Q}")
_TARGET_MISMATCH_RE = re.compile(
    r"target specific option mismatch|inlining failed in call to .always_inline"
    r"|AVX vector return without AVX enabled|AVX512 vector return without AVX512", re.I)
_AVX512_HINT_RE = re.compile(r"avx512|__m512|_mm512", re.I)
_VEC_CONST_INIT_RE = re.compile(r"error: initializer element is not constant", re.I)
_LITERAL_NL_RE = re.compile(r"extra tokens at end of #include directive|stray .\\", re.I)
_AVX_INCLUDE_RE = re.compile(r"#\s*include\s*<(avxintrin|fmaintrin|x86intrin)\.h>")
# AVX pragma must precede any include to affect glibc/immintrin parsing.
_PRAGMA_AVX2 = "#pragma GCC target(\"avx2,fma\")\n"
_PRAGMA_AVX512 = "#pragma GCC target(\"avx512f,avx512dq,avx512vl\")\n"

 # POSIX feature-test failures: with -std=c11 glibc hides clock_gettime /
# CLOCK_MONOTONIC unless _GNU_SOURCE (or _POSIX_C_SOURCE) precedes the
# includes.  LLM-generated C code hits this constantly.
_POSIX_CLOCK_RE = re.compile(
    rf"(implicit declaration of function\s+{_Q}?clock_gettime|{_Q}CLOCK_MONOTONIC{_Q}\s+undeclared)",
    re.I,
)

_IDENT_RE = re.compile(r"\b")

MAX_FIX_TRIES = 5


def parse_hints(stderr: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Extract (include_headers, token_fixes) from compiler stderr."""
    includes: List[str] = []
    means: List[Tuple[str, str]] = []
    if not stderr:
        return includes, means
    for hdr in _INCLUDE_HINT_RE.findall(stderr):
        hdr = hdr.strip()
        if hdr and hdr not in includes:
            includes.append(hdr)
    for bad, good in _MEAN_HINT_RE.findall(stderr):
        if bad != good and (bad, good) not in means:
            means.append((bad, good))
    return includes, means


def _has_include(source: str, header: str) -> bool:
    return re.search(rf"^\s*#\s*include\s*[<\"']{re.escape(header)}[>\"']", source, re.M) is not None


def _add_include(source: str, header: str) -> str:
    line = f"#include <{header}>"
    # Insert after the last include line when one exists…
    if re.search(r"^\s*#\s*include\b", source, re.M):
        lines = source.splitlines(keepends=True)
        last = 0
        for i, ln in enumerate(lines):
            if re.match(r"^\s*#\s*include\b", ln):
                last = i
        lines.insert(last + 1, line + "\n")
        return "".join(lines)
    # …otherwise after any leading preprocessor lines (#define _GNU_SOURCE
    # MUST precede the first include — an earlier gnu_source fix relies on
    # this), falling back to the very top.
    lines = source.splitlines(keepends=True)
    last = -1
    for i, ln in enumerate(lines):
        if re.match(r"^\s*#(define|pragma|ifn?def|if|else|endif)\b", ln):
            last = i
        elif ln.strip():
            break
    lines.insert(last + 1, line + "\n")
    return "".join(lines)


def _replace_token(source: str, bad: str, good: str) -> str:
    """Replace the first whole-word occurrence of `bad` with `good`."""
    pat = re.compile(rf"\b{re.escape(bad)}\b")
    m = pat.search(source)
    if not m:
        return source
    return source[:m.start()] + good + source[m.end():]

def _add_gnu_source(source: str) -> str:
    """POSIX feature-test fix: #define _GNU_SOURCE must precede every
    include; also add <time.h> for clock_gettime if missing."""
    if "#define _GNU_SOURCE" not in source:
        source = "#define _GNU_SOURCE\n" + source
    if not _has_include(source, "time.h"):
        source = _add_include(source, "time.h")
    return source


def apply_fix(source: str, kind: str, bad: str, good: str) -> str:
    if kind == "include":
        return _add_include(source, bad)
    if kind == "gnu_source":
        return _add_gnu_source(source)
    return _replace_token(source, bad, good)


def autofix_build(
    source_path: str | Path,
    build_cmd: List[str],
    run_build: Callable[[List[str]], Dict[str, Any]],
    max_tries: int = MAX_FIX_TRIES,
    apply_on_success: bool = False,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Try compiler-suggested fixes on a build.

    Runs `build_cmd`; on failure parses gcc hints and applies one fix to
    the source file in place, rebuilding after each.  With
    `apply_on_success=True` it ALSO applies hints found in warnings of a
    successful build — but only if the fixed rebuild succeeds; a fix that
    breaks a working build is reverted.

    Returns (ok, applied_fixes, last_run_result).
    """
    source_path = Path(source_path)
    applied: List[str] = []
    applied_headers = set()
    applied_means = set()
    applied_gnu = False
    applied_simd = False
    applied_vec_const = False
    applied_literal_nl = False
    applied_avx_inc = False
    result: Dict[str, Any] = {}

    for _ in range(max_tries):
        result = run_build(build_cmd)
        ok = bool(result.get("ok"))
        stderr = result.get("stderr") or ""
        includes, means = parse_hints(stderr)
        posix = bool(_POSIX_CLOCK_RE.search(stderr))
        simd_mismatch = bool(_TARGET_MISMATCH_RE.search(stderr))
        vec_const = bool(_VEC_CONST_INIT_RE.search(stderr))
        literal_nl = bool(_LITERAL_NL_RE.search(stderr))
        src_now = source_path.read_text(encoding="utf-8", errors="replace")
        # avxintrin.h was removed from modern GCC: a failing build whose
        # source still includes it gets rewritten to immintrin.h (superset).
        avx_inc = bool(_AVX_INCLUDE_RE.search(src_now))
        if ok and (not apply_on_success or not (includes or means or posix or simd_mismatch or vec_const or literal_nl)):
            return True, applied, result
        if not ok and not (includes or means or posix or simd_mismatch or vec_const or literal_nl or avx_inc):
            return False, applied, result
        fix: Optional[Tuple[str, ...]] = None
        for hdr in includes:
            if hdr not in applied_headers:
                fix = ("include", hdr)
                applied_headers.add(hdr)
                break
        if fix is None:
            for bad, good in means:
                if (bad, good) not in applied_means:
                    fix = ("mean", bad, good)
                    applied_means.add((bad, good))
                    break
        if fix is None and posix and not applied_gnu:
            fix = ("gnu_source",)
            applied_gnu = True
        if fix is None and simd_mismatch and not applied_simd:
            fix = ("simd_pragma", "avx512" if bool(_AVX512_HINT_RE.search(stderr)) else "avx2")
            applied_simd = True
        if fix is None and vec_const and not applied_vec_const:
            fix = ("vec_const",)
            applied_vec_const = True
        if fix is None and literal_nl and not applied_literal_nl:
            fix = ("literal_nl",)
            applied_literal_nl = True
        if fix is None and avx_inc and not applied_avx_inc:
            fix = ("avx_include",)
            applied_avx_inc = True
        if fix is None:
            return ok, applied, result
        original = source_path.read_text(encoding="utf-8", errors="replace")
        if fix[0] == "include":
            new_source = _add_include(original, fix[1])
            label = f"include <{fix[1]}>"
        elif fix[0] == "gnu_source":
            new_source = _add_gnu_source(original)
            label = "define _GNU_SOURCE + <time.h>"
        elif fix[0] == "simd_pragma":
            pragma = _PRAGMA_AVX512 if fix[1] == "avx512" else _PRAGMA_AVX2
            new_source = pragma + original
            label = f"pragma GCC target {fix[1]}"
        elif fix[0] == "vec_const":
            # `static const __m256 x = _mm256_set1_ps(...)` is invalid C —
            # drop the const (read-only usage is unaffected).
            new_source = re.sub(r"static\s+const\s+(__m(?:128|256|512))\s+", r"static \1 ", original)
            label = "drop const on static SIMD vector"
        elif fix[0] == "literal_nl":
            # The model wrote literal backslash-n instead of newlines.
            new_source = original.replace("\\n", "\n")
            label = "unescape literal \\n"
        elif fix[0] == "avx_include":
            new_source = _AVX_INCLUDE_RE.sub("#include <immintrin.h>", original)
            label = "avxintrin.h -> immintrin.h"
        else:
            new_source = _replace_token(original, fix[1], fix[2])
            label = f"{fix[1]} -> {fix[2]}"
        if new_source == original:
            continue
        source_path.write_text(new_source, encoding="utf-8")
        applied.append(label)
        probe = run_build(build_cmd)
        if probe.get("ok"):
            result = probe
            continue
        if ok:
            # The ORIGINAL build succeeded; this fix broke it — revert and
            # keep the working build.
            source_path.write_text(original, encoding="utf-8")
            applied.pop()
            return True, applied, result
        result = probe
    return bool(result.get("ok")), applied, result


def resolve_mode(spec: Dict[str, Any], default: bool = True):
    """How autofix runs for a project.

    spec.skills.autofix_build may be:
      True / absent  -> the built-in default fixer
      False          -> disabled
      "path" (str)   -> a CUSTOM fixer script, project-relative
                        (e.g. "harness/autofix.py"), replacing the default
                        for this project only.
    Returns "off" | "default" | "python" | ("custom", path)."""
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
        # The gcc-message fixer only makes sense for the C compiler family.
        # Python gets its own linter-backed fixer; other languages disable
        # the default (their diagnostics surface via the build stderr).
        from .languages import normalize_lang
        lang = normalize_lang((spec.get("language") or ""))
        if lang == "python":
            return "python"
        if lang not in ("c", "cpp", "cc", "cxx", "cuda"):
            return "off"
    return mode
