#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KernelBench score: measured speedup of the candidate vs the reference.

One call to KernelBench's evaluator with performance ON (CUDA-event timing,
N trials, same seed for every candidate) measures BOTH the kernel and the
reference model on the pinned GPU, so the metric is self-consistent.

Metric printed on stdout:  speedup=<ref_time / kernel_time>  (higher better)
A candidate whose measured speedup exceeds the excessive-speedup threshold
suspected of reward hacking (cached results, skipped work, stream tricks)
FAILS the generation — a fast-but-wrong kernel can never become champion.

Usage: score.py <candidate>
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import kb_common as kb  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: score.py <candidate>", file=sys.stderr)
        return 2
    cand = Path(sys.argv[1])
    kb.pin_gpu()
    kb_eval = kb.load_kb_eval()
    ref_src = kb.read_reference()
    cand_src = kb.read_candidate(str(cand))

    with kb.gpu_lock():
        kb.warm_gpu()
        res = kb_eval.eval_kernel_against_ref(
            ref_src,
            cand_src,
            num_correct_trials=1,
            num_perf_trials=kb.NUM_PERF_TRIALS,
            measure_performance=True,
            timing_method="cuda_event",
            check_for_excessive_speedup=True,
            excessive_speedup_threshold=kb.EXCESSIVE_SPEEDUP,
            verbose=False,
            build_dir=str(kb.BUILD_DIR),
        )

    if res is None or not res.compiled or not res.correctness:
        print("KB SCORE FAIL: " + kb.failure_detail(res), file=sys.stderr)
        return 1

    runtime = float(res.runtime or -1.0)
    ref_runtime = float(res.ref_runtime or -1.0)
    if runtime <= 0 or ref_runtime <= 0:
        print(
            f"KB SCORE FAIL: no timing (kernel={runtime} us, ref={ref_runtime} us)",
            file=sys.stderr,
        )
        return 1

    speedup = ref_runtime / runtime
    print(f"time_us={runtime:.4f}")
    print(f"ref_time_us={ref_runtime:.4f}")

    if (res.metadata or {}).get("excessive_speedup"):
        print(
            f"EXCESSIVE SPEEDUP {speedup:.2f}x (>{kb.EXCESSIVE_SPEEDUP:.0f}x) — "
            "suspected reward hack; inspect runs/ artifacts before trusting it",
            file=sys.stderr,
        )
        print(f"speedup={speedup:.6f}")
        return 1

    print(f"speedup={speedup:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
