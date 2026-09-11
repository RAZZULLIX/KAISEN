#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KernelBench -> KAISEN factory: one KAISEN project per KernelBench problem.

Each generated project evolves ONE python file (`ModelNew`) against that
problem's own harness, built on KernelBench's own evaluator:

    build  -> compile the candidate's torch extension (kb_eval.build_compile_cache)
    verify -> correctness vs the reference model (N randomized trials, fp32 tol)
    score  -> measured speedup = ref_time / kernel_time (CUDA events)  => metric `speedup`

Context discipline: a project carries ONLY its own problem (reference
source + contract + its own champion) — nothing from other problems ever
enters the prompt.

GPU: every eval is pinned (by UUID) to one physical device (`--gpu`, default
the first GPU whose name contains `--gpu-match`, default "3090") and
serialized machine-wide by a per-GPU file lock, so timings stay comparable
no matter how many engines run.

Usage:
    python3 tools/kb_factory.py --levels 1 --limit 3      # generate a sample
    python3 tools/kb_factory.py --check kb-l1-001-...     # smoke one project
    python3 tools/kb_factory.py --levels 1,2,3            # the full batch
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kaisen.projects import PROJECTS_DIR, _merge_defaults, validate_spec  # noqa: E402
from kaisen.util import save_json  # noqa: E402

TEMPLATES = ROOT / "tools" / "kb_templates"
DEFAULT_KB_REPO = Path(os.environ.get("KB_REPO", str(ROOT.parent / "KernelBench")))
LEVELS = (1, 2, 3, 4)

# Eval sizing per level.  Correctness trials cost GPU time; L3 models are
# heavy, L4 loads HF weights.  Perf trials drive the timing noise floor —
# KB's own baselines were collected with 100 trials, so match that where the
# per-call cost allows it.
NUM_CORRECT = {1: 3, 2: 3, 3: 2, 4: 1}
NUM_PERF = {1: 100, 2: 100, 3: 30, 4: 10}
# (build, verify, score) step timeouts in seconds.
STEP_TIMEOUT = {
    1: (900.0, 900.0, 1200.0),
    2: (900.0, 900.0, 1200.0),
    3: (1800.0, 1800.0, 2400.0),
    4: (1800.0, 2400.0, 3000.0),
}
EXCESSIVE_SPEEDUP = 10.0
LOCK_TIMEOUT = 1800.0

GOAL_TMPL = """TASK — KernelBench level {level}, problem {num}: {name}

Write `class ModelNew(torch.nn.Module)`: a FASTER drop-in replacement for the reference PyTorch `Model` below. It must take the same constructor arguments (from `get_init_inputs()`) and produce the same outputs on the same inputs.

Scoring: speedup = mean(reference model time) / mean(ModelNew time), measured with CUDA events on {gpu} ({trials} trials, fp32, identical seeded inputs). The reference eager implementation defines 1.0x. Beating 1.0x needs a real kernel-level win — fused ops, fewer passes/launches, better memory behaviour — not micro-tweaks.

REFERENCE IMPLEMENTATION (this is what you must replace and beat):
```python
{ref}
```
"""

CONTRACT_TMPL = """HARD CONTRACT
- ONE self-contained python file defining `class ModelNew(torch.nn.Module)` at module level, with its CUDA code inline.
- CUDA kernels: build ONE extension via `torch.utils.cpp_extension.load_inline(...)` at module scope; `forward()` calls it. (Triton is allowed the same way.)
- Same `__init__` signature as the reference `Model` — the evaluator constructs `ModelNew(*get_init_inputs())`.
- `forward()` takes the same tensor arguments (CUDA, fp32) and returns the same dtype/shape, matching the reference within atol=rtol=1e-4.
- The math must be genuine on every call: no caching results between calls, no skipping work, no non-default CUDA streams. The evaluator flags suspicious speedups (>{excessive}x) as reward hacking and rejects them.
- No file I/O, no network, no subprocess, no `os.system`, no `eval/exec`. Do not read or modify `reference.py`.
- Import must be fast: definitions only, no work executed at import time.
"""


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def discover(kb_repo: Path, levels) -> list[dict]:
    out: list[dict] = []
    for level in levels:
        d = kb_repo / "KernelBench" / f"level{level}"
        if not d.is_dir():
            raise SystemExit(f"no such level dir: {d}")
        for path in sorted(d.glob("*.py")):
            m = re.match(r"^(\d+)_(.+)\.py$", path.name)
            if not m:
                continue
            out.append({
                "level": level,
                "num": int(m.group(1)),
                "name": m.group(2),
                "path": path,
            })
    out.sort(key=lambda p: (p["level"], p["num"]))
    return out


def project_id(prob: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prob["name"].lower()).strip("-")[:28].strip("-")
    base = f"kb-l{prob['level']}-{prob['num']:03d}"
    return f"{base}-{slug}" if slug else base


