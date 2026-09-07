# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""haskell nudge backend — parse haskell diagnostics and do what it suggests.

Real message formats:
  "file.hs:4:1: error: parse error (possibly incorrect indentation or mismatched brackets)"
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "mismatched brackets" in stderr or "parse error (possibly" in stderr or "parse error" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    return fixes
