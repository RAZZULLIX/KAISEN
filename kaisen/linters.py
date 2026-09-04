# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Language linters that back the auto-fix layer.

Every tool here is a LOCAL binary or pure-python module (16 GB machines
included): no network, no service.  The gcc-message fixer stays in
autofix.py (C/C++/CUDA); this module covers the rest:

  python  -> ast (syntax) + pyflakes (names) + ruff --fix (unused imports)
  shell   -> bash -n
  javascript -> node --check
  everything else -> no linter yet: empty diagnostics (the pipeline still
  surfaces the raw build stderr to the LLM via history).
"""

from __future__ import annotations

import ast
import io
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .languages import normalize_lang

_TOOL_CACHE: dict = {}


def _which(name: str) -> Optional[str]:
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    # ruff/pyflakes installed with pip --user land in ~/.local/bin, which
    # may be missing from a systemd/daemon PATH — probe common locations.
    home = str(Path.home())
    candidates = []
    found = shutil.which(name)
    if found:
        candidates.append(found)
    for extra in (f"{home}/.local/bin", "/usr/local/bin", "/usr/bin"):
        p = f"{extra}/{name}"
        if p not in candidates and Path(p).is_file():
            candidates.append(p)
    if not candidates:
        _TOOL_CACHE[name] = None
        return None
    _TOOL_CACHE[name] = candidates[0]
    return candidates[0]

def _run_tool(cmd: List[str], input_bytes: Optional[bytes] = None, timeout: int = 60) -> Tuple[bool, str]:
    """Run a toolchain binary; returns (ok, message).  Never raises:
    a missing binary reports 'not found' (the message goes back to the
    model as step feedback), and output is decoded UTF-8 with replacement
    — locale codecs (cp1252 "charmap" on Windows) must not break
    validation of non-ASCII compiler messages."""
    exe = cmd[0]
    if shutil.which(exe) is None:
        return False, f"{exe} not found — install the toolchain to validate this language"
    try:
        r = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
    except Exception as e:
        return False, f"{exe} could not run: {e}"
    msg = (r.stderr or b"").decode("utf-8", "replace")
    if not msg.strip():
        msg = (r.stdout or b"").decode("utf-8", "replace")
    return r.returncode == 0, msg.strip()

def _pyflakes_issues(code: str) -> str:
    """pyflakes diagnostics as text (undefined names, unused imports…)."""
    try:
        from pyflakes.api import check
        from pyflakes.reporter import Reporter
    except ImportError:
        return ""
    buf = io.StringIO()
    check(code, "candidate.py", Reporter(buf, buf))
    return buf.getvalue().strip()


def lint_python(code: str) -> List[str]:
    """Syntax + name-level diagnostics for a python source (no side effects)."""
    issues: List[str] = []
    try:
        ast.parse(code)
    except SyntaxError as e:
        issues.append(f"line {e.lineno or '?'}: SyntaxError: {e.msg}")
        return issues  # name analysis needs a parseable tree
    fl = _pyflakes_issues(code)
    for line in fl.splitlines():
        line = line.strip()
        if line and not line.startswith("---"):
            issues.append(line)
    return issues


def autofix_python(path: str | Path) -> Tuple[bool, List[str]]:
    """Mechanical, SAFE fixes for python candidates:
      1. ruff --fix (unused imports F401 / unused variables F841)
      2. report remaining syntax errors so they surface to the LLM.
    Returns (applied_any_fix, notes)."""
    path = Path(path)
    notes: List[str] = []
    fixed = False
    try:
        code = path.read_text(encoding="utf-8")
    except OSError:
        return False, ["candidate unreadable"]
    ruff = _which("ruff")
    if ruff:
        try:
            before = path.read_text(encoding="utf-8")
            _run_tool([ruff, "check", "--fix", "--select", "F401,F841", "--quiet", str(path)], timeout=60)
            if path.read_text(encoding="utf-8") != before:
                fixed = True
                notes.append("ruff: removed unused imports/variables")
        except Exception:
            pass
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        notes.append(f"syntax error line {e.lineno or '?'}: {e.msg}")
    else:
        fl = _pyflakes_issues(path.read_text(encoding="utf-8"))
        if fl:
            notes.append(fl.splitlines()[0][:200])
    return fixed, notes


def lint_shell(path: str | Path) -> List[str]:
    bash = _which("bash")
    if not bash:
        return []
    _ok, out = _run_tool([bash, "-n", str(path)], timeout=30)
    return [l for l in out.splitlines() if l.strip()][:6]


def lint_javascript(path: str | Path) -> List[str]:
    node = _which("node")
    if not node:
        return []
    _ok, out = _run_tool([node, "--check", str(path)], timeout=30)
    return [l for l in out.splitlines() if l.strip()][:6]


def syntax_check(language: str, code: str) -> tuple:
    """Fast compile/syntax probe for a source string. Returns (ok, message).
    Used to verify every agent-written file BEFORE acceptance — the error
    message goes straight back to the model as step feedback."""
    from .languages import ext_from_lang, normalize_lang
    import tempfile as _tempfile
    lang = normalize_lang(language)
    if lang == "python":
        import ast as _ast
        try:
            _ast.parse(code)
            return True, ""
        except SyntaxError as e:
            return False, f"line {e.lineno or '?'}: {e.msg}"[:400]
    if lang in ("ruby", "r", "lua", "perl", "haskell", "php", "java", "go", "rust", "zig", "kotlin", "swift", "scala", "dart", "csharp"):
        return True, ""  # no cheap universal probe; structural gates cover these
    if lang == "shell":
        ok, msg = _run_tool(["bash", "-n"], input_bytes=code.encode("utf-8"), timeout=30)
        return ok, msg[:400]
    if lang in ("javascript", "typescript"):
        ok, msg = _run_tool(["node", "--check"], input_bytes=code.encode("utf-8"), timeout=30)
        return ok, msg[:400]
    if lang in ("c", "cpp"):
        ext = ".c" if lang == "c" else ".cpp"
        f = _tempfile.NamedTemporaryFile("w", suffix=ext, delete=False, encoding="utf-8")
        f.write(code)
        f.close()
        try:
            ok, msg = _run_tool(
                ["g++" if lang == "cpp" else "gcc", "-fsyntax-only",
                 "-x", "c++" if lang == "cpp" else "c", f.name], timeout=60)
            # strip temp-path noise from the message
            return ok, msg.replace(f.name, "program")[:600]
        finally:
            Path(f.name).unlink(missing_ok=True)
    if lang == "cuda":
        f = _tempfile.NamedTemporaryFile("w", suffix=".cu", delete=False, encoding="utf-8")
        f.write(code)
        f.close()
        try:
            out = _tempfile.NamedTemporaryFile(suffix=".o", delete=False)
            out.close()
            ok, msg = _run_tool(["nvcc", "-arch=native", "-c", f.name, "-o", out.name], timeout=120)
            Path(out.name).unlink(missing_ok=True)
            return ok, msg.replace(f.name, "program")[:600]
        finally:
            Path(f.name).unlink(missing_ok=True)
    return True, ""



def lint_notes(language: Optional[str], candidate_path: str | Path) -> List[str]:
    """Short, LLM-readable diagnostics for a failed candidate, per language."""
    lang = normalize_lang(language)
    p = Path(candidate_path)
    if not p.is_file():
        return []
    try:
        code = p.read_text(encoding="utf-8")
    except OSError:
        return []
    if lang == "python":
        return lint_python(code)[:6]
    if lang == "shell":
        return lint_shell(p)
    if lang in ("javascript", "typescript"):
        return lint_javascript(p)
    # C/C++/CUDA diagnostics already ride the gcc-message fixer path.
    return []
