#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KernelBench FAMILY factory — one KAISEN project per operator family.

Each project evolves ONE python file: a pack of drop-in `ModelNew_<i>`
replacements.  The whole KernelBench family (references, input shapes,
tolerances, graded slice) lives in the project's HARNESS — the model's
prompt only ever sees the code it is improving.

    build  -> compile the pack + one forced forward per kernel
    verify -> every kernel in the graded slice matches its reference model
    score  -> mean speedup over the graded slice (0 for a broken kernel)
              + fast_p (fraction correct AND faster)
    score_full.py -> the same over the WHOLE family (audit, not pipeline)

Usage:
    python3 tools/kb_family_factory.py                 # build all families
    python3 tools/kb_family_factory.py --force         # rebuild
    python3 tools/kb_family_factory.py --check kb-matmul
"""
from __future__ import annotations

import argparse
import ast
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

# --------------------------------------------------------------------------- #
# families: (level, problem number, file) — the harness-side test set
# --------------------------------------------------------------------------- #

FAMILIES: dict[str, dict] = {
    "matmul": {
        "label": "KernelBench matmul family (L1 GEMM variants)",
        # NOTE: #9 (tall-skinny) is deliberately absent: its OUTPUT is
        # 32768x32768 fp32 = 4 GB, and KB's allclose-based correctness check
        # transiently needs >22 GB — it cannot be evaluated on a 24 GB card.
        "slice": [1, 16, 15],
        "problems": [
            (1, 1),
            (1, 2),
            (1, 3),
            (1, 4),
            (1, 5),
            (1, 6),
            (1, 7),
            (1, 8),
            (1, 10),
            (1, 11),
            (1, 12),
            (1, 13),
            (1, 14),
            (1, 15),
            (1, 16),
            (1, 17),
            (1, 18),
        ],
    },
    "conv": {
        "label": "KernelBench conv family (L1 + fused L2)",
        "slice": [1, 2, 3, 4, 5, 6],
        "problems": [
            (1, 50),
            (1, 82),
            (1, 87),
            (1, 54),
            (1, 57),
            (2, 1),
        ],
    },
    "actnorm": {
        "label": "KernelBench activation / norm / reduction family",
        "slice": [1, 2, 3, 4, 5, 6],
        "problems": [
            (1, 26),
            (1, 23),
            (1, 40),
            (1, 36),
            (1, 47),
            (1, 42),
        ],
    },
    "elementwise": {
        "label": "KernelBench elementwise activations (L1)",
        "slice": [1, 7],
        "problems": [
            (1, 19), (1, 20), (1, 21), (1, 22), (1, 24), (1, 25), (1, 27),
            (1, 28), (1, 29), (1, 30), (1, 31), (1, 32), (1, 88),
        ],
    },
    "norms": {
        "label": "KernelBench normalization layers (L1)",
        "slice": [1, 2],
        "problems": [(1, 33), (1, 34), (1, 35), (1, 37), (1, 38), (1, 39)],
    },
    "pooling": {
        "label": "KernelBench pooling (L1)",
        "slice": [1, 4],
        "problems": [(1, 41), (1, 43), (1, 44), (1, 45), (1, 46)],
    },
    "reduction": {
        "label": "KernelBench reductions (L1)",
        "slice": [1, 4],
        "problems": [(1, 48), (1, 49), (1, 51), (1, 52), (1, 53)],
    },
    "conv-std": {
        "label": "KernelBench standard/depthwise conv variants (L1)",
        "slice": [1, 11],
        "problems": [
            (1, 55), (1, 56), (1, 59), (1, 60), (1, 62), (1, 63), (1, 66),
            (1, 67), (1, 76), (1, 80), (1, 83), (1, 84), (1, 85), (1, 86),
        ],
    },
    "conv-t": {
        "label": "KernelBench transposed conv variants (L1)",
        "slice": [1, 7],
        "problems": [
            (1, 58), (1, 61), (1, 64), (1, 65), (1, 68), (1, 69), (1, 70),
            (1, 71), (1, 72), (1, 73), (1, 74), (1, 75), (1, 77), (1, 78),
            (1, 79), (1, 81),
        ],
    },
    "cumsum": {
        "label": "KernelBench cumulative ops (L1)",
        "slice": [1, 3],
        "problems": [(1, 89), (1, 90), (1, 91), (1, 92), (1, 93)],
    },
    "loss": {
        "label": "KernelBench losses + attention (L1)",
        "slice": [1, 2],
        "problems": [(1, 94), (1, 95), (1, 96), (1, 97), (1, 98), (1, 99),
                     (1, 100)],
    },
}

NUM_CORRECT = 3
NUM_PERF = 30
STEP_TIMEOUT = (2400.0, 2400.0, 3000.0)   # build, verify, score
EXCESSIVE_SPEEDUP = 10.0
LOCK_TIMEOUT = 2400.0

GOAL_TMPL = """TASK — {label}

