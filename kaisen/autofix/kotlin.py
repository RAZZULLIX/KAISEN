# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""kotlin nudge backend — parse kotlin diagnostics and do what it suggests.

Real message formats:
  "file.kt:4:2: error: syntax error: Expecting '}'."   (unbalanced)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "Expecting '}'" in stderr or "Expecting ';'" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
        else:
            fixes.append(("semicolon", ln))
    return fixes
