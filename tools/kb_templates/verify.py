#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KernelBench correctness gate: candidate vs the reference model.

Runs KernelBench's own evaluator with performance measurement OFF, on
N randomized input draws (fp32, atol=rtol from KernelBench).  Exit 0 only
when every trial matches.

Usage: verify.py <candidate>
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import kb_common as kb  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: verify.py <candidate>", file=sys.stderr)
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
            num_correct_trials=kb.NUM_CORRECT_TRIALS,
            num_perf_trials=1,
            measure_performance=False,
            check_for_excessive_speedup=False,
            verbose=False,
            build_dir=str(kb.BUILD_DIR),
        )

    if res is None:
        print("KB VERIFY FAIL: " + kb.failure_detail(res), file=sys.stderr)
        return 1
    if not res.compiled or not res.correctness:
        print("KB VERIFY FAIL: " + kb.failure_detail(res), file=sys.stderr)
        return 1
    print("OK correct")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