CLASS_RE = re.compile(r"^class\s+Model\s*\(", re.M)
MODEL_WORD_RE = re.compile(r"\bModel\b(?!New)")


def baseline_source(ref_src: str) -> str:
    """The starting candidate: the reference model with its class renamed —
    correct by construction, speedup 1.0x.

    EVERY whole-word `Model` is renamed (not just the class statement):
    KB shares one exec context between the reference and the candidate, so
    a leftover `super(Model, self)` would bind to the OLD class and blow up
    with `super(type, obj): obj must be an instance or subtype of type`.
    """
    out = MODEL_WORD_RE.sub("ModelNew", ref_src)
    if "class ModelNew(" not in out:
        raise ValueError("reference source has no `class Model(` to clone")
    return out


def discover_gpu(match: str) -> tuple[str, str]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,uuid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as e:  # pragma: no cover
        raise SystemExit(f"nvidia-smi failed: {e}")
    for row in out.strip().splitlines():
        parts = [p.strip() for p in row.split(",")]
        if len(parts) >= 3 and match.lower() in parts[1].lower():
            return parts[1], parts[2]
    raise SystemExit(f"no GPU matching {match!r} — pass --gpu <uuid>")


# --------------------------------------------------------------------------- #
# project generation
# --------------------------------------------------------------------------- #

def build_spec(prob: dict, ref_src: str, gpu_name: str) -> dict:
    level, num, name = prob["level"], prob["num"], prob["name"]
    pid = project_id(prob)
    b_t, v_t, s_t = STEP_TIMEOUT[level]
    goal = GOAL_TMPL.format(level=level, num=num, name=name, gpu=gpu_name,
                            trials=NUM_PERF[level], ref=ref_src.rstrip())
    contract = CONTRACT_TMPL.format(excessive=int(EXCESSIVE_SPEEDUP))
    return {
        "id": pid,
        "name": f"KB L{level} #{num} {name[:40]}",
        "description": f"KernelBench level {level} problem {num}: {name}",
        "language": "python",
        "artifact_name": "build.log",
        "steps": {
            "build": {
                "program": "python3",
                "args": ["harness/build.py", "{candidate}", "{artifact}"],
                "timeout": b_t,
            },
            "verify": [{
                "program": "python3",
                "args": ["harness/verify.py", "{candidate}"],
                "timeout": v_t,
            }],
            "score": [{
                "program": "python3",
                "args": ["harness/score.py", "{candidate}"],
                "timeout": s_t,
                "parse": [{"type": "regex",
                           "pattern": r"speedup=(?P<speedup>[\d.]+)"}],
            }],
        },
        "metrics": {
            "speedup": {
                "label": "Speedup vs reference model",
                "unit": "x",
                "direction": "higher",
                "weight": 1.0,
            }
        },
        "telemetry": {"enabled": True, "progress_token": "KAISEN_PROGRESS",
                      "live_fields": ["speedup"]},
        "engine": {"workers": 4, "multi": 1,
                   "autofix": {"tries": 2, "repair": 2}},
        "select": {"hysteresis": 1.05},
        "guardrails": {"enabled": True, "allow_extra": [], "deny_extra": []},
        "prompts": {
            "goal": goal,
            # Tight context: no memory/lessons/history blocks (gpt-oss budget).
            "generation_blocks": ["contract", "metrics", "current_code",
                                  "output_format"],
        },
        "skills": {"analyze": True, "dedup": True,
                   "deepwork": {"enabled": False}, "lessons": False},
        "data": {
            "baseline_source": "original.py",
            "protected_files": ["reference.py"],
            "contract_text": contract,
    },
    }


