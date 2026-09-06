# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Deterministic fuzz-case generation for correctness verification.

The campaign's biggest silent risk is a candidate that passes the fixed
verify cases but is wrong elsewhere — "fast but wrong" winning the
championship on a lucky slice.  Every generated project therefore ships with
a SEEDED set of edge + random inputs and reference outputs (computed at
factory time from the naive baseline).  The per-project verify step
(`harness/fuzz_verify.py`, see templates/_shared) replays every case against
the candidate artifact and fails on the first mismatch with a
machine-readable diagnostic:

    FUZZ MISMATCH case=42 tag=rand:7 input=[123456] expected=... got=...

Determinism contract: same (family, seed, n, domain) -> identical cases,
forever.  The bug-capture loop re-runs a failing case exactly as it happened.

Input model: argv-only.  Pipeline steps run with cwd=project dir and no
stdin channel, so every case is a list of argument strings — the same shape
the harness substitutes into `{candidate}`/`{artifact}` commands.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------- #
# families
# --------------------------------------------------------------------------- #

#: family name -> how many argv entries a case carries (excluding program)
FAMILY_ARITY = {
    "int": 1,        # one integer argument, domain [lo, hi]
    "pair_int": 2,   # two integer arguments, domains [alo, ahi], [blo, bhi]
    "str": 1,        # one string argument (text)
    "pair_str": 2,   # two string arguments (text)
    "triple_int": 3, # three integer arguments, domains [alo..], [blo..], [mlo, mhi]
    "intlist": 1,    # ONE argument: space-separated integers in [-10**6, 10**6]
}

_INT_RE = re.compile(r"^-?\d+$")


def _int_edges(lo: int, hi: int) -> List[int]:
    """Boundary values for an integer domain: the places off-by-one and
    overflow bugs live.  Order is stable (dedup keeps first occurrence)."""
    out: List[int] = []

    def add(v: int) -> None:
        if lo <= v <= hi and v not in out:
            out.append(v)

    for v in (lo, lo + 1, hi - 1, hi, -1, 0, 1, 2):
        add(v)
    # small naturals from the low end
    start = max(lo, 0)
    for v in range(start, min(hi, start + 8) + 1):
        add(v)
    # powers of two and their neighbours (bit/size bugs)
    p = 1
    while p <= hi:
        add(p - 1)
        add(p)
        add(p + 1)
        p *= 2
    # a few values hugging the top end
    for v in (hi - 2, hi - 3):
        add(v)
    return out


def _str_edges(ascii_only: bool = False) -> List[str]:
    """Boundary strings: empty, tiny, repeated, palindromes, unicode, long.
    ascii_only=True drops the non-ASCII edge (for byte-oriented programs)."""
    edges = [
        "",
        "a",
        "aa",
        "ab",
        "abc",
        "aba",          # palindrome
        "abba",         # even-length palindrome
        "a b c",        # spaces
        "x" * 500,      # long homogeneous
    ]
    if not ascii_only:
        edges.insert(7, "héllo wörld")  # non-ASCII (charmap/UTF-8 bugs)
    return edges

def _str_pair_edges() -> List[List[str]]:
    """Boundary string pairs: empty-vs-empty, empty-vs-nonempty both ways,
    equal, one-char-diff, reversals, homogeneous runs."""
    return [
        ["", ""],
        ["a", ""],
        ["", "a"],
        ["a", "b"],
        ["ab", "ba"],
        ["abc", "abc"],
        ["aba", "abba"],
        ["x" * 50, "x" * 50 + "y"],
    ]


def _triple_int_edges(alo: int, ahi: int, blo: int, bhi: int,
                     mlo: int, mhi: int) -> List[List[int]]:
    """Corners for (a, b, m)-style problems: zero/one bases, zero exponents,
    modulus 1 and 2 (the degenerate cases that break naive code)."""
    ea = [v for v in (alo, alo + 1, ahi - 1, ahi, 0, 1, 2) if alo <= v <= ahi]
    eb = [v for v in (blo, blo + 1, bhi - 1, bhi, 0, 1, 2, 3) if blo <= v <= bhi]
    em = [v for v in (mlo, mlo + 1, mhi - 1, mhi, 1, 2) if mlo <= v <= mhi]
    out: List[List[int]] = []
    seen = set()
    for a in ea:
        for b in eb[:3]:
            for m in em[:3]:
                t = (a, b, m)
                if t not in seen:
                    seen.add(t)
                    out.append(list(t))
    return out


