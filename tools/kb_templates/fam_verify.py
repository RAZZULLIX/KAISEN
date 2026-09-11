#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Family pack VERIFY gate: every kernel in the graded slice must match its
reference model (KernelBench's own evaluator, randomized trials, fp32 tol).

Exit 0 only when the whole graded set is correct.

Usage: verify.py <candidate>
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fam_common as kb  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: verify.py <candidate>", file=sys.stderr)
        return 2
    src = kb.read_candidate(sys.argv[1])
    gen = kb.current_generation(sys.argv[1])
    kb.pin_gpu()
    kb_eval = kb.load_kb_eval()

    problems = kb.verify_problems(gen)
    print(f"verify window (gen {gen}): "
          + ", ".join(f"[{q['index']}]" for q in problems))
    bad: list[str] = []
    with kb.gpu_lock():
        kb.warm_gpu()
        for p in problems:
            res = kb.eval_problem(
                p, kb_eval, src, measure=False,
                correct_trials=int(p.get("correct_trials") or 3),
                perf_trials=1,
            )
            ok = bool(res is not None and res.compiled and res.correctness)
            print(f"{'ok  ' if ok else 'FAIL'} [{p['index']:>3}] {p['name']}"
                  + ("" if ok else " — " + kb.failure_detail(res)))
            if not ok:
                bad.append(f"[{p['index']}] {p['name']}: {kb.failure_detail(res)}")

    if bad and kb.STRICT:
        print(f"VERIFY FAIL: {len(bad)}/{len(problems)} kernels wrong",
              file=sys.stderr)
        for line in bad:
            print("  " + line, file=sys.stderr)
        return 1
    if len(bad) == len(problems):
        print("VERIFY FAIL: no kernel in the pack is correct", file=sys.stderr)
        return 1
    print(f"OK correct ({len(problems) - len(bad)}/{len(problems)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
