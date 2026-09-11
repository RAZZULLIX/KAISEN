#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Family pack SCORE: measured speedup of the pack over the graded set.

Prints one line per graded problem and the aggregate the engine optimizes:

    speedup=<mean over the GRADED SET>   (missing/wrong kernels count 0)
    fast_p=<fraction correct AND >1.0x>
    valid=<correct>/<graded>

`score.py` grades the project's slice (fast, one representative per family
axis).  `score_full.py` grades the WHOLE family held in the harness — the
graded set costs no prompt context, so the audit can be as large as the
family allows.

Usage: score.py <candidate> [--full]
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fam_common as kb  # noqa: E402


def run(which: str) -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 1:
        print("usage: score.py <candidate> [--full]", file=sys.stderr)
        return 2
    src = kb.read_candidate(args[0])
    kernel = "score"
    kb.pin_gpu()
    kb_eval = kb.load_kb_eval()

    problems = kb.problems_for(which)
    per: list[tuple] = []
    with kb.gpu_lock():
        kb.warm_gpu()
        for p in problems:
            res = kb.eval_problem(
                p, kb_eval, src, measure=True,
                correct_trials=1,
                perf_trials=int(p.get("perf_trials") or 30),
            )
            good = bool(res is not None and res.compiled and res.correctness
                        and (res.runtime or 0) > 0 and (res.ref_runtime or 0) > 0)
            sp = (res.ref_runtime / res.runtime) if good else 0.0
            flag = ""
            if good and (res.metadata or {}).get("excessive_speedup"):
                sp = 0.0
                flag = " EXCESSIVE-SPEEDUP(flagged as reward hack)"
            per.append((p, sp, good, flag))
            if good:
                print(f"  [{p['index']:>3}] {p['name']:<44} "
                      f"time_us={res.runtime:.3f} ref_us={res.ref_runtime:.3f} "
                      f"speedup={sp:.4f}{flag}")
            else:
                print(f"  [{p['index']:>3}] {p['name']:<44} "
                      f"INVALID — {kb.failure_detail(res)}")

    n = len(per) or 1
    valid = sum(1 for _, _, g, _ in per if g)
    mean_sp = sum(s for _, s, _, _ in per) / n
    fast_p = sum(1 for _, s, g, _ in per if g and s > 1.0) / n
    print(f"valid={valid}/{len(per)}")
    print(f"fast_p={fast_p:.4f}")
    print(f"speedup={mean_sp:.6f}")

    if valid == 0:
        print(f"{kernel.upper()} FAIL: nothing in the {which} set scored",
              file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return run("full" if "--full" in sys.argv else "slice")


if __name__ == "__main__":
    raise SystemExit(main())