This file is a PACK of drop-in kernel replacements for PyTorch reference models. Keep every `ModelNew_<i>` class name and its constructor signature; replace what each class DOES with a faster implementation (CUDA/Triton via `torch.utils.cpp_extension`, or a better-structured PyTorch computation where a kernel is not the answer).

The evaluation harness owns the reference models, the input shapes and the tolerances — it is NOT in this prompt and you do not need it: the naive implementation inside each class IS the semantics you must preserve exactly.

Scoring: speedup = mean over graded problems of (reference time / your time) on CUDA-event timing. A class that is wrong, slower, or missing scores 0 for its problem; the untouched naive pack scores 1.0x.
"""

CONTRACT_TMPL = """HARD CONTRACT
- ONE self-contained python file. Keep the exact class names ({classes}) — the harness looks them up by name.
- Each class: same `__init__` signature as the naive implementation it replaces; `forward()` returns the same values within atol=rtol=1e-4 (fp32, CUDA tensors).
- CUDA/Triton kernels: build with `torch.utils.cpp_extension.load_inline(...)`. Every extension name MUST be unique (torch caches by name — a collision silently grades the wrong binary), e.g. name="kb_{family}_p<i>".
- No module-level work at import beyond definitions and extension loading. No file I/O, no network, no subprocess, no eval/exec. Never read or modify harness files or reference files.
- Preserve exact semantics: no result caching between calls, no skipping work, no non-default CUDA streams, no precision tricks beyond fp32 tolerance. Speedups above {excessive}x are treated as reward hacking and rejected.
- Leaving a kernel unchanged is fine; BREAKING one costs its whole problem (score 0).
"""


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

MODEL_WORD_RE = re.compile(r"\bModel\b(?!New)")


def class_source(ref_src: str, cls_name: str) -> str:
    """Extract `class Model` from a KernelBench reference and rename it."""
    tree = ast.parse(ref_src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Model":
            lines = ref_src.splitlines()
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            out = MODEL_WORD_RE.sub(cls_name, body)
            if f"class {cls_name}(" not in out:
                raise ValueError("class rename failed")
            return out
    raise ValueError("reference has no `class Model`")


def problem_file(kb_repo: Path, level: int, num: int) -> Path:
    """Resolve `levelN/<num>_*.py` — the KB tree's own naming."""
    matches = sorted((kb_repo / "KernelBench" / f"level{level}").glob(f"{num}_*.py"))
    if len(matches) != 1:
        raise SystemExit(f"level{level}: expected exactly one file for problem "
                         f"{num}, found {[m.name for m in matches]}")
    return matches[0]


def problem_name(filename: str) -> str:
    stem = re.sub(r"^\d+_", "", filename[:-3])
    return stem.replace("__", " ").replace("_", " ").strip().rstrip(".")


