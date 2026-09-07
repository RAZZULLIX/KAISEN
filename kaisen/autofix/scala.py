# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""scala nudge backend — parse scala diagnostics and do what it suggests.

Real message formats:
  "file.scala:5: error: Missing closing brace '}' assumed here"  (unbalanced)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "Missing closing brace" in stderr or "';' expected" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
        elif "';' expected" in stderr:
            fixes.append(("semicolon", ln))
    return fixes
