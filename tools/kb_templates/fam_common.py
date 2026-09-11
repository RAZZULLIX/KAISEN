# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KernelBench FAMILY pack bridge — shared harness helpers (generated).

A family project evolves ONE python file: a pack of drop-in `ModelNew_<i>`
replacements.  The PROBLEMS (reference models, inputs, tolerances, graded
slice) live here, in the harness — never in the model's prompt.

Every eval is pinned to one physical GPU (by UUID) before torch is
imported and serialized machine-wide with a per-GPU file lock, so timings
stay comparable no matter how many engines run.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_DIR = HERE.parent
MANIFEST = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))

FAMILY = str(MANIFEST["family"])
FAMILY_LABEL = str(MANIFEST.get("label") or FAMILY)
PROBLEMS = list(MANIFEST["problems"])            # [{index, name, reference, class, ...}]
SLICE = list(MANIFEST.get("score_slice") or [p["index"] for p in PROBLEMS])
STRICT = bool(MANIFEST.get("strict", True))
KB_REPO = Path(MANIFEST["kb_repo"])
GPU_UUID = str(MANIFEST.get("gpu_uuid") or "")
GPU_LOCK = str(MANIFEST.get("gpu_lock") or "/tmp/kaisen-kb-gpu.lock")
LOCK_TIMEOUT = float(MANIFEST.get("lock_timeout") or 2400.0)
EXCESSIVE_SPEEDUP = float(MANIFEST.get("excessive_speedup") or 10.0)
WARM_SECONDS = float(MANIFEST.get("warm_seconds") or 2.0)

BUILD_DIR = PROJECT_DIR / ".kb_cache"
BUILD_LOCK = str(BUILD_DIR / ".build.lock")


