# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Per-compiler "nudge" engine: parse stderr, do what the compiler says.

Each language backend (a module in this package) exposes
`parse(stderr, source) -> list[fix]`.  The shared engine applies one fix
at a time, rebuilds after each, and stops when the build goes green —
the same contract as the C-family fixer.

Generated fix kinds (applied by `_apply_nudge`):
  ("delim", closers, lineno)   -> close an unclosed ()[]{} block
  ("semicolon", lineno)        -> append ';' to the line the compiler flagged
  ("raw", new_source)          -> backend rewrote the whole source
  ("import", pkg, "go"|"std")  -> add `import "pkg"` / `use std::pkg;`
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_FIX_TRIES = 5

_PAIRS = {")": "(", "]": "[", "}": "{"}
_OPENERS = set("([{")
_CLOSERS = set(")]}")
# Comment markers that are safe for the nudge languages (rust/go/js/perl/
# shell/python).  `;` and `--` are deliberately NOT here: they are statement
# terminators / decrement operators in JS/Go/Rust — treating them as comments
# would swallow real closing braces.  The scanner is error-driven (fires only
# after the compiler says delimiters are unclosed); a wrong scan is simply
# rejected by the rebuild, so a minimal comment set is the safe choice.
_LINE_COMMENT = ("//", "#")


