# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Agent-suggested project builder — the "user that knows nothing" flow.

The user pastes (or attaches) a program + goal; the local LLM proposes a
COMPLETE project: spec + harness scripts + baseline.  Nothing is trusted
blindly — every step is guarded:

  1. structure      — the spec has the required keys
  2. guardrails     — every pipeline command is checked (launchers,
                      denylist, protected-data-file NO-CHANGE policy)
  3. content        — harness scripts are scanned for shell/escape hatches
  4. lint           — every python program must `py_compile` clean
  5. smoke run      — the whole pipeline actually RUNS on the baseline
                      (build -> verify -> score) in a temp project, with
                      the real data file (copied; the original is never
                      written) and capped timeouts

If any check fails, the errors are fed back to the LLM and it is asked to
fix the spec — up to `max_rounds` times.  Only a spec that survives every
gate is offered to the user for creation.

The user's DATA file is immutable: `data.protected_files` is forced into
the spec, the guardrails deny write access to it, and the pipeline
verifies its hash after every stage.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .guardrails import _scan_denylist, check_command
from .util import load_json, save_json

MAX_ROUNDS = 8
SMOKE_TIMEOUT_CAP = 120          # cap every step timeout for the smoke run
MAX_CODE_CHARS = 30000
MAX_DATA_B64 = 64_000_000      # base64 chars ≈ 48 MB raw — any file format
SAFE_CODE_EXTS = set()  # populated from the language registry below

from .languages import code_exts as _registry_code_exts
SAFE_CODE_EXTS = _registry_code_exts()
_BAD_SCRIPT_RE = [
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bsubprocess\s*\.\s*(run|Popen|call|check_output|check_call)\s*\(\s*[^)]*shell\s*=\s*True"),
    re.compile(r"\bshutil\s*\.\s*rmtree\s*\(\s*[\"']\/"),
    re.compile(r"\bexec\s*\(|eval\s*\("),
    # Network egress: a local harness has no business reaching out.
    re.compile(r"\b(import\s+(socket|urllib|requests|http\.client|ftplib|paramiko|aiohttp|websocket)|from\s+(socket|urllib|requests|http|ftplib)\b)"),
]

_BAD_SHELL_RE = [
    re.compile(r"\brm\s+(-[a-z]*r[a-z]*|-rf?|-fr?)\b"),
    re.compile(r"\b(curl|wget|nc|ncat|socat)\b"),
    re.compile(r"\|\s*(sh|bash|zsh|dash)\b"),
    re.compile(r">\s*(/etc|/bin|/usr|/dev|/boot|/lib)/"),
    re.compile(r"\bshred\b|\bmkfs\b|\bdd\s+if="),
    re.compile(r"\bsudo\b|\bchmod\s+-R\b|\bchown\b|\bkillall\b|\bpkill\b"),
    re.compile(r"\b(ssh|scp|rsync)\b"),
]


def _coerce_metrics(metrics: Any) -> Dict[str, Any]:
    """Coerce LLM direction/weight vocabulary ("minimize", "up", "2.0x")
    into the registry vocabulary so a validated suggestion cannot be
    rejected at CREATE time."""
    if not isinstance(metrics, dict):
        return metrics
    for mv in metrics.values():
        if not isinstance(mv, dict):
            continue
        d = str(mv.get("direction", "")).strip().lower()
        if d in ("minimize", "min", "down", "less", "smaller", "decrease", "lower-better"):
            mv["direction"] = "lower"
        elif d in ("maximize", "max", "up", "more", "larger", "increase", "higher-better"):
            mv["direction"] = "higher"
        elif d not in ("lower", "higher"):
            mv["direction"] = "lower"
        w = mv.get("weight", 1.0)
        if isinstance(w, str):
            # LLMs write weights as "2.0x"/"2x" — the multiplier IS the weight.
            m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[xX×]\s*$", w)
            if m:
                w = float(m.group(1))
        try:
            mv["weight"] = float(w)
        except (TypeError, ValueError):
            mv["weight"] = 1.0
    return metrics


