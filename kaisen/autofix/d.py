# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""d nudge backend — parse d diagnostics and do what it suggests.

Real message formats:
  "file.d(6): Error: matching '}' expected following compound statement, not 'End of File'"  (unbalanced)
  "file.d(4): Error: semicolon needed to end declaration of 'x'"  (missing ;)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "semicolon needed" in stderr or "expected ';'" in stderr:
        fixes.append(("semicolon", ln))
    if "matching '}' expected" in stderr or "unmatched '{'" in stderr or "expected '}'" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    return fixes