def _scan_delim(source: str) -> Tuple[str, ...]:
    """Stack-scan ()[]{} outside strings & comments.

    Returns the missing closers in close order (innermost first):
    () means the delimiters are balanced.  Strings/comments handling is
    deliberately permissive — every fix is re-verified by the rebuild."""
    stack: List[str] = []
    i, n = 0, len(source)
    in_str: Optional[str] = None
    while i < n:
        ch = source[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        # line comments
        for lc in _LINE_COMMENT:
            if source.startswith(lc, i):
                j = source.find("\n", i)
                i = n if j == -1 else j
                break
        else:
            # block comment /* ... */
            if source.startswith("/*", i):
                j = source.find("*/", i)
                i = n if j == -1 else j + 2
                continue
            if ch in ("'", '"', "`"):
                in_str = ch
                i += 1
                continue
            if ch in _OPENERS:
                stack.append(ch)
            elif ch in _CLOSERS:
                if stack and stack[-1] == _PAIRS[ch]:
                    stack.pop()
            i += 1
    if not stack:
        return ()
    seen: List[str] = []
    for opener in reversed(stack):
        seen.append(")" if opener == "(" else ("]" if opener == "[" else "}"))
    return tuple(seen)


def _insert_at_line_end(src: str, text: str, lineno: int) -> str:
    lines = src.split("\n")
    idx = max(0, min(lineno - 1, len(lines) - 1))
    lines[idx] = lines[idx] + text
    return "\n".join(lines)


def _fix_delim_closers(src: str, closers: List[str], lineno: Optional[int]) -> str:
    text = "".join(reversed(closers))  # innermost first -> text order
    if lineno is not None:
        return _insert_at_line_end(src, text, lineno)
    return src + text


def _go_add_import(source: str, pkg: str) -> str:
    """Add `import "pkg"` to a Go source (after the package clause)."""
    if re.search(rf'^\s*import\s+"{re.escape(pkg)}"', source, re.M) or \
       re.search(rf'"{re.escape(pkg)}"\s*$', source, re.M):
        return source  # already imported
    lines = source.split("\n")
    ins = 0
    for idx, l in enumerate(lines):
        if l.startswith("package "):
            ins = idx + 1
            break
    if ins < len(lines) and lines[ins].strip() == "":
        lines.insert(ins, f'import "{pkg}"')
        lines.insert(ins + 1, "")
    else:
        lines.insert(ins, f'import "{pkg}"')
    return "\n".join(lines)


def _rust_add_import(source: str, mod: str) -> str:
    """Add `use std::<mod>;` to a Rust source (top, after any doc comment)."""
    from .rust import _RUST_STDLIB
    if mod not in _RUST_STDLIB:
        return source  # not a std module we know — don't guess
    if re.search(rf'^\s*use\s+std::(?:[a-z_]::)*{re.escape(mod)}', source, re.M):
        return source
    line = f"use std::{mod};\n"
    m = re.search(r"(?m)^\s*(pub\s+)?use\s+", source)
    if m:
        return source[:m.start()] + line + source[m.start():]
    return line + source


def _apply_nudge(source: str, fix: Tuple[str, ...]) -> Optional[str]:
    """Apply one nudge; returns the new source (or None if unapplicable)."""
    kind = fix[0]
    if kind == "delim":
        return _fix_delim_closers(source, fix[1], fix[2])
    if kind == "raw":
        return fix[1]
    if kind == "semicolon":
        lineno = fix[1]
        if lineno is None:
            return None
        lines = source.split("\n")
        idx = max(0, min(lineno - 1, len(lines) - 1))
        line = lines[idx]
        # The compiler said "expected ;" — append it unless the line already
        # ends in a terminator or a block/expression opener/operator (a real
        # statement is missing there, not a semicolon).
        if line.rstrip().endswith((";", "{", ",", ":",
                                   "=", "+", "-", "*", "/", "%", "&", "|",
                                   "^", "~", "!", "&&", "||", "->", ".", "(", "[")):
            return None
        lines[idx] = line + ";"
        return "\n".join(lines)
    if kind == "import":
        pkg, style = fix[1], fix[2]
        if style == "go":
            return _go_add_import(source, pkg)
        if style == "std":
            return _rust_add_import(source, pkg)
        return None
    return None


def _parse_line_no(stderr: str) -> Optional[int]:
    """Best-effort source line number from a compiler message (1-based).

    Scans line-by-line and returns the FIRST source-reference line number,
    skipping node's internal stack frames ("at wrapSafe (node:internal/...)").
    Formats we actually observed:
      "--> file:2:14"            (rustc)
      "main.go:8:1:"             (go)
      "file.js:6"                (node --check)
      "FILE line 2, near ..."    (perl)
      "/tmp/x.sh: line 2: ..."   (bash -n)
      "line 3:"                  (python ast)"""
    exts = ("js", "ts", "go", "rs", "sh", "pl", "php", "rb", "lua",
            "r", "java", "kt", "scala", "swift", "dart", "c", "cc",
            "cpp", "cu", "zig", "hs", "d", "py")
    ext_alt = "|".join(re.escape(e) for e in exts)
    ext_pat = r"\." + "(?:" + ext_alt + r"):(\d+)(?::|\b|$)"
    for line in stderr.splitlines():
        s = line.strip()
        if not s or s.startswith("at ") or "node:internal" in line or \
           s.startswith("panicked at") or s.startswith("=== ") or \
           s.startswith("error: aborting"):
            continue
        # "--> file:2:14" (rustc arrow) / "2:14:" (go) / "path:2:1:"
        m = re.search(r":\s*(\d+):\s*(\d+)(?::|\b|$)", s)
        if m:
            return int(m.group(1))
        # "path/file.java:3:" (javac/Kotlin/Scala/...) or "path/file.js:6" (node)
        m = re.search(ext_pat, s)
        if m:
            return int(m.group(1))
        # "path/file.d(4):" — D's parenthesized line form
        m = re.search(r"\." + "(?:" + ext_alt + r")(?:\((\d+)\)|:(\d+))", s)
        if m:
            return int(m.group(1) or m.group(2))
        # "path: line N:" / "FILE line N" / "line N"
        m = re.search(r"\bline\s+(\d+)\b", s, re.I)
        if m:
            return int(m.group(1))
    return None


# Registry: language -> parse(stderr, source) -> [fix].  Populated by
# __init__.py after importing every language backend.
_NUDGE_PARSERS: Dict[str, Callable[[str, str], List[Tuple[str, ...]]]] = {}


def register_parser(lang: str, fn: Callable[[str, str], List[Tuple[str, ...]]]) -> None:
    _NUDGE_PARSERS[lang] = fn


NUDGE_AVAILABLE = frozenset(_NUDGE_PARSERS)


def autofix_nudge(
    source_path: str | Path,
    build_cmd: List[str],
    run_build: Callable[[List[str]], Dict[str, Any]],
    lang: str,
    max_tries: int = MAX_FIX_TRIES,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Per-compiler local repair: parse stderr, apply what it suggests.

    Same contract as autofix_build: one fix per iteration, rebuild after
    each, stop when green, never re-apply a fix, and revert any fix that
    breaks a previously-successful build.  The candidate is mutated in
    place, so the artifact is the code that actually compiled."""
    from ..languages import normalize_lang
    source_path = Path(source_path)
    lang = normalize_lang(lang)
    parse = _NUDGE_PARSERS.get(lang)
    if parse is None:
        result = run_build(build_cmd)
        return bool(result.get("ok")), [], result
    applied: List[str] = []
    applied_set = set()
    result: Dict[str, Any] = {}
    for _ in range(max_tries):
        result = run_build(build_cmd)
        ok = bool(result.get("ok"))
        if ok:
            return True, applied, result
        stderr = result.get("stderr") or ""
        try:
            src = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False, applied, result
        for fix in parse(stderr, src):
            if not fix or fix in applied_set:
                continue
            new_source = _apply_nudge(src, fix)
            if new_source is None or new_source == src:
                continue
            applied_set.add(fix)
            source_path.write_text(new_source, encoding="utf-8")
            applied.append(f"[{lang}] " + " ".join(str(x) for x in fix))
            probe = run_build(build_cmd)
            if probe.get("ok"):
                result = probe
                continue
            if ok:
                # the ORIGINAL build succeeded; this fix broke it — revert.
                source_path.write_text(src, encoding="utf-8")
                applied.pop()
                return True, applied, result
            result = probe
            break
    return bool(result.get("ok")), applied, result