def _normalize_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce LLM-variant shapes into the framework spec shape.

    gpt-oss sometimes wraps the spec in a narrative object
    ({"explanation": ..., "spec": {...}} / {"project": {...}}) or emits
    steps as an array of section objects.  Normalize defensively."""
    for key in ("spec", "project", "project_spec", "suggestion", "pipeline"):
        v = spec.get(key)
        if isinstance(v, dict) and ("steps" in v or "name" in v):
            inner = _normalize_spec(v)
            # keep the outer keys that carry meaning
            for k2, v2 in v.items():
                if k2 == key:
                    continue
                spec.setdefault(k2, v2)
            spec = inner
            break
    steps = spec.get("steps")
    if isinstance(steps, list):
        merged: Dict[str, Any] = {}
        for item in steps:
            if isinstance(item, dict):
                for k, v in item.items():
                    merged[k] = v
        spec["steps"] = merged
    steps = spec.get("steps") or {}
    for stage in ("verify", "score"):
        v = steps.get(stage)
        if isinstance(v, dict):
            steps[stage] = [v]
    # Small models emit direction/weight synonyms ("minimize", "up", "2.0x") —
    # coerce to the registry vocabulary (shared with the assembly path).
    _coerce_metrics(spec.get("metrics"))
    return spec


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """Pull the spec JSON object out of an LLM reply.

    gpt-oss narrates before emitting the JSON and often shows a prose
    example of the shape (`{"build": ...}`) before the real thing.  The
    real spec — with name/steps/metrics/data/files — is by far the largest
    valid JSON object in the reply, so collect every brace-matched span
    and keep the biggest parseable one."""
    if not raw:
        return None
    text = raw.strip()
    candidates: List[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S):
        candidates.append(m.group(1).strip())
    candidates.append(text)
    best: Optional[Dict[str, Any]] = None
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            best = json.loads(cand)
            continue
        except json.JSONDecodeError:
            pass
        for i, ch in enumerate(cand):
            if ch != "{":
                continue
            depth = 0
            in_str = False
            esc = False
            for j in range(i, len(cand)):
                c = cand[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(cand[i:j + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(obj, dict) and (
                            best is None or j - i > len(json.dumps(best))
                        ):
                            best = obj
                        break
    return best


# ---------------------------------------------------------------------------
# validation gates
# ---------------------------------------------------------------------------

def _referenced_programs(spec: Dict[str, Any]) -> List[str]:
    progs: List[str] = []
    steps = spec.get("steps") or {}
    for stage in ("build", "verify", "score"):
        if stage == "build":
            items = [steps.get("build")] if steps.get("build") else []
        else:
            items = steps.get(stage, []) or []
        for st in items:
            p = (st or {}).get("program")
            if p and p not in ("gcc", "cc", "g++", "clang", "make"):
                progs.append(p)
    return progs


def validate_structure(spec: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not spec.get("name"):
        errors.append("spec.name is required")
    steps = spec.get("steps") or {}
    b = steps.get("build") or {}
    if not b.get("program"):
        errors.append("steps.build.program is required")
    for stage in ("verify", "score"):
        for i, st in enumerate(steps.get(stage, []) or []):
            if not st.get("program"):
                errors.append(f"steps.{stage}[{i}].program is required")
            if stage == "score" and not (st.get("parse") or []):
                errors.append(f"steps.score[{i}].parse is required (how metrics are read)")
    if not (spec.get("metrics") or {}):
        errors.append("metrics: at least one metric required")
    try:
        hyst = float((spec.get("select") or {}).get("hysteresis", 1.0001))
        if hyst < 1.0:
            errors.append("select.hysteresis must be >= 1 (1 = any strict improvement; values below 1 accept WORSE results as improvements)")
    except (TypeError, ValueError):
        errors.append("select.hysteresis must be numeric")
    if not (spec.get("data") or {}).get("baseline_source"):
        errors.append("data.baseline_source is required (the single C file to improve)")
    files = spec.get("files") or {}
    if not isinstance(files, dict) or not files:
        errors.append("files: the LLM must supply harness scripts + baseline in files{}")
    for p in _referenced_programs(spec):
        if p not in files:
            errors.append(f"program '{p}' referenced in steps but missing from files{{}}")
    if (spec.get("data") or {}).get("baseline_source") not in files:
        errors.append("data.baseline_source must exist in files{}")
    return errors


def _scan_script_content(path: str, content: str, lang: str = "python") -> List[str]:
    """Harness scripts must not contain shell escape hatches, file
    destruction, or network egress.  The regex set is per-script-language
    (python scripts vs shell scripts get different rules)."""
    errs: List[str] = []
    bad, reason = _scan_denylist(content)
    if bad:
        errs.append(f"{path}: {reason}")
    rxs = _BAD_SHELL_RE if str(lang).lower() in ("shell", "sh", "bash", "zsh") else _BAD_SCRIPT_RE
    for rx in rxs:
        if rx.search(content):
            errs.append(f"{path}: forbidden pattern {rx.pattern}")
    return errs


def _lint_scripts(files: Dict[str, str]) -> List[str]:
    errs: List[str] = []
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        # LLMs often double-escape newlines inside JSON strings: the file
        # ends up with literal \n two-char sequences (valid JSON, broken
        # Python).  Try the original first; if it fails and an unescaped
        # variant lints clean, adopt the repair.
        candidates = [content]
        if "\\n" in content:
            candidates.append(content.replace("\\n", "\n"))
        last_err = "no candidate compiled"
        ok = False
        for cand in candidates:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                f.write(cand)
                tmp = f.name
            try:
                res = subprocess.run(
                    ["python3", "-m", "py_compile", tmp],
                    capture_output=True, text=True, timeout=60,
                )
                if res.returncode == 0:
                    ok = True
                    files[path] = cand
                    break
                last_err = (res.stderr or res.stdout)[-800:]
            finally:
                Path(tmp).unlink(missing_ok=True)
        if not ok:
            errs.append(f"{path}: lint failed: {last_err}")
    return errs


def _guardrail_scan_spec(spec: Dict[str, Any], project_dir: Path) -> List[str]:
    """Check every pipeline command against the guardrails."""
    errs: List[str] = []
    steps = spec.get("steps") or {}
    from .languages import ext_from_lang
    subs = {"candidate": f"candidate{ext_from_lang((spec.get('language') or 'c'))}", "artifact": "program.so", "workdir": "smoke", "project_dir": str(project_dir)}

    for stage in ("build", "verify", "score"):
        items = [steps.get("build")] if stage == "build" and steps.get("build") else steps.get(stage, []) or []
        for i, st in enumerate(items):
            prog = str(st.get("program", ""))
            if prog in ("gcc", "cc", "g++", "clang"):
                # bare compiler launcher — allowlist covers it
                continue
            p = Path(prog)
            resolved = p if p.is_absolute() else (project_dir / p).resolve()
            args = [str(a) for a in st.get("args", [])]
            cmd = [str(resolved)]
            for a in args:
                for k, v in subs.items():
                    a = a.replace("{" + k + "}", v)
                cmd.append(a)
            ok, reason = check_command(cmd, project=spec, project_dir=project_dir)
            if not ok:
                errs.append(f"steps.{stage}[{i}]: {reason}")
    return errs


def ensure_harness_ready(root: Path, spec: Dict[str, Any]) -> None:
    """Step programs run as executables (the pipeline execs them directly):
    guarantee a python shebang + +x so the model can never trip on
    'Exec format error'."""
    for prog in _referenced_programs(spec):
        if prog in ("gcc", "cc", "g++", "clang", "make"):
            continue
        safe, why = _safe_rel_path(prog)
        if not safe:
            continue
        p = root / prog
        if not p.exists():
            continue
        if p.suffix == ".py":
            content = p.read_text(encoding="utf-8")
            if not content.startswith("#!"):
                p.write_text("#!/usr/bin/env python3\n" + content, encoding="utf-8")
        try:
            p.chmod(0o755)
        except Exception:
            pass


def _write_project_files(root: Path, spec: Dict[str, Any], data_file: Optional[Dict[str, str]]) -> None:
    """Materialize a temp project (spec + files + data copy).  The data file
    is written only here (a copy of the user's original)."""
    root.mkdir(parents=True, exist_ok=True)
    for path, content in (spec.get("files") or {}).items():
        safe, why = _safe_rel_path(path)
        if not safe:
            raise ValueError(f"unsafe file path in suggestion: {path}: {why}")
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    ensure_harness_ready(root, spec)
    # data file (the user's original, copied verbatim)
    prot = (spec.get("data") or {}).get("protected_files", [])
    if data_file and prot:
        rel = prot[0]
        safe, why = _safe_rel_path(rel)
        if not safe:
            raise ValueError(f"unsafe protected data path: {rel}: {why}")
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(data_file.get("content_b64", "")))
    # spec with capped timeouts for the smoke run
    smoke_spec = json.loads(json.dumps(spec))
    for stage in ("build", "verify", "score"):
        items = [smoke_spec["steps"]["build"]] if stage == "build" else smoke_spec["steps"].get(stage, []) or []
        for st in items:
            st["timeout"] = min(int(st.get("timeout", 60) or 60), SMOKE_TIMEOUT_CAP)
    save_json(root / "project.json", smoke_spec)


