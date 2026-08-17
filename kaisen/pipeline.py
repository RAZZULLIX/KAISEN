# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Pipeline runner: build -> verify* -> score*.

Runs inside a worker process.  Executes the project's harness programs in
the order declared by the spec, parses unified metrics from score outputs,
and returns a serializable result.  Every command is guardrail-checked
before execution (single choke point).

Result shape:
  {
    "ok": bool, "stage": "build|verify|score", "outcome": "...",
    "reason": str, "metrics": {key: value},
    "artifact": str|None, "timings": {stage: seconds},
    "stdout_tail": str, "stderr_tail": str,
  }
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .guardrails import check_command
from .projects import Project, expand_command
from .scores import parse_metrics
from .util import format_bytes, run_subprocess, RunAbort

TAIL_CHARS = 4000


class PipelineError(Exception):
    def __init__(self, stage: str, outcome: str, reason: str, **extra):
        super().__init__(reason)
        self.stage = stage
        self.outcome = outcome
        self.reason = reason
        self.extra = extra


def _tail(text: str, n: int = TAIL_CHARS) -> str:
    return (text or "")[-n:]


def _fail(stage: str, outcome: str, reason: str, **extra) -> Dict[str, Any]:
    res = {
        "ok": False,
        "stage": stage,
        "outcome": outcome,
        "reason": reason,
        "metrics": {},
        "artifact": None,
        "timings": {},
        "stdout_tail": extra.pop("stdout_tail", ""),
        "stderr_tail": extra.pop("stderr_tail", ""),
    }
    res.update(extra)
    return res




def _materialize_inline(step: Dict[str, Any], project: Project, stage: str, idx: int) -> Dict[str, Any]:
    """Inline steps (scripts written directly in the spec, GUI-editable)
    become real executable files in the project's .kaisen_scripts dir, so
    any language can be driven by a pipeline stored fully in project.json."""
    inline = step.get("inline")
    if not isinstance(inline, dict) or not inline.get("code"):
        return step
    lang = str(inline.get("lang", "python")).strip().lower()
    if lang in ("python", "py"):
        shebang, ext = "#!/usr/bin/env python3\n", ".py"
    else:
        shebang, ext = "#!/usr/bin/env bash\nset -euo pipefail\n", ".sh"
    scripts_dir = project.path / ".kaisen_scripts"
    scripts_dir.mkdir(exist_ok=True)
    path = scripts_dir / f"{stage}{idx}{ext}"
    path.write_text(shebang + str(inline["code"]), encoding="utf-8")
    try:
        path.chmod(0o755)
    except OSError:  # pragma: no cover
        pass
    out = dict(step)
    out.pop("inline", None)
    out["program"] = str(path.relative_to(project.path))
    return out


