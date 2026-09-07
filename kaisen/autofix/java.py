# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""java nudge backend — parse java diagnostics and do what it suggests.

Real message formats:
  "file.java:3: error: ';' expected"                  (missing ;)
  "file.java:6: error: reached end of file while parsing"  (unbalanced)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "';' expected" in stderr:
        fixes.append(("semicolon", ln))
    if "reached end of file" in stderr or "')' expected" in stderr or "illegal start of expression" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    return fixes