def _safe_rel_path(path: str) -> Tuple[bool, str]:
    p = Path(path)
    if p.is_absolute():
        return False, "absolute paths not allowed"
    if ".." in p.parts:
        return False, "path traversal not allowed"
    if not p.parts:
        return False, "empty path"
    return True, ""


def _score_probe(spec: Dict[str, Any], data_file: Optional[Dict[str, str]]) -> Tuple[bool, str]:
    """Validate the SCORE step independently: compile a fast stand-in
    candidate (trial division to sqrt — fast on any workload), run only
    the score step, and require its parse rules to yield at least one
    metric.  Catches broken parse rules / score crashes even when the
    naive baseline never reached the score stage."""
    from .languages import normalize_lang
    if normalize_lang(spec.get("language")) != "c":
        return True, "score probe skipped (non-C language) — the smoke run validates parse rules"
    probe_c = r"""
#include <stdint.h>
#include <math.h>
int is_prime(long long n) {
    if (n < 2) return 0;
    if (n % 2 == 0) return n == 2;
    for (long long d = 3; d * d <= n && d < 3000000; d += 2)
        if (n % d == 0) return 0;
    return 1;
}
"""
    tmp = Path(tempfile.mkdtemp(prefix="kaisen-suggest-score-"))
    try:
        from .projects import Project, expand_command
        from .scores import parse_metrics
        from .util import run_subprocess
        _write_project_files(tmp, spec, data_file)
        probe = tmp / "probe.c"
        probe.write_text(probe_c, encoding="utf-8")
        artifact = tmp / "probe.so"
        res = subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2", "-o", str(artifact), str(probe), "-lm"],
            capture_output=True, text=True, timeout=60,
        )
        if res.returncode != 0:
            return False, f"probe build failed: {res.stderr[-400:]}"
        project = Project(tmp)
        score_steps = (spec.get("steps") or {}).get("score", []) or []
        if not score_steps:
            return False, "score step missing"
        step = score_steps[0]
        cmd = expand_command(step, project, probe, artifact, tmp / "probe-work")
        res = run_subprocess(cmd, timeout=min(int(step.get("timeout", 60) or 60), SMOKE_TIMEOUT_CAP), cwd=tmp)
        combined = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
        metrics = parse_metrics(combined, step.get("parse", []))
        if not metrics:
            tail = combined[-600:]
            return False, f"score probe produced no metrics (exit={res.get('returncode')}): {tail}"
        # The parsed metric keys must cover the spec's declared metrics —
        # otherwise every generation reports no_score (key mismatch).
        declared = set((spec.get("metrics") or {}).keys())
        parsed = set(metrics.keys())
        missing = declared - parsed
        if missing:
            return False, (
                f"score probe metrics {sorted(parsed)} do not cover the declared metrics "
                f"{sorted(declared)} — the parse regex named groups must match the metric keys "
                f"exactly (missing: {sorted(missing)})"
            )
        return True, f"score probe OK: metrics={metrics}"
    except Exception as e:
        return False, f"score probe raised: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _smoke_run(spec: Dict[str, Any], data_file: Optional[Dict[str, str]]) -> Tuple[Dict[str, Any], List[str]]:
    """Run the suggested pipeline for real on the baseline, in a temp
    project, with the real data (copied).  Returns (pipeline_result, notes).
    The pipeline result carries stage/outcome/reason + stdout/stderr tails,
    so the caller can judge harness validity vs baseline slowness."""
    notes: List[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="kaisen-suggest-"))
    try:
        from .pipeline import run_pipeline
        from .projects import Project
        _write_project_files(tmp, spec, data_file)
        project = Project(tmp)
        candidate = tmp / str((spec.get("data") or {}).get("baseline_source", "original.c"))
        if not candidate.exists():
            notes.append(f"smoke run FAILED: the baseline file '{candidate.name}' is missing — your JSON MUST include the complete program in files as \"{candidate.name}\"")
            return {"ok": False, "stage": "baseline", "reason": f"baseline program file missing from files: {candidate.name}"}, notes
        workdir = tmp / "smoke"
        res = run_pipeline(project, candidate, workdir)
        if res.get("ok"):
            notes.append(f"smoke run OK: metrics={res.get('metrics')}")
        else:
            stage = res.get("stage", "?")
            reason = (res.get("reason") or "")[:1200]
            tail = (res.get("stderr_tail") or res.get("stdout_tail") or "")[:1200]
            notes.append(f"smoke run FAILED at {stage}: {reason}\n{tail}")
        return res, notes
    except Exception as e:
        notes.append(f"smoke run raised: {e}")
        return {"ok": False, "stage": "worker", "reason": str(e), "timed_out": False}, notes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# the suggest loop