def run_pipeline(
    project: Project,
    candidate: str | Path,
    workdir: str | Path,
    progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute the full pipeline for a candidate source file."""
    spec = project.spec
    steps = spec["steps"]
    context = context or {}
    # Early-abort rules: metric key -> {direction, limit}.  A score step is
    # killed mid-run once its live value provably cannot beat the champion.
    abort_rules = context.get("score_abort") or {}
    timings: Dict[str, float] = {}
    score_details: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {}
    artifact: Optional[str] = None

    # Protected data files: the originals are immutable.  Snapshot their
    # hashes up front; any modification (by the candidate, the harness,
    # anything) fails the run — generations can never tamper with the
    # test data.
    protected = list((spec.get("data") or {}).get("protected_files", []) or [])
    protected_hashes = {}
    for rel in protected:
        p = Path(rel)
        full = p if p.is_absolute() else Path(project.path) / p
        if full.exists():
            protected_hashes[rel] = _sha256(full)
    if protected_hashes:
        pass  # protection is silent: hashes verified after every stage

    def verify_protected(stage: str) -> Optional[Dict[str, Any]]:
        for rel, h0 in protected_hashes.items():
            p = Path(rel)
            full = p if p.is_absolute() else Path(project.path) / p

            try:
                if not full.exists() or _sha256(full) != h0:
                    return _fail(
                        stage, "protected_data_modified",
                        f"protected data file '{rel}' was modified or removed during {stage} — generations may not touch test data (work on copies)",
                        timings=timings,
                    )
            except Exception:
                return _fail(stage, "protected_data_modified", f"cannot verify protected data file '{rel}'", timings=timings)
        return None

    def emit(stage: str, extra: Optional[Dict[str, Any]] = None):
        if progress:
            progress(stage, extra or {})

    # Per-project live-telemetry protocol: harnesses print "<token> k=v ..."
    # lines during scoring; the pipeline streams them to the dashboard.
    telemetry = spec.get("telemetry") or {}
    progress_token = telemetry.get("progress_token") if telemetry.get("enabled", True) else None
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)



    # ── BUILD ────────────────────────────────────────────────────────────
    build_step = steps.get("build")
    build_fixes: List[str] = []
    if build_step:
        build_step = _materialize_inline(build_step, project, "build", 0)
        artifact_path = workdir / str(spec.get("artifact_name", "program"))
        # The build program receives the target artifact path as {artifact}.
        cmd = expand_command(build_step, project, candidate, artifact_path, workdir)
        ok, reason = check_command(cmd, project=spec, project_dir=project.path)
        if not ok:
            return _fail("build", "guardrail_denied", reason)
        emit("build", {"status": "running"})
        t0 = time.time()
        res = run_subprocess(
            cmd,
            timeout=build_step.get("timeout"),
            memory_limit_bytes=_mb(build_step.get("memory_limit_mb")),
            cwd=project.path,
            progress=lambda info, _st="build": emit(_st, {
                "status": "running", "live": info.get("live", {}),
                "elapsed": info.get("elapsed"), "rss": info.get("rss"), "pid": info.get("pid"),
            }),
            progress_token=progress_token,
        )
        timings["build"] = time.time() - t0
        # Auto-fix: gcc often tells us exactly what's wrong ("did you
        # forget to include <X>?", "did you mean 'Y'?").  Engaged when the
        # build FAILS or its stderr carries hints (warnings with
        # suggestions).  Three modes: off / default fixer / custom
        # per-project fixer script (skills.autofix_build = true|false|path).
        from .autofix import MAX_FIX_TRIES, autofix_build, parse_hints, resolve_mode
        mode = resolve_mode(spec)
        stderr0 = res.get("stderr") or ""
        _inc0, _mean0 = parse_hints(stderr0)
        if mode != "off" and (not res["ok"] or _inc0 or _mean0):
            if mode == "default":
                def _run(cmd: List[str]) -> Dict[str, Any]:
                    return run_subprocess(
                        cmd,
                        timeout=build_step.get("timeout"),
                        memory_limit_bytes=_mb(build_step.get("memory_limit_mb")),
                        cwd=project.path,
                    )
                # Compile-loop cap: engine override (KAI AUTOFIX tries <n>)
                # or the framework default (5).
                _max_tries = int((context.get("autofix_max_tries")
                                  if context.get("autofix_max_tries") is not None
                                  else MAX_FIX_TRIES))
                fixed, fixes, last = autofix_build(candidate, cmd, _run,
                                                   apply_on_success=True,
                                                   max_tries=_max_tries)
                res = last
                build_fixes = fixes
                if fixes:
                    emit("build", {"status": "fixed", "fixes": fixes})
            elif mode == "python":
                from . import linters
                if not res["ok"]:
                    fixed, notes = linters.autofix_python(candidate)
                    if fixed:
                        emit("build", {"status": "fixed", "fixes": notes})
                        res = run_subprocess(
                            cmd,
                            timeout=build_step.get("timeout"),
                            memory_limit_bytes=_mb(build_step.get("memory_limit_mb")),
                            cwd=project.path,
                        )
                    build_fixes = notes
            elif not res["ok"]:

                # Custom fixer script (user-authored, per-project).
                # Contract: python3 <fixer> <candidate> <artifact>
                # <project_dir> <workdir> -- <build_command...>; it runs
                # the build itself, applies its own fixes, rebuilds, and
                # exits 0 once the artifact exists (else non-zero with
                # the failure on stderr).
                fpath = project.resolve_program(mode[1])
                fix_cmd = ["python3", str(fpath), str(candidate),
                           str(artifact_path), str(project.path), str(workdir),
                           "--"] + list(cmd)
                ok_cmd, why = check_command(fix_cmd, project=spec, project_dir=project.path)
                if ok_cmd:
                    fres = run_subprocess(
                        fix_cmd,
                        timeout=build_step.get("timeout"),
                        memory_limit_bytes=_mb(build_step.get("memory_limit_mb")),
                        cwd=project.path,
                    )
                    if fres.get("ok") and artifact_path.exists():
                        res = fres
                        build_fixes = ["custom fixer"]
                        emit("build", {"status": "fixed", "fixes": ["custom fixer"]})
                    else:
                        res = {"ok": False, "returncode": fres.get("returncode"),
                               "stdout": fres.get("stdout", ""), "stderr": fres.get("stderr", "")}
                else:
                    res = {"ok": False, "returncode": None, "stdout": "",
                           "stderr": f"custom fixer blocked: {why}"}
        if not res["ok"]:
                extra = ""
                if res.get("timed_out"):
                    extra = f" (TIMED OUT after {res.get('seconds', 0):.0f}s)"
                elif res.get("mem_exceeded"):
                    extra = " (MEMORY LIMIT EXCEEDED)"
                lint_tail = ""
                try:
                    from . import linters
                    lnotes = linters.lint_notes(spec.get("language", "c"), str(candidate))
                    if lnotes:
                        lint_tail = "\nLINT:\n" + "\n".join(lnotes)
                except Exception:
                    pass
                return _fail(
                    "build", "build_fail",
                    f"build failed (exit={res['returncode']}){extra}: {_tail(res['stderr'])}{lint_tail}",
                    stdout_tail=_tail(res["stdout"]), stderr_tail=_tail(res["stderr"]),
                    timings=timings,
                    build_fixes=build_fixes,
                )
        if not artifact_path.exists():
            return _fail(
                "build", "build_fail",
                f"build succeeded but artifact not produced at {artifact_path.name}",
                stdout_tail=_tail(res["stdout"]), stderr_tail=_tail(res["stderr"]),
                timings=timings,
            )
        bad = verify_protected("build")
        if bad:
            return bad
        artifact = str(artifact_path)
        emit("build", {"status": "done", "artifact": artifact})

    # ── VERIFY ───────────────────────────────────────────────────────────
    for i, step in enumerate(steps.get("verify", []) or []):
        step = _materialize_inline(step, project, "verify", i)
        cmd = expand_command(step, project, candidate, artifact, workdir)
        ok, reason = check_command(cmd, project=spec, project_dir=project.path)
        if not ok:
            return _fail("verify", "guardrail_denied", reason, timings=timings)
        emit("verify", {"index": i, "status": "running"})
        t0 = time.time()
        res = run_subprocess(
            cmd,
            timeout=step.get("timeout"),
            memory_limit_bytes=_mb(step.get("memory_limit_mb")),
            cwd=project.path,
            progress=lambda info, _st="verify", _i=i: emit(_st, {
                "index": _i, "status": "running", "live": info.get("live", {}),
                "elapsed": info.get("elapsed"), "rss": info.get("rss"), "pid": info.get("pid"),
            }),
            progress_token=progress_token,
        )
        timings[f"verify{i}"] = time.time() - t0
        if not res["ok"]:
            extra = ""
            if res.get("timed_out"):
                extra = f" (TIMED OUT after {res.get('seconds', 0):.0f}s)"
            elif res.get("mem_exceeded"):
                extra = " (MEMORY LIMIT EXCEEDED)"
            return _fail(
                "verify", "verify_fail",
                f"verify[{i}] failed (exit={res['returncode']}){extra}: {_tail(res['stderr'] or res['stdout'])}",
                stdout_tail=_tail(res["stdout"]), stderr_tail=_tail(res["stderr"]),
                timings=timings,
            )
        bad = verify_protected(f"verify[{i}]")
        if bad:
            return bad
        emit("verify", {"index": i, "status": "done"})

    for i, step in enumerate(steps.get("score", []) or []):
        step = _materialize_inline(step, project, "score", i)
        cmd = expand_command(step, project, candidate, artifact, workdir)

        ok, reason = check_command(cmd, project=spec, project_dir=project.path)
        if not ok:
            return _fail("score", "guardrail_denied", reason, timings=timings)
        emit("score", {"index": i, "status": "running"})

        samples = [0]

        def score_progress(info: Dict[str, Any], _st: str = "score", _i: int = i):
            emit(_st, {
                "index": _i, "status": "running", "live": info.get("live", {}),
                "elapsed": info.get("elapsed"), "rss": info.get("rss"), "pid": info.get("pid"),
            })
            live = info.get("live") or {}
            if not live:
                return
            samples[0] += 1
            if samples[0] < 3:
                return  # cold-start noise: don't kill on the first samples
            for key, rule in abort_rules.items():
                if key not in live:
                    continue
                try:
                    val = float(live[key])
                except (TypeError, ValueError):
                    continue
                limit = float(rule["limit"])
                direction = rule["direction"]
                if (direction == "lower" and val > limit) or (direction == "higher" and val < limit):
                    raise RunAbort(f"{key}={val} beyond {limit}")

        t0 = time.time()
        res = run_subprocess(
            cmd,
            timeout=step.get("timeout"),
            memory_limit_bytes=_mb(step.get("memory_limit_mb")),
            cwd=project.path,
            progress=score_progress,
            progress_token=progress_token,
        )
        timings[f"score{i}"] = time.time() - t0
        if res.get("aborted"):
            return _fail(
                "score", "score_early_abort",
                f"score[{i}] aborted early: live metric is already worse than the champion threshold",
                timings=timings,
            )
        combined = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
        step_metrics = parse_metrics(combined, step.get("parse", []))
        score_details.append({
            "index": i,
            "exit": res.get("returncode"),
            "seconds": round(timings[f"score{i}"], 3),
            "max_rss_bytes": res.get("max_rss_bytes"),
            "metrics": step_metrics,
            "stdout_tail": _tail(res.get("stdout", "")),
            "stderr_tail": _tail(res.get("stderr", "")),
        })
        if not res["ok"]:
            extra = ""
            if res.get("timed_out"):
                extra = f" (TIMED OUT after {res.get('seconds', 0):.0f}s)"
            elif res.get("mem_exceeded"):
                extra = " (MEMORY LIMIT EXCEEDED)"
            return _fail(
                "score", "score_fail",
                f"score[{i}] failed (exit={res['returncode']}){extra}: {_tail(res['stderr'] or res['stdout'])}",
                stdout_tail=_tail(res["stdout"]), stderr_tail=_tail(res["stderr"]),
                timings=timings, metrics=step_metrics,
            )
        metrics.update(step_metrics)
        bad = verify_protected(f"score[{i}]")
        if bad:
            return bad
        emit("score", {"index": i, "status": "done", "metrics": step_metrics})

    if not metrics:
        return _fail(
            "score", "no_metrics",
            "no metrics parsed from score output — check parse rules in project spec",
            timings=timings,
        )

    return {
        "ok": True,
        "stage": "done",
        "outcome": "ok",
        "reason": "",
        "metrics": metrics,
        "artifact": artifact,
        "timings": timings,
        "score_details": score_details,
        "build_fixes": build_fixes,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _mb(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value) * 1024 * 1024)
    except (TypeError, ValueError):
        return None


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
