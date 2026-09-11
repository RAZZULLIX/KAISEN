# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KernelBench bridge — shared harness helpers (generated; do not edit).

Every KernelBench project's harness pins ONE physical GPU (by UUID) before
torch is imported, points torch's extension cache at the project, and
serializes GPU work machine-wide with a per-GPU file lock, so two workers
never benchmark at the same time and timings stay comparable.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_DIR = HERE.parent
PROBLEM = json.loads((HERE / "_kb_problem.json").read_text(encoding="utf-8"))

KB_REPO = Path(PROBLEM["kb_repo"])
KB_LEVEL = int(PROBLEM["level"])
KB_PROBLEM_FILE = str(PROBLEM["problem_file"])
KB_PROBLEM_NUM = int(PROBLEM.get("problem_num") or 0)
GPU_UUID = str(PROBLEM.get("gpu_uuid") or "")
GPU_LOCK = str(PROBLEM.get("gpu_lock") or "/tmp/kaisen-kb-gpu.lock")
REFERENCE_FILE = str(PROBLEM.get("reference_file") or "reference.py")
NUM_PERF_TRIALS = int(PROBLEM.get("num_perf_trials") or 10)
NUM_CORRECT_TRIALS = int(PROBLEM.get("num_correct_trials") or 3)
EXCESSIVE_SPEEDUP = float(PROBLEM.get("excessive_speedup") or 10.0)
LOCK_TIMEOUT = float(PROBLEM.get("lock_timeout") or 1800.0)

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
    """Import KernelBench's own evaluator (single source of truth)."""
    src = str(KB_REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from kernelbench import eval as kb_eval  # noqa: E402

    return kb_eval


def warm_gpu(seconds: float = 2.0) -> None:
    """Bring the GPU to steady-state clocks BEFORE timing anything.

    Without this, whichever model is measured first runs at idle clocks and
    the speedup ratio swings by >10% on identical code.  Call inside the GPU
    lock, right before eval_kernel_against_ref."""
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


def read_reference() -> str:
    return (PROJECT_DIR / REFERENCE_FILE).read_text(encoding="utf-8")


def read_candidate(arg: str) -> str:
    return Path(arg).read_text(encoding="utf-8")


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
                        f"lock busy for more than {self.timeout:.0f}s: {self.path}"
                    )
                time.sleep(0.25)

    def __exit__(self, *exc) -> None:
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def gpu_lock(timeout: float = LOCK_TIMEOUT) -> FileLock:
    """The machine-wide GPU slot for this pinned device."""
    return FileLock(GPU_LOCK, timeout=timeout)


def failure_detail(res) -> str:
    """Compact reason string from a KernelExecResult (for stderr/reports)."""
    if res is None:
        return "eval returned None (compile lock contention?)"
    md = dict(getattr(res, "metadata", {}) or {})
    if md.get("compilation_error"):
        return "COMPILE: " + str(md.get("compilation_error"))[:300]
    if md.get("runtime_error"):
        return "RUNTIME: " + str(md.get("runtime_error"))[:300]
    if md.get("correctness_issue"):
        out = str(md.get("correctness_issue"))
        if md.get("max_difference"):
            out += f" max_diff={md['max_difference'][-1]}"
            if md.get("avg_difference"):
                out += f" avg_diff={md['avg_difference'][-1]}"
        return f"INCORRECT: {out} trials={md.get('correctness_trials', '?')}"
    if not getattr(res, "compiled", False):
        return "NOT COMPILED"
    if not getattr(res, "correctness", False):
        return "INCORRECT (no detail)"
    return "unknown failure"
