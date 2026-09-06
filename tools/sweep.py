#!/usr/bin/env python3
"""Factory self-check sweep, incremental.

Verifies every (algorithm, language) factory project end-to-end (build the
baseline, run its full fuzz gate, score it) and streams each result to
<out>.jsonl as it completes, so failures can be triaged live while the sweep
is still running. A row is {"id", "ok", "error"}; "ok" false rows carry a
diagnostic. NO_TOOLCHAIN rows are recorded but not retried.

Usage: python3 tools/sweep.py [--n-cases 200] [--out /tmp/sweep.jsonl]
"""
import argparse, json, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from kaisen import factory as F


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-cases", type=int, default=200)
    ap.add_argument("--out", default="/tmp/sweep.jsonl")
    ap.add_argument("--only", default=None, help="comma list of (algo,lang) or algo, to narrow")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.unlink(missing_ok=True)
    keys = F.list_algorithms()
    langs = F.list_languages()
    if args.only:
        parts = [p.strip() for p in args.only.split(",") if p.strip()]
        if len(parts) == 2 and "-" not in parts[0]:
            keys = [parts[0]]; langs = [parts[1]]
        else:
            keys = [p for p in parts if p in F.list_algorithms()]
            langs = [p for p in parts if p in F.list_languages()] or langs

    total = len(keys) * len(langs)
    n_ok = n_fail = n_skip = 0
    t0 = time.time()
    print(f"[sweep] {total} projects, n_cases={args.n_cases}", flush=True)
    with open(out, "a", encoding="utf-8") as f:
        for key in keys:
            for lang in langs:
                pid = f"{key}-{lang}"
                if not F.toolchain_available(lang):
                    row = {"id": pid, "ok": False, "error": F.NO_TOOLCHAIN}
                    n_skip += 1
                else:
                    try:
                        proj = F.make_project(key, lang, n_cases=args.n_cases)
                        errs = F.check_project(proj)
                        if errs:
                            row = {"id": pid, "ok": False, "error": "; ".join(errs)}
                            n_fail += 1
                        else:
                            row = {"id": pid, "ok": True, "error": ""}
                            n_ok += 1
                    except Exception as e:  # core bug / registration crash
                        row = {"id": pid, "ok": False, "error": f"EXC: {e}"}
                        n_fail += 1
                f.write(json.dumps(row) + "\n")
                f.flush()
                done = n_ok + n_fail + n_skip
                if done % 5 == 0 or done == total:
                    el = time.time() - t0
                    print(f"[sweep] {done}/{total} ok={n_ok} fail={n_fail} skip={n_skip} "
                          f"({el:.0f}s, {el/max(done,1):.1f}s/proj)", flush=True)
    print(f"[sweep] DONE ok={n_ok} fail={n_fail} skip={n_skip} total={total} "
          f"elapsed={time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