def build_pack(family: str, problems: list[dict], kb_repo: Path) -> str:
    cfg = FAMILIES[family]
    parts = [f"# KernelBench family pack — {cfg['label']}\n"
             "# Each ModelNew_<i> replaces the reference model that problem i\n"
             "# is graded against (the references live in the harness).\n"
             "import math\n\nimport torch\nimport torch.nn as nn\n"
             "import torch.nn.functional as F\n"]
    for p in problems:
        ref = problem_file(kb_repo, p["level"], p["problem_num"]).read_text(
            encoding="utf-8")
        parts.append(f"\n# ---- [{p['index']}] {p['name']}"
                     f" (L{p['level']} #{p['problem_num']}) "
                     + "-" * 8 + "\n"
                     + class_source(ref, p["class"]) + "\n")
    return "\n".join(parts)


def build_spec(family: str, problems: list[dict], gpu_name: str) -> dict:
    pid = f"kb-{family}"
    cfg = FAMILIES[family]
    b_t, v_t, s_t = STEP_TIMEOUT
    classes = ", ".join(f"`{p['class']}`" for p in problems)
    goal = GOAL_TMPL.format(label=cfg["label"])
    contract = CONTRACT_TMPL.format(classes=classes, family=family,
                                    excessive=int(EXCESSIVE_SPEEDUP))
    return {
        "id": pid,
        "name": f"KB family: {family} ({len(problems)} kernels)",
        "description": f"{cfg['label']} — one pack file, graded over the "
                       f"harness's problem set",
        "language": "python",
        "artifact_name": "build.log",
        "steps": {
            "build": {"program": "python3",
                      "args": ["harness/build.py", "{candidate}", "{artifact}"],
                      "timeout": b_t},
            "verify": [{"program": "python3",
                        "args": ["harness/verify.py", "{candidate}"],
                        "timeout": v_t}],
            "score": [{"program": "python3",
                       "args": ["harness/score.py", "{candidate}"],
                       "timeout": s_t,
                       "parse": [{"type": "regex",
                                  "pattern": r"speedup=(?P<speedup>[\d.]+)"}]}],
        },
        "metrics": {"speedup": {
            "label": "Mean speedup over the graded set",
            "unit": "x", "direction": "higher", "weight": 1.0}},
        "telemetry": {"enabled": True, "progress_token": "KAISEN_PROGRESS",
                      "live_fields": ["speedup"]},
        "engine": {"workers": 4, "multi": 1,
                   "autofix": {"tries": 2, "repair": 2}},
        "select": {"hysteresis": 1.05},
        "guardrails": {"enabled": True, "allow_extra": [], "deny_extra": []},
        "prompts": {
            "goal": goal,
            "generation_blocks": ["contract", "metrics", "current_code",
                                  "output_format"],
        },
        "skills": {"analyze": True, "dedup": True,
                   "deepwork": {"enabled": False}, "lessons": False},
        "data": {
            "baseline_source": "original.py",
            "contract_text": contract,
            "protected_files": (["harness/manifest.json"]
                                + [p["reference"] for p in problems]
                                + ["harness/" + n for n in
                                   ("build.py", "verify.py", "score.py",
                                    "score_full.py", "fam_common.py",
                                    "fam_score.py", "fam_build.py",
                                    "fam_verify.py")]),
        },
    }


