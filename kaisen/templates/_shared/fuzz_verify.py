#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Correctness fuzz gate — the anti-"fast but wrong" verify step.

Replays EVERY seeded case in the project's `fuzz_cases.json` (inputs +
reference outputs, generated deterministically by kaisen/fuzzlib at factory
time) against the candidate artifact.  First mismatch fails the generation
with a machine-readable diagnostic for the bug-capture loop:

    FUZZ MISMATCH case=42 tag=rand:7 input=[123456] expected='...' got='...'
    FUZZ CRASH    case=7  tag=edge:3  input=[0] exit=139 stderr='...'
    FUZZ TIMEOUT  case=12 tag=rand:1  input=[999983]

Usage: fuzz_verify.py <artifact> [cases.json]
(cases.json defaults to ./fuzz_cases.json; the pipeline runs this with
cwd = project dir.)

Note: the compare modes below mirror kaisen/fuzzlib.compare_outputs — keep
the two in sync if one changes.
"""
import json
import os
import subprocess
import sys


def compare_outputs(got, expected, mode="exact"):
    if mode == "exact":
        return got.strip() == expected.strip()
    if mode == "sorted_lines":
        g = sorted(l for l in got.splitlines() if l.strip())
        e = sorted(l for l in expected.splitlines() if l.strip())
        return g == e
    if mode == "float_last":
        g, e = _last_number(got), _last_number(expected)
        if g is None or e is None:
            return False
        # relative tolerance, floored at absolute 1e-6 (covers e == 0 too)
        return abs(g - e) / max(1.0, abs(e)) <= 1e-6
    raise ValueError(f"unknown compare mode {mode!r}")


def _last_number(s):
    toks = s.split()
    if not toks:
        return None
    tok = toks[-1]
    if "=" in tok:
        tok = tok.rsplit("=", 1)[1]
    try:
        return float(tok)
    except ValueError:
        return None

def main():
    if len(sys.argv) < 2:
        print("Usage: fuzz_verify.py <artifact> [cases.json]", file=sys.stderr)
        sys.exit(1)
    artifact = os.path.abspath(sys.argv[1])
    cases_path = sys.argv[2] if len(sys.argv) > 2 else "fuzz_cases.json"
    try:
        with open(cases_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"FUZZ SETUP error reading {cases_path}: {e}", file=sys.stderr)
        sys.exit(1)

    compare = doc.get("compare", "exact")
    per_case_timeout = float(doc.get("case_timeout", 5.0))
    cases = doc.get("cases") or []
    if not cases:
        print("FUZZ SETUP no cases in " + cases_path, file=sys.stderr)
        sys.exit(1)

    for i, c in enumerate(cases):
        argv = [str(a) for a in c.get("argv", [])]
        tag = c.get("tag", "?")
        try:
            r = subprocess.run([artifact, *argv], capture_output=True,
                               timeout=per_case_timeout)
        except subprocess.TimeoutExpired:
            print(f"FUZZ TIMEOUT case={i} tag={tag} input={argv}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            # artifact not executable (missing shebang, lost exec bit) —
            # report it machine-readably; the LLM can fix a hint like this
            print(f"FUZZ EXEC ERROR case={i} tag={tag} input={argv}: {e}",
                  file=sys.stderr)
            sys.exit(1)
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")[:200]
            print(f"FUZZ CRASH case={i} tag={tag} input={argv} "
                  f"exit={r.returncode} stderr={err!r}", file=sys.stderr)
            sys.exit(1)
        got = r.stdout.decode(errors="replace")
        if not compare_outputs(got, c.get("expected", ""), compare):
            print(f"FUZZ MISMATCH case={i} tag={tag} input={argv} "
                  f"expected={c.get('expected', '')[:200]!r} got={got[:200]!r}",
                  file=sys.stderr)
            sys.exit(1)
    print(f"OK {len(cases)} cases")


if __name__ == "__main__":
    main()
