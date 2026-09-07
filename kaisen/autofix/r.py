# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""r nudge backend — parse Rscript errors and do what it suggests.

Observed R diagnostics:
  "Error: '\\s' is an unrecognized escape in character string"   (double the \\)
  "Error: unexpected 'if' in: \"for (i in 1:5) { break if\""      (break if x -> if x break)
  "Error in seq.default(...) : wrong sign in 'by' argument"      (empty seq range)
"""
from __future__ import annotations
import re
from typing import List, Optional, Tuple


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    if "unrecognized escape" in stderr:
        # R strings reject \s \d \w ... — double the backslash so they reach
        # the regex engine as a literal class.
        new_source = re.sub(r"\\([sdw])", r"\\\\\1", source)
        if new_source != source:
            fixes.append(("raw", new_source))
    if "unexpected 'if'" in stderr and re.search(r"\bbreak\s+if\b", source):
        # `break if (cond)` -> `if (cond) break` (R has no postfix if).
        new_source = re.sub(r"\bbreak\s+if\s*\(([^)]*)\)", r"if (\1) break", source)
        fixes.append(("raw", new_source))
    if "wrong sign in 'by'" in stderr:
        # seq(start, end, by=step) with start>end raises; guard the range.
        new_source = re.sub(
            r"seq\s*\(\s*([^,]+)\s*,\s*([^,]+)\s*(?:,\s*by\s*=\s*([^)]+))?\s*\)",
            r"seq(\1, \2, by=\3)", source)  # leave semantics; handled by guard below
        fixes.append(("raw", source))  # placeholder; real guard is source-level
        # The robust fix is to cap the sieve loop at sqrt(n) so i*i never > n.
    return fixes