def write_project(family: str, kb_repo: Path, gpu_uuid: str, gpu_name: str) -> str:
    cfg = FAMILIES[family]
    pid = f"kb-{family}"
    pdir = PROJECTS_DIR / pid
    if pdir.exists():
        shutil.rmtree(pdir)
    (pdir / "harness").mkdir(parents=True)
    (pdir / "prompts").mkdir()

    problems: list[dict] = []
    for i, (level, num) in enumerate(cfg["problems"], 1):
        ref_name = f"reference_{i}.py"
        src = problem_file(kb_repo, level, num)
        shutil.copy2(src, pdir / ref_name)
        fname = src.name
        problems.append({
            "index": i,
            "name": problem_name(fname),
            "level": level,
            "problem_num": num,
            "file": fname,
            "reference": ref_name,
            "class": f"ModelNew_{i}",
            "correct_trials": NUM_CORRECT,
            "perf_trials": NUM_PERF,
        })

    (pdir / "original.py").write_text(
        build_pack(family, problems, kb_repo), encoding="utf-8")

    for name, dest in (("fam_common.py", "fam_common.py"),
                       ("fam_build.py", "build.py"),
                       ("fam_verify.py", "verify.py"),
                       ("fam_score.py", "score.py"),
                       ("fam_score.py", "fam_score.py"),
                       ("fam_score_full.py", "score_full.py"),
                       ("fam_probe.py", "probe.py")):
        shutil.copy2(TEMPLATES / name, pdir / "harness" / dest)
    for n in ("build.py", "verify.py", "score.py", "score_full.py"):
        os.chmod(pdir / "harness" / n, 0o755)

    by_index = {p["index"]: p for p in problems}
    manifest = {
        "family": family,
        "label": cfg["label"],
        "kb_repo": str(kb_repo),
        "gpu_uuid": gpu_uuid,
        "gpu_name": gpu_name,
        "gpu_lock": f"/tmp/kaisen-kb-gpu-{gpu_uuid}.lock",
        "lock_timeout": LOCK_TIMEOUT,
        "excessive_speedup": EXCESSIVE_SPEEDUP,
        "strict": True,
        "score_slice": [i for i in cfg["slice"] if i in by_index],
        "problems": problems,
    }
    (pdir / "harness" / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    spec = _merge_defaults(build_spec(family, problems, gpu_name))
    spec["id"] = pid
    errors = validate_spec(spec)
    if errors:
        shutil.rmtree(pdir, ignore_errors=True)
        raise SystemExit(f"{pid}: invalid spec: {'; '.join(errors)}")
    save_json(pdir / "project.json", spec)
    return pid


def probe_project(pid: str) -> list[str]:
    """Per-problem capacity + baseline-correctness probe, in a FRESH process.

    Decides `fit` once, from evidence: a problem the machine cannot evaluate
    (memory) or that the naive baseline cannot match is excluded from the
    graded set with the reason recorded in the manifest."""
    pdir = PROJECTS_DIR / pid
    man_path = pdir / "harness" / "manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    for p in man["problems"]:
        t0 = time.time()
        ok, line, used = False, "", 0
        # KB's correctness loop keeps the previous trial's outputs alive while
        # the next trial's inputs are built: for multi-GB problems that peak
        # OOMs a 24 GB card.  Walk the trial count down until it fits — the
        # fit is per problem and recorded, never guessed.
        for trials in (int(p.get("correct_trials") or 3), 2, 1):
            try:
                r = subprocess.run(
                    [sys.executable, "harness/probe.py", str(p["index"]),
                     "original.py", str(trials)],
                    cwd=pdir, capture_output=True, text=True, timeout=1800)
                out = (r.stdout or "").strip().splitlines()
                line = out[-1] if out else (r.stderr or "").strip()[-160:]
                ok = r.returncode == 0
            except subprocess.TimeoutExpired:
                ok, line = False, "PROBE TIMEOUT"
            if ok:
                used = trials
                break
        p["correct_trials"] = used or int(p.get("correct_trials") or 3)
        p["fit"] = ok
        p["fit_reason"] = "" if ok else str(line)[:200]
        mark = "ok  " if ok else "EXCL"
        extra = f"trials={used}" if ok and used != int(man["problems"][0].get("correct_trials") or 3) else ""
        print(f"    [{mark}] {p['index']:>3} {p['name'][:38]:<38} "
              f"{time.time() - t0:5.1f}s {extra} {p['fit_reason'][:80]}")
    fit = {p["index"] for p in man["problems"] if p["fit"]}
    man["score_slice"] = [i for i in man["score_slice"] if i in fit]
    man["excluded"] = {str(p["index"]): p["fit_reason"]
                       for p in man["problems"] if not p["fit"]}
    man_path.write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
    print(f"  graded slice {man['score_slice']} — excluded "
          f"{len(man['excluded'])}/{len(man['problems'])}")
    return list(man["excluded"].values())


# --------------------------------------------------------------------------- #

STAGES = (("build", ["harness/build.py", "original.py", "{tmp}/build.log"]),
          ("verify", ["harness/verify.py", "original.py"]),
          ("score", ["harness/score.py", "original.py"]))


def check_project(pid: str) -> list[str]:
    pdir = PROJECTS_DIR / pid
    if not (pdir / "project.json").exists():
        return [f"{pid}: no such project"]
    errs: list[str] = []
    tmp = Path("/tmp") / f"kb_fam_check_{pid}"
    tmp.mkdir(parents=True, exist_ok=True)
    for stage, argv in STAGES:
        argv = [a.replace("{tmp}", str(tmp)) for a in argv]
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, *argv], cwd=pdir,
                               capture_output=True, text=True, timeout=3000)
        except subprocess.TimeoutExpired:
            errs.append(f"{stage}: TIMEOUT")
            continue
        took = time.time() - t0
        out = r.stdout.strip().splitlines()
        tail = out[-1] if out else ""
        metrics = [l for l in out if l.startswith(("speedup=", "fast_p=", "valid="))]
        detail = " ".join(metrics) or tail
        if r.returncode != 0:
            err = (r.stderr.strip().splitlines() or [""])[-1]
            errs.append(f"{stage}: rc={r.returncode} in {took:.0f}s — {err[:240]}")
        else:
            print(f"  {stage:6s} ok  {took:6.1f}s  {detail[:110]}")
    return errs