def write_project(spec: dict, prob: dict, kb_repo: Path, gpu_uuid: str,
                  gpu_name: str) -> Path:
    pid = spec["id"]
    pdir = PROJECTS_DIR / pid
    if pdir.exists():
        shutil.rmtree(pdir)
    (pdir / "harness").mkdir(parents=True)
    (pdir / "prompts").mkdir()

    ref_src = prob["path"].read_text(encoding="utf-8")
    (pdir / "reference.py").write_text(ref_src, encoding="utf-8")
    (pdir / "original.py").write_text(baseline_source(ref_src), encoding="utf-8")

    for name in ("kb_common.py", "build.py", "verify.py", "score.py"):
        shutil.copy2(TEMPLATES / name, pdir / "harness" / name)
    for name in ("build.py", "verify.py", "score.py"):
        os.chmod(pdir / "harness" / name, 0o755)

    level = prob["level"]
    meta = {
        "kb_repo": str(kb_repo),
        "level": level,
        "problem_num": prob["num"],
        "problem_name": prob["name"],
        "problem_file": str(prob["path"].relative_to(kb_repo)),
        "reference_file": "reference.py",
        "gpu_uuid": gpu_uuid,
        "gpu_name": gpu_name,
        "gpu_lock": f"/tmp/kaisen-kb-gpu-{gpu_uuid}.lock",
        "num_correct_trials": NUM_CORRECT[level],
        "num_perf_trials": NUM_PERF[level],
        "excessive_speedup": EXCESSIVE_SPEEDUP,
        "lock_timeout": LOCK_TIMEOUT,
    }
    (pdir / "harness" / "_kb_problem.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    spec = _merge_defaults(spec)
    spec["id"] = pid
    errors = validate_spec(spec)
    if errors:
        shutil.rmtree(pdir, ignore_errors=True)
        raise SystemExit(f"{pid}: invalid spec: {'; '.join(errors)}")
    save_json(pdir / "project.json", spec)
    return pdir


# --------------------------------------------------------------------------- #
# direct harness check (no KAISEN pipeline involved)
# --------------------------------------------------------------------------- #

STAGES = (("build", ["harness/build.py", "original.py", "{tmp}/build.log"]),
          ("verify", ["harness/verify.py", "original.py"]),
          ("score", ["harness/score.py", "original.py"]))


def check_project(pid: str, timeout_scale: float = 1.0) -> list[str]:
    pdir = PROJECTS_DIR / pid
    if not (pdir / "project.json").exists():
        return [f"{pid}: no such project"]
    errs: list[str] = []
    tmp = Path("/tmp") / f"kb_check_{pid}"
    tmp.mkdir(parents=True, exist_ok=True)
    for stage, argv in STAGES:
        argv = [a.replace("{tmp}", str(tmp)) for a in argv]
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, *argv], cwd=pdir,
                               capture_output=True, text=True,
                               timeout=2400 * timeout_scale)
        except subprocess.TimeoutExpired:
            errs.append(f"{stage}: TIMEOUT")
            continue
        took = time.time() - t0
        tail = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode != 0:
            err = (r.stderr.strip().splitlines() or [""])[-1]
            errs.append(f"{stage}: rc={r.returncode} in {took:.0f}s — {err[:200]}")
        else:
            print(f"  {stage:6s} ok  {took:6.1f}s  {tail[:80]}")
    return errs


# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kb-repo", default=str(DEFAULT_KB_REPO))
    ap.add_argument("--levels", default="1",
                    help="comma-separated levels to generate (default 1)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N problems of each level (0 = all)")
    ap.add_argument("--force", action="store_true",
                    help="regenerate projects that already exist")
    ap.add_argument("--gpu", default=None, help="GPU UUID to pin evals to")
    ap.add_argument("--gpu-match", default="3090",
                    help="substring to pick the GPU by name (default 3090)")
    ap.add_argument("--check", default=None,
                    help="no generation: run build/verify/score of one project "
                         "on its baseline")
    args = ap.parse_args(argv)

    if args.check:
        errs = check_project(args.check)
        for e in errs:
            print(f"  ERR  {e}", file=sys.stderr)
        return 1 if errs else 0

    kb_repo = Path(args.kb_repo).resolve()
    if not (kb_repo / "src" / "kernelbench" / "eval.py").exists():
        raise SystemExit(f"not a KernelBench checkout: {kb_repo}")

    levels = [int(x) for x in str(args.levels).split(",") if x.strip()]
    gpu_name, gpu_uuid = discover_gpu(args.gpu_match) if not args.gpu \
        else (f"pinned:{args.gpu}", args.gpu)
    print(f"KernelBench: {kb_repo}\nGPU pinned: {gpu_name} ({gpu_uuid})")

    problems = discover(kb_repo, levels)
    if args.limit:
        per_level: dict[int, int] = {}
        kept = []
        for p in problems:
            n = per_level.get(p["level"], 0)
            if n < args.limit:
                kept.append(p)
                per_level[p["level"]] = n + 1
        problems = kept
    print(f"problems: {len(problems)} (levels {levels})")

    created, skipped, failed = [], [], []
    for i, prob in enumerate(problems, 1):
        pid = project_id(prob)
        pdir = PROJECTS_DIR / pid
        if pdir.exists() and not args.force:
            skipped.append(pid)
            continue
        try:
            spec = build_spec(prob, prob["path"].read_text(encoding="utf-8"),
                              gpu_name)
            write_project(spec, prob, kb_repo, gpu_uuid, gpu_name)
            created.append(pid)
        except Exception as e:
            failed.append(f"{pid}: {e}")
        if i % 25 == 0 or i == len(problems):
            print(f"  ... {i}/{len(problems)} ({len(created)} created, "
                  f"{len(failed)} failed)")

    print(f"\ncreated {len(created)}  skipped {len(skipped)}  failed {len(failed)}")
    for f in failed:
        print(f"  FAIL {f}", file=sys.stderr)
    if created:
        print("sample:", ", ".join(created[:5]))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
