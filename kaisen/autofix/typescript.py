# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""typescript nudge backend — parse typescript diagnostics and do what it suggests.

Real message formats:
  "file.ts:2: ... error: Expected ')'"   (unbalanced)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "Expected ')'" in stderr or "Expected '}'" in stderr or "Unexpected end of input" in stderr or "cannot find name" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    return fixes