def _intlist_edges() -> List[str]:
    """Boundary lists: single values (incl. negative/zero), all-negative,
    all-equal, alternating signs, long list."""
    return [
        "0",
        "1",
        "-5",
        "-1 -2 -3",
        "1 2 3 4",
        "0 0 0",
        "7 -7 7 -7",
        "5 -3 5 -3 5",
        " ".join(str(i) for i in range(1, 61)),
    ]


def gen_cases(
    family: str,
    seed: int,
    n: int,
    lo: int = 0,
    hi: int = 10 ** 6,
    alo: Optional[int] = None,
    ahi: Optional[int] = None,
    blo: Optional[int] = None,
    bhi: Optional[int] = None,
    mlo: Optional[int] = None,
    mhi: Optional[int] = None,
    ascii_only: bool = False,
) -> List[Dict[str, Any]]:
    """Generate `n` deterministic cases for a family.

    The first ~40% are edge values (stable order), the rest seeded random.
    `tag` records provenance so a mismatch tells you WHICH kind of input
    broke ("edge:pow2-17" vs "rand:3")."""
    if family not in FAMILY_ARITY:
        raise ValueError(f"unknown fuzz family {family!r} (have {sorted(FAMILY_ARITY)})")
    if n < 1:
        raise ValueError("n must be >= 1")
    rng = random.Random(seed)

    cases: List[Dict[str, Any]] = []

    def push(argv: List[str], tag: str) -> None:
        cases.append({"argv": argv, "tag": tag})

    if family == "int":
        edges = _int_edges(lo, hi)
        for i, v in enumerate(edges):
            push([str(v)], f"edge:{i}")
        seen = {c["argv"][0] for c in cases}
        i = 0
        while len(cases) < n:
            v = rng.randint(lo, hi)
            if str(v) in seen:
                continue
            seen.add(str(v))
            push([str(v)], f"rand:{i}")
            i += 1
    elif family == "pair_int":
        alo_, ahi_ = (alo if alo is not None else lo, ahi if ahi is not None else hi)
        blo_, bhi_ = (blo if blo is not None else lo, bhi if bhi is not None else hi)
        ea, eb = _int_edges(alo_, ahi_), _int_edges(blo_, bhi_)
        # cross the interesting corners: (edgeA x first edgeB) then (first edgeA x edgeB)
        for i, a in enumerate(ea):
            push([str(a), str(eb[0])], f"edge:a{i}")
        for j, b in enumerate(eb):
            push([str(ea[0]), str(b)], f"edge:b{j}")
        seen = {tuple(c["argv"]) for c in cases}
        i = 0
        while len(cases) < n:
            a, b = rng.randint(alo_, ahi_), rng.randint(blo_, bhi_)
            key = (str(a), str(b))
            if key in seen:
                continue
            seen.add(key)
            push(list(key), f"rand:{i}")
            i += 1
    elif family == "pair_str":
        for i, (a, b) in enumerate(_str_pair_edges()):
            push([a, b], f"edge:{i}")
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        seen = {tuple(c["argv"]) for c in cases}
        i = 0
        while len(cases) < n:
            la, lb = rng.choice([0, 1, 2, 3, 5, 8, 13, 21, 40]), \
                rng.choice([0, 1, 2, 3, 5, 8, 13, 21, 40])
            a = "".join(rng.choice(alphabet) for _ in range(la))
            b = "".join(rng.choice(alphabet) for _ in range(lb))
            key = (a, b)
            if key in seen:
                continue
            seen.add(key)
            push(list(key), f"rand:{i}")
            i += 1
    elif family == "triple_int":
        alo_, ahi_ = (alo if alo is not None else lo, ahi if ahi is not None else hi)
        blo_, bhi_ = (blo if blo is not None else lo, bhi if bhi is not None else hi)
        mlo_, mhi_ = (mlo if mlo is not None else lo, mhi if mhi is not None else hi)
        for i, t in enumerate(_triple_int_edges(alo_, ahi_, blo_, bhi_, mlo_, mhi_)):
            push([str(v) for v in t], f"edge:{i}")
        seen = {tuple(c["argv"]) for c in cases}
        i = 0
        while len(cases) < n:
            t = (rng.randint(alo_, ahi_), rng.randint(blo_, bhi_),
                 rng.randint(mlo_, mhi_))
            key = tuple(str(v) for v in t)
            if key in seen:
                continue
            seen.add(key)
            push(list(key), f"rand:{i}")
            i += 1
    elif family == "intlist":
        for i, s in enumerate(_intlist_edges()):
            push([s], f"edge:{i}")
        seen = {c["argv"][0] for c in cases}
        i = 0
        while len(cases) < n:
            k = rng.choice([1, 2, 3, 5, 8, 13, 21, 40, 80, 120])
            s = " ".join(str(rng.randint(-10 ** 6, 10 ** 6)) for _ in range(k))
            if s in seen:
                continue
            seen.add(s)
            push([s], f"rand:{i}")
            i += 1
    else:  # str
        edges = _str_edges(ascii_only)
        for i, s in enumerate(edges):
            push([s], f"edge:{i}")
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        seen = {c["argv"][0] for c in cases}
        i = 0
        while len(cases) < n:
            length = rng.choice([1, 2, 3, 5, 8, 13, 21, 40, 100])
            s = "".join(rng.choice(alphabet) for _ in range(length))
            if s in seen:
                continue
            seen.add(s)
            push([s], f"rand:{i}")
            i += 1

    return cases[:n]