def pin_gpu() -> None:
    """Pin the eval to one physical GPU.  MUST run before `import torch`."""
    if GPU_UUID:
        os.environ["CUDA_VISIBLE_DEVICES"] = GPU_UUID
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TORCH_EXTENSIONS_DIR", str(BUILD_DIR))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def load_kb_eval():
    src = str(KB_REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from kernelbench import eval as kb_eval  # noqa: E402

    return kb_eval


def warm_gpu(seconds: float = WARM_SECONDS) -> None:
    """Steady-state clocks before timing (idle GPU clocks skew the ratio)."""
    try:
        import torch
    except Exception:
        return
    if not torch.cuda.is_available():
        return
    a = torch.randn(4096, 4096, device="cuda")
    b = torch.randn(4096, 4096, device="cuda")
    t0 = time.time()
    while time.time() - t0 < seconds:
        a @ b
    torch.cuda.synchronize()
    del a, b
    torch.cuda.empty_cache()


def read_candidate(arg: str) -> str:
    return Path(arg).read_text(encoding="utf-8")


def reference_src(problem: dict) -> str:
    return (PROJECT_DIR / problem["reference"]).read_text(encoding="utf-8")


def problems_for(which: str = "slice") -> list:
    """Graded set.  `fit` is decided once, at generation time, by a fresh-
    process probe per problem (a problem this machine cannot evaluate must
    never be silently scored 0)."""
    ok = [p for p in PROBLEMS if p.get("fit", True)]
    if which == "full":
        return ok
    by_index = {p["index"]: p for p in ok}
    return [by_index[i] for i in SLICE if i in by_index]


ROTATE_VERIFY = int(MANIFEST.get("rotate_verify") or 2)


def current_generation(candidate: str) -> int:
    """Generation number from the candidate path (runs/gen_NNNNNN/...)."""
    m = re.search(r"gen_(\d+)", str(candidate))
    return int(m.group(1)) if m else 0


def verify_problems(gen: int) -> list:
    """Everything that must be CORRECT for a generation to count.

    The graded slice (fixed metric, unchanged champions' denominator) PLUS a
    rotating window over the rest of the pack — a kernel the model broke
    outside the slice then FAILS the generation and is named in the report
    instead of shipping silently.  With K per generation every kernel is
    gated at least once every ceil(N/K) generations."""
    fit = [p for p in PROBLEMS if p.get("fit", True)]
    by_index = {p["index"]: p for p in fit}
    graded = [by_index[i] for i in SLICE if i in by_index]
    graded_ids = {p["index"] for p in graded}
    rest = [p for p in fit if p["index"] not in graded_ids]
    k = max(0, int(ROTATE_VERIFY))
    if not rest or k <= 0:
        return graded
    k = min(k, len(rest))
    start = (gen * k) % len(rest)
    return graded + [rest[(start + i) % len(rest)] for i in range(k)]


class FileLock:
    """Exclusive advisory lock on a file (fcntl.flock), with a wait cap."""

    def __init__(self, path: str, timeout: float = LOCK_TIMEOUT):
        self.path = str(path)
        self.timeout = float(timeout)
        self._fh = None

    def __enter__(self) -> "FileLock":
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        t0 = time.time()
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() - t0 > self.timeout:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(
                        f"lock busy for more than {self.timeout:.0f}s: {self.path}")
                time.sleep(0.25)

    def __exit__(self, *exc) -> None:
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def gpu_lock(timeout: float = LOCK_TIMEOUT) -> FileLock:
    return FileLock(GPU_LOCK, timeout=timeout)


def install_chunked_allclose(chunk_mb: int = 192) -> None:
    """Make KernelBench's correctness check fit a 24 GB card.

    KB compares the candidate against the reference with
    `torch.allclose(out, out_new, atol, rtol)` on the FULL tensors; for KB's
    large L1 problems that transiently needs ~3x the tensor size (a 6.4 GB
    output pair wanted another ~19 GB) and OOMs a 24 GB device.  The
    criterion is elementwise — |a - b| <= atol + rtol * |b| — so it can be
    evaluated in slices with EXACTLY the same boolean result and a bounded
    peak (one chunk at a time).  Nothing about the tolerance semantics
    changes; only the memory strategy."""
    import torch
    if getattr(torch, "_kb_chunked_allclose", False):
        return
    original = torch.allclose

    def chunked(a, b, rtol=1e-5, atol=1e-8, equal_nan=False):
        if not (isinstance(a, torch.Tensor) and isinstance(b, torch.Tensor)) \
                or a.shape != b.shape or a.numel() == 0 \
                or not (a.is_cuda and b.is_cuda):
            return original(a, b, rtol=rtol, atol=atol, equal_nan=equal_nan)
        fa, fb = a.reshape(-1), b.reshape(-1)
        step = max(1, (chunk_mb * 2 ** 20) // max(1, a.element_size()))
        for i in range(0, fa.numel(), step):
            x, y = fa[i:i + step], fb[i:i + step]
            ok = (x - y).abs() <= (atol + rtol * y.abs())
            if equal_nan:
                ok = ok | (x.isnan() & y.isnan())
            if not bool(ok.all()):
                return False
            del x, y, ok
        return True

    torch.allclose = chunked
    torch._kb_chunked_allclose = True


def eval_problem(problem: dict, kb_eval, cand_src: str, *,
                 measure: bool, correct_trials: int, perf_trials: int):
    """One KernelBench evaluation of the pack's class for this problem.

    KernelBench's evaluator looks up a class literally named `ModelNew` in
    the executed context, so the pack's class is aliased for the call —
    everything else (correctness trials, tolerances, CUDA-event timing,
    excessive-speedup flag) stays KB's own code."""
    install_chunked_allclose()
    aliased = (cand_src
               + f"\n\n# --- harness alias: KB's evaluator resolves `ModelNew` ---\n"
               + f"ModelNew = {problem['class']}\n")
    return kb_eval.eval_kernel_against_ref(
        reference_src(problem),
        aliased,
        num_correct_trials=correct_trials,
        num_perf_trials=perf_trials,
        measure_performance=measure,
        timing_method="cuda_event",
        check_for_excessive_speedup=measure,
        excessive_speedup_threshold=EXCESSIVE_SPEEDUP,
        verbose=False,
        build_dir=str(BUILD_DIR),
    )


def failure_detail(res) -> str:
    if res is None:
        return "eval returned None (compile lock contention?)"
    md = dict(getattr(res, "metadata", {}) or {})
    if md.get("compilation_error"):
        return "COMPILE: " + str(md.get("compilation_error"))[:240]
    if md.get("runtime_error"):
        return "RUNTIME: " + str(md.get("runtime_error"))[:240]
    if md.get("correctness_issue"):
        out = str(md.get("correctness_issue"))
        if md.get("max_difference"):
            out += f" max_diff={md['max_difference'][-1]}"
        return f"INCORRECT: {out} trials={md.get('correctness_trials', '?')}"
    if not getattr(res, "compiled", False):
        return "NOT COMPILED"
    if not getattr(res, "correctness", False):
        return "INCORRECT (no detail)"
    return "unknown failure"


def duplicate_ext_names(src: str) -> list:
    """load_inline extension names that appear more than once.

    The pack's kernels all live in ONE process: two `load_inline(name=...)`
    calls with the same name but different sources silently collide (torch
    caches by name), which would grade the wrong binary.  Hard blocker."""
    import re
    names = re.findall(
        r"load_inline\(\s*[^)]*?name\s*=\s*[\"']([^\"']+)[\"']", src, re.S)
    seen, dupes = set(), []
    for n in names:
        if n in seen:
            dupes.append(n)
        seen.add(n)
    return sorted(set(dupes))
