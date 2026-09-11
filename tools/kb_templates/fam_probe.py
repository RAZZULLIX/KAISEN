#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Single-problem capacity/correctness probe — run in a FRESH process by the
factory while generating a family project.

Establishes, per problem: can this machine evaluate it at all (memory), and
does the naive baseline pass its own reference?  Problems that cannot be
evaluated here are marked `fit: false` and excluded from the graded set
with the reason recorded — silently grading a kernel the machine cannot run
would poison every speedup figure in the family.

Usage: probe.py <problem-index> <candidate> [correct-trials]
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fam_common as kb  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: probe.py <index> <candidate>", file=sys.stderr)
        return 2
    index, cand = int(sys.argv[1]), sys.argv[2]
    trials = int(sys.argv[3]) if len(sys.argv) > 3 else None
    problem = [p for p in kb.PROBLEMS if p["index"] == index]
    if not problem:
        print(f"no problem {index}", file=sys.stderr)
        return 2

    p = problem[0]
    src = kb.read_candidate(cand)
    kb.pin_gpu()
    kb_eval = kb.load_kb_eval()

    try:
        res = kb.eval_problem(
            p, kb_eval, src, measure=False,
            correct_trials=int(trials or p.get("correct_trials") or 3),
            perf_trials=1)
    except Exception as e:  # pragma: no cover — KB swallows its own errors
        print(f"CRASH {type(e).__name__}: {e}"[:300])
        return 1

    detail = kb.failure_detail(res)
    if res is not None and res.compiled and res.correctness:
        print("OK")
        return 0
    kind = "OOM" if "out of memory" in detail.lower() else "FAIL"
    print(f"{kind} {detail}"[:300])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