# --------------------------------------------------------------------------- #
# output comparison
# --------------------------------------------------------------------------- #

def compare_outputs(got: str, expected: str, mode: str = "exact") -> bool:
    """Compare a candidate's stdout against the reference expectation.

    Modes:
      exact         — stripped strings equal (the default; use it unless the
                      problem statement allows output order freedom);
      sorted_lines  — multiset of non-empty lines equal (order-free output);
      float_last    — last whitespace token parsed as float, relative
                      tolerance 1e-6 (measured-value outputs)."""
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


def _last_number(s: str) -> Optional[float]:
    """Last whitespace token as float; `key=value` tokens take the value
    part (score outputs are `time_ms=3.8`, not bare numbers)."""
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


# --------------------------------------------------------------------------- #
# reference outputs (factory time) + cases file I/O
# --------------------------------------------------------------------------- #

def compute_expected(
    artifact: str | Path,
    cases: List[Dict[str, Any]],
    timeout: float = 30.0,
) -> List[Dict[str, Any]]:
    """Run the REFERENCE artifact on every case and attach `expected`
    (stdout).  Called once at project-creation time — the reference is the
    naive baseline, trusted by construction.  Raises on any reference
    failure: a fuzz set with missing expectations must never ship."""
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(cases):
        r = subprocess.run(
            [str(artifact), *c["argv"]],
            capture_output=True, timeout=timeout, cwd=Path(artifact).parent,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"reference artifact failed on case {i} ({c['tag']} "
                f"argv={c['argv']}): exit {r.returncode}: "
                f"{r.stderr.decode(errors='replace')[:200]!r}")
        c = dict(c)
        c["expected"] = r.stdout.decode(errors="replace")
        out.append(c)
    return out


def save_cases(path: str | Path, family: str, seed: int, n: int,
               compare: str, cases: List[Dict[str, Any]], **domain: Any) -> None:
    """Write the project's fuzz_cases.json (inputs + reference outputs)."""
    doc = {
        "family": family,
        "seed": seed,
        "n": n,
        "compare": compare,
        "domain": domain,
        "cases": cases,
    }
    Path(path).write_text(json.dumps(doc, indent=1), encoding="utf-8")


def load_cases(path: str | Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
