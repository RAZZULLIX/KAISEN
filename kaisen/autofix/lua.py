# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""lua nudge backend — parse lua syntax errors and do what it suggests.

Observed lua diagnostics:
  "prog.lua:3: 'then' expected near 'print'"   (missing then)
  "prog.lua:5: 'end' expected near <eof>"      (missing end)
"""
from __future__ import annotations
import re
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    lines = source.split("\n")
    if "'then' expected" in stderr:
        for idx, l in enumerate(lines):
            if re.match(r"\s*(if|elseif)\b", l) and "then" not in l:
                lines[idx] = l.rstrip() + (" then" if not l.rstrip().endswith("then") else "")
                fixes.append(("raw", "\n".join(lines)))
                break
    if "'do' expected" in stderr:
        for idx, l in enumerate(lines):
            if re.match(r"\s*(for|while)\b", l) and not re.search(r"\bdo\b", l):
                lines[idx] = l.rstrip() + " do"
                fixes.append(("raw", "\n".join(lines)))
                break
    if "'end' expected" in stderr or "end' expected near <eof>" in stderr:
        # Count block openers (function/if/for/while/do) vs `end`; append missing.
        openers = len(re.findall(r"\b(function|if|for|while|do)\b", source)) - \
                  len(re.findall(r"\bend\b", source))
        if openers > 0:
            fixes.append(("raw", source + ("\nend" * openers)))
    return fixes
