# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""zig nudge backend — parse zig diagnostics and do what it suggests.

Real message formats:
  "file.zig:6:1: error: expected statement, found 'EOF'"   (unbalanced)
  "file.zig:3:21: error: expected ';' after statement"    (missing ;)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "expected ';'" in stderr or "expected ';' after" in stderr:
        fixes.append(("semicolon", ln))
    if "expected statement, found 'EOF'" in stderr or "expected '}'" in stderr or "unexpected end of file" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    return fixes