def suggest_project(
    request: Callable[[str], str],
    goal: str,
    code: str,
    data_file: Optional[Dict[str, str]] = None,
    max_rounds: int = MAX_ROUNDS,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """Stepwise, guarded project builder: ONE small job per prompt,
    assembled at the end. Each step retries with its own error feedback;
    the assembled spec then passes the structural/guardrail/lint gates and
    a real smoke run (repair rounds if needed)."""
    from . import promptlib
    from .languages import lang_info, normalize_lang
    from .skills import extract_code

    lang = normalize_lang(language or "c")
    info = lang_info(lang)
    kind = str(info["kind"])
    baseline_name = f"original{info['ext']}"
    orig_goal = goal
    goal = promptlib.expand_goal(goal)
    tier = "small"  # best-effort; local-first default

    def prog(**kw: Any) -> None:
        if on_progress:
            try:
                on_progress(kw)
            except Exception:
                pass

    result: Dict[str, Any] = {"ok": False, "rounds": 0, "notes": [], "steps": []}
    prog(stage="starting", max_rounds=max_rounds)
    log_dir = Path(tempfile.gettempdir()) / "kaisen-suggest"
    log_dir.mkdir(exist_ok=True)

    data_note = ""
    if data_file:
        data_note = (f"\n\nDATA FILE: a protected data file '{data_file.get('name', 'data.bin')}' "
                     f"will be available at data/<name> relative to the project dir. NEVER "
                     f"modify it; work on copies.")

    steps: List[Dict[str, Any]] = []

    def run_step(name: str, label: str, build_prompt, parse, retries: int = 2, clarify=None):
        """One bite of the elephant — as a CONVERSATION. The model gets the
        full user/assistant transcript so every turn is in context. When it
        replies with a question instead of the expected answer, `clarify`
        answers it from what KAISEN already knows (the provided program,
        the analysis) and the conversation continues instead of failing."""
        conv: List[Dict[str, str]] = []
        err = ""
        for attempt in range(retries + 1):
            prog(step=name, state="running", label=label, attempt=attempt + 1)
            conv.append({"role": "user", "content": build_prompt(err)})
            try:
                raw = request(conv)
            except Exception as e:
                err = f"LLM call failed: {e}"
                conv.append({"role": "assistant", "content": ""})
                continue
            (log_dir / f"step-{name}-{attempt}.raw.txt").write_text(raw, encoding="utf-8", errors="replace")
            conv.append({"role": "assistant", "content": raw})
            parsed, perr = parse(raw)
            if perr:
                # Did the model ASK instead of answer? Answer it.
                if clarify is not None and "?" in raw and "{" not in raw:
                    answer = clarify(raw)
                    if answer:
                        conv.append({"role": "user", "content": answer})
                        continue
                err = perr
                continue
            steps.append({"id": name, "label": label, "state": "done", "output": raw})
            prog(step=name, state="done", label=label, output=raw, attempt=attempt + 1)
            return parsed
        steps.append({"id": name, "label": label, "state": "failed", "error": err})
        prog(step=name, state="failed", label=label, error=err)
        return None

    def retry_suffix(err: str) -> str:
        return f"\n\nYOUR LAST REPLY WAS REJECTED. FIX THIS AND REPLY AGAIN:\n{err}" if err else ""

    def parse_analysis(raw: str):
        a = _extract_json(raw)
        if not a:
            return None, "no valid JSON found"
        missing = [k for k in ("summary", "entry_contract", "metrics") if not a.get(k)]
        if missing:
            return None, f"missing keys: {missing}"
        return a, ""

    analysis = run_step("analysis", "Analyze the goal",
                        lambda err: promptlib.step_goal_analysis(tier, goal, code=code) + retry_suffix(err),
                        parse_analysis,
                        clarify=(lambda q: f"Here is the program again, verbatim (it was in the first message):\n```\n{code.strip()[:4000]}\n```\nNow analyze THIS program and reply with the JSON.")
                                 if code.strip() else None)
    if analysis is None:
        result["error"] = "analysis step failed — the model could not produce a specification"
        return result
    analysis_json = json.dumps(analysis, indent=1)

    # ---- STEP 2 — baseline -------------------------------------------------
    def parse_code(raw: str):
        c = extract_code(raw, lang)
        if not c or len(c.strip()) < 5:
            return None, "no code block found"
        from .linters import syntax_check
        ok, msg = syntax_check(lang, c)
        if not ok:
            return None, f"the program does not compile: {msg}"
        return c, ""

    if code.strip():
        baseline_code = code.strip()
        steps.append({"id": "baseline", "label": "Baseline program", "state": "provided"})
        prog(step="baseline", state="done", label="Baseline program (provided)")
    else:
        baseline_code = run_step("baseline", "Write the baseline program",
                                 lambda err: promptlib.step_baseline_program(tier, goal, analysis_json, lang) + retry_suffix(err),
                                 parse_code, retries=4)
        if baseline_code is None:
            result["error"] = "baseline step failed — the model could not write the program"
            return result

    # ---- STEPS 3/4/5 — build, verify, score --------------------------------
    def parse_py(raw: str):
        c = extract_code(raw, "python")
        if not c or len(c.strip()) < 10:
            return None, "no python code block found"
        from .linters import syntax_check
        ok, msg = syntax_check("python", c)
        if not ok:
            return None, f"the script has a syntax error: {msg}"
        return c, ""
    clarify_analysis = lambda q: ("Here is the analysis you need:\n" + analysis_json +
                                  "\nAnswer the original request with the python code block only.")

    # Library-style user programs (no main): the harness needs a driver
    # main — generated here and compiled together with the candidate.
    user_code = bool(code.strip())
    no_main = user_code and not re.search(r"\bmain\s*\(", baseline_code)
    driver_code: Optional[str] = None
    if no_main:
        def parse_c(raw: str):
            c = extract_code(raw, lang)
            if not c or len(c.strip()) < 5:
                return None, "no code block found"
            from .linters import syntax_check
            ok, msg = syntax_check(lang, c)
            if not ok:
                return None, f"driver does not compile: {msg}"
            return c, ""
        driver_code = run_step("driver", "Write the driver main",
                               lambda err: promptlib.step_driver_program(tier, analysis_json, lang) + retry_suffix(err),
                               parse_c, retries=2, clarify=clarify_analysis)
        if driver_code is None:
            result["error"] = "driver step failed"
            return result

    build_script = run_step("build", "Write the build step",
                            lambda err: promptlib.step_build_script(tier, lang, kind, with_driver=no_main) + retry_suffix(err),
                            parse_py, clarify=clarify_analysis)
    if build_script is None:
        result["error"] = "build step failed"
        return result

    verify_script = run_step("verify", "Write the verify step",
                             lambda err: promptlib.step_verify_script(tier, analysis_json, lang, data_note, with_driver=no_main) + retry_suffix(err),
                             parse_py, clarify=clarify_analysis)
    if verify_script is None:
        result["error"] = "verify step failed"
        return result

    def parse_score(raw: str):
        script = extract_code(raw, "python")
        if not script or len(script.strip()) < 10:
            return None, "no python code block found"
        # Small models often paste METRICS JSON / PARSE inside the code
        # block — pull those sections out (they belong after the fence).
        cut = re.search(r"\n\s*(METRICS JSON:|PARSE:)\s*", script)
        if cut:
            script = script[:cut.start()].rstrip()
        if len(script.strip()) < 10:
            return None, "code block contained only the METRICS/PARSE sections"
        # The model quotes the instruction in its thinking — the REAL
        # declaration is the LAST "METRICS JSON:" in the reply.
        m = raw.rfind("METRICS JSON:")
        m = m if m >= 0 else -1
        metrics = None
        if m >= 0:
            start = raw.find("{", m)
            if start >= 0:
                # Balanced brace scan: nested metric objects end at the
                # FIRST '}' for a naive regex — walk depth instead.
                depth = 0
                in_str = False
                esc = False
                for j in range(start, len(raw)):
                    c = raw[j]
                    if in_str:
                        if esc:
                            esc = False
                        elif c == "\\":
                            esc = True
                        elif c == '"':
                            in_str = False
                        continue
                    if c == '"':
                        in_str = True
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                metrics = json.loads(raw[start:j + 1])
                            except Exception:
                                metrics = None
                            break
        parse_rules: List[Dict[str, str]] = []
        for line in raw.splitlines():
            pm = re.match(r"^\s*([A-Za-z_][\w]*)\s*=\s*(\(\?P<[^>]+>.*\))\s*$", line)
            if pm:
                parse_rules.append({"type": "regex", "pattern": pm.group(2).strip()})
        # Models often emit the same rule twice, once over-escaped
        # (`[\\d.]+`) and once clean (`[\d.]+`) — parse_metrics treats
        # them identically, so keep one canonical copy.
        seen: set = set()
        unique_rules: List[Dict[str, str]] = []
        for r in parse_rules:
            key = str(r["pattern"]).replace("\\\\", "\\")
            if key in seen:
                continue
            seen.add(key)
            unique_rules.append(r)
        parse_rules = unique_rules
        if not metrics:
            return None, "no METRICS JSON found after the code block"
        if not parse_rules:
            # The model forgot the PARSE lines: the score contract prints
            # `key=value` lines — synthesize the default rules from the
            # declared metric keys instead of failing the step.
            for key in (metrics or {}).keys():
                parse_rules.append({"type": "regex", "pattern": f"(?P<{key}>[\\\\d.]+)"})
        return {"script": script, "metrics": metrics, "parse": parse_rules}, ""

    score = run_step("score", "Write the score step",
                     lambda err: promptlib.step_score_script(tier, analysis_json, data_note, with_driver=no_main) + retry_suffix(err),
                     parse_score, clarify=clarify_analysis)
    if score is None:
        result["error"] = "score step failed"
        return result

    # ---- ASSEMBLE ----------------------------------------------------------
    metrics = dict(score["metrics"] or {})
    parse_keys: set = set()
    for r in score["parse"]:
        pm = re.search(r"\?P<(\w+)>", r.get("pattern", ""))
        if pm:
            parse_keys.add(pm.group(1))
    if not metrics:
        direction_map = {m.get("key"): m.get("direction", "lower") for m in (analysis.get("metrics") or [])}
        metrics = {k: {"label": k, "unit": "", "direction": direction_map.get(k, "lower"), "weight": 1} for k in parse_keys}
    _coerce_metrics(metrics)

    name_slug = re.sub(r"[^a-z0-9]+", "-", orig_goal.lower()).strip("-")[:40] or "project"
    spec: Dict[str, Any] = {
        "name": analysis.get("summary", name_slug)[:80] or name_slug,
        "description": analysis.get("summary", ""),
        "language": lang,
        "artifact_name": analysis.get("artifact_name") or "program",
        "steps": {
            "build": {"program": "harness/build.py", "args": ["{candidate}", "{artifact}"], "timeout": 60},
            "verify": [{"program": "harness/verify.py", "args": ["{artifact}"], "timeout": 60}],
            "score": [{"program": "harness/score.py", "args": ["{artifact}"], "timeout": 60, "parse": score["parse"]}],
        },
        "metrics": metrics,
        "telemetry": {"enabled": True, "progress_token": "KAISEN_PROGRESS", "live_fields": sorted(parse_keys)},
        "select": {"hysteresis": 1.0001},
        "guardrails": {"enabled": True},
        "prompts": {"goal": orig_goal},
        "data": {"baseline_source": baseline_name, "contract_text": analysis.get("entry_contract", "")},
        "files": {
            baseline_name: baseline_code,
            "harness/build.py": build_script,
            "harness/verify.py": verify_script,
            "harness/score.py": score["script"],
            **({f"harness/driver{info['ext']}": driver_code} if driver_code else {}),
        },
    }
    if data_file:
        spec["data"]["protected_files"] = [f"data/{data_file.get('name', 'data.bin')}"]

    # ---- GATES + REPAIR ROUNDS ---------------------------------------------
    for round_no in range(1, max_rounds + 1):
        result["rounds"] = round_no
        errs: List[str] = []
        errs += validate_structure(spec)
        if not errs:
            # Full registry validation — a suggestion that passes the smoke
            # run must also pass CREATE, or the user hits a rejection AFTER
            # being told everything is valid.
            from .projects import validate_spec as registry_validate
            check = dict(spec)
            check.setdefault("id", "suggest-check")
            errs += [f"spec: {e}" for e in registry_validate(check)]
        if not errs:
            for path, content in (spec.get("files") or {}).items():
                if path.endswith(".py"):
                    errs += _scan_script_content(path, content)
        if not errs:
            errs += _lint_scripts(spec.get("files") or {})
        if not errs:
            tmp = Path(tempfile.mkdtemp(prefix="kaisen-suggest-gr-"))
            try:
                _write_project_files(tmp, spec, data_file)
                errs += _guardrail_scan_spec(spec, tmp)
            except ValueError as e:
                errs += [str(e)]
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if not errs:
            prog(stage="smoke", round=round_no, raw_label=f"Round {round_no} — smoke run (build → verify → score)…")
            res, notes = _smoke_run(spec, data_file)
            if res.get("ok"):
                prog(stage="done", ok=True, raw_label=f"Validated in {round_no} round(s)")
                result.update({"ok": True, "suggested_spec": spec, "notes": notes, "steps": steps})
                return result
            stage = res.get("stage", "?")
            reason = (res.get("reason") or "")[:1200]
            hint = ""
            if stage == "build":
                hint = ("\nHINT: the failure is in the BUILD stage. Inspect the baseline program AND "
                        "harness/build.py: use ONLY the standard library (no boost/gmp/external libs), "
                        "match the compiler flags to the language, and return the CORRECTED FILE inside "
                        "the files object of your JSON.")
            elif stage == "verify":
                hint = ("\nHINT: verify failed fast — the baseline contradicts your reference. "
                        "For FLOAT programs the usual cause is tolerance: accumulation noise grows with "
                        "input size — use np.allclose(result, reference, rtol=1e-3, atol=1e-3) or looser. "
                        "Also check the artifact's invocation/ctypes signature.")
            elif stage == "score":
                hint = ("\nHINT: the score step produced no usable metrics. Make it deterministic and "
                        "bounded: loop the workload until a wall-clock budget (~5-15 s) elapses, count "
                        "rounds, print key=value lines EXACTLY matching the PARSE regexes (one rule per line).")
            elif stage == "baseline":
                hint = "\nHINT: the baseline program file is missing from files — include it."
            errs = (notes[-1].splitlines() if notes else [reason])[:12]
            if hint:
                errs.append(hint)
        # ---- Targeted repair: re-run ONLY the responsible step, with the
        # real smoke error as feedback. One small job per repair — the
        # model never has to re-emit the whole spec.
        fix_feedback = "\n".join((notes[-1].splitlines() if notes else [reason])[:10])
        prog(stage="repair", round=round_no, raw_label=f"Round {round_no} — repairing the {stage} step…")
        repaired = False
        user_baseline = bool(code.strip())
        if stage in ("build", "baseline") and not user_baseline:
            fixed_b = run_step("baseline", "Fix the baseline program",
                               lambda err: promptlib.step_baseline_program(tier, goal, analysis_json, lang)
                               + "\n\nTHE SMOKE RUN FAILED WITH (fix the program so this cannot happen):\n" + fix_feedback
                               + retry_suffix(err),
                               parse_code, retries=2)
            if fixed_b:
                spec["files"][baseline_name] = fixed_b
                repaired = True
        # The user's provided baseline is SACRED: build failures with a
        # user program are the harness's fault — fix the scripts, never
        # regenerate the program.
        if stage == "build":
            if no_main:
                fixed_d = run_step("driver", "Fix the driver main",
                                   lambda err: promptlib.step_driver_program(tier, analysis_json, lang)
                                   + "\n\nTHE SMOKE RUN FAILED WITH (fix the driver so this cannot happen):\n" + fix_feedback
                                   + retry_suffix(err),
                                   parse_c, retries=2)
                if fixed_d:
                    spec["files"][f"harness/driver{info['ext']}"] = fixed_d
                    repaired = True
            fixed_build = run_step("build", "Fix the build step",
                                   lambda err: promptlib.step_build_script(tier, lang, kind, with_driver=no_main)
                                   + "\n\nTHE SMOKE RUN FAILED WITH (fix the script so this cannot happen):\n" + fix_feedback
                                   + retry_suffix(err),
                                   parse_py, retries=2)
            if fixed_build:
                spec["files"]["harness/build.py"] = fixed_build
                repaired = True
        if stage == "verify" and not user_baseline:
            fixed_b2 = run_step("baseline", "Fix the baseline program",
                                lambda err: promptlib.step_baseline_program(tier, goal, analysis_json, lang)
                                + "\n\nTHE SMOKE RUN FAILED AT VERIFY (the program's output is wrong — fix the program):\n" + fix_feedback
                                + retry_suffix(err),
                                parse_code, retries=2)
            if fixed_b2:
                spec["files"][baseline_name] = fixed_b2
                repaired = True
        if stage == "verify":
            if no_main:
                fixed_d = run_step("driver", "Fix the driver main",
                                   lambda err: promptlib.step_driver_program(tier, analysis_json, lang)
                                   + "\n\nTHE SMOKE RUN FAILED AT VERIFY (the driver's verify mode is broken — fix the driver):\n" + fix_feedback
                                   + retry_suffix(err),
                                   parse_c, retries=2)
                if fixed_d:
                    spec["files"][f"harness/driver{info['ext']}"] = fixed_d
                    repaired = True
            fixed_v = run_step("verify", "Fix the verify step",
                               lambda err: promptlib.step_verify_script(tier, analysis_json, lang, data_note, with_driver=no_main)
                               + "\n\nTHE SMOKE RUN FAILED WITH (fix the script so this cannot happen):\n" + fix_feedback
                               + retry_suffix(err),
                               parse_py, retries=2)
            if fixed_v:
                spec["files"]["harness/verify.py"] = fixed_v
                repaired = True
        if stage == "score":
            fixed_sc = run_step("score", "Fix the score step",
                                lambda err: promptlib.step_score_script(tier, analysis_json, data_note, with_driver=no_main)
                                + "\n\nTHE SMOKE RUN FAILED WITH (fix the script so this cannot happen):\n" + fix_feedback
                                + retry_suffix(err),
                                parse_score, retries=2)
            if fixed_sc:
                spec["files"]["harness/score.py"] = fixed_sc["script"]
                spec["steps"]["score"][0]["parse"] = fixed_sc["parse"]
                spec["metrics"] = dict(fixed_sc["metrics"] or spec.get("metrics") or {})
                repaired = True
        if not repaired:
            # Last resort: ask for a corrected full spec (large models only
            # can emit it; small models rarely fix anything this way).
            repair_prompt = promptlib.step_spec_repair(tier, json.dumps(spec)[:30000], "\n- ".join(errs[:15]))
            try:
                rep_raw = request(repair_prompt)
            except Exception as e:
                steps.append({"id": "repair", "label": f"Repair round {round_no}", "state": "failed", "error": str(e)})
                continue
            rep_spec = _extract_json(rep_raw)
            if rep_spec:
                rep_spec = _normalize_spec(rep_spec)
                merged_files = dict(spec.get("files") or {})
                merged_files.update(rep_spec.get("files") or {})
                rep_spec["files"] = merged_files
                spec = rep_spec
            steps.append({"id": "repair", "label": f"Repair round {round_no}",
                          "state": "done" if rep_spec else "failed", "output": rep_raw})


    result["error"] = ("suggested spec could not pass validation after "
                       f"{max_rounds} repair rounds. Last errors:\n- " + "\n- ".join(errs[:12]))
    result["steps"] = steps
    return result