def discover_gpu(match: str) -> tuple[str, str]:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,name,uuid", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=20).stdout
    for row in out.strip().splitlines():
        parts = [p.strip() for p in row.split(",")]
        if len(parts) >= 3 and match.lower() in parts[1].lower():
            return parts[1], parts[2]
    raise SystemExit(f"no GPU matching {match!r}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kb-repo", default=str(DEFAULT_KB_REPO))
    ap.add_argument("--families", default=",".join(FAMILIES))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--gpu", default=None)
    ap.add_argument("--gpu-match", default="3090")
    ap.add_argument("--check", default=None, help="run one project's harness")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the per-problem capacity probe")
    args = ap.parse_args(argv)

    if args.check:
        errs = check_project(args.check)
        for e in errs:
            print(f"  ERR  {e}", file=sys.stderr)
        return 1 if errs else 0

    kb_repo = Path(args.kb_repo).resolve()
    if not (kb_repo / "src" / "kernelbench" / "eval.py").exists():
        raise SystemExit(f"not a KernelBench checkout: {kb_repo}")
    gpu_name, gpu_uuid = (f"pinned:{args.gpu}", args.gpu) if args.gpu \
        else discover_gpu(args.gpu_match)
    print(f"KernelBench: {kb_repo}\nGPU pinned: {gpu_name} ({gpu_uuid})")

    created, failed = [], []
    for family in [f.strip() for f in args.families.split(",") if f.strip()]:
        if family not in FAMILIES:
            raise SystemExit(f"unknown family {family!r}")
        pid = f"kb-{family}"
        if (PROJECTS_DIR / pid).exists() and not args.force:
            print(f"skip {pid} (exists)")
            continue
        try:
            write_project(family, kb_repo, gpu_uuid, gpu_name)
            print(f"created {pid} ({len(FAMILIES[family]['problems'])} kernels)")
            if not args.no_probe:
                probe_project(pid)
            created.append(pid)
        except Exception as e:
            failed.append(f"{pid}: {e}")
    for f in failed:
        print(f"  FAIL {f}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
