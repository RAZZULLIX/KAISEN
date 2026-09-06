# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""shell nudge backend — parse `bash -n` stderr and do what it suggests.

Observed bash diagnostics:
  "prog.sh: line 2: unexpected EOF while looking for matching `)'"
  "prog.sh: line 5: syntax error near unexpected token `fi'"
  "prog.sh: line 5: `fi'"
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "unexpected EOF" in stderr or "looking for matching" in stderr:
        closers = _scan_delim(source)
        if closers:
            fixes.append(("delim", closers, ln))
    if re.search(r"unexpected token [`'\"]fi", stderr):
        lines = source.split("\n")
        for idx, l in enumerate(lines):
            if re.match(r"\s*if\b", l) and "then" not in l:
                if ";" in l:
                    # inline: `if cond; echo; fi` -> `if cond; then echo; fi`
                    lines[idx] = l.replace(";", "; then", 1)
                else:
                    # multiline: `if cond` alone on the line -> append ; then
                    lines[idx] = l + "; then"
                fixes.append(("raw", "\n".join(lines)))
                break
    if re.search(r"unexpected token [`'\"]done", stderr):
        lines = source.split("\n")
        for idx, l in enumerate(lines):
            if re.match(r"\s*(for|while)\b", l) and " do" not in l:
                if ";" in l:
                    lines[idx] = l.replace(";", "; do", 1)
                else:
                    lines[idx] = l + "; do"
                fixes.append(("raw", "\n".join(lines)))
                break
    return fixes
