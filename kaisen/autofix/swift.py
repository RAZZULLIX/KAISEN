# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""swift nudge backend — parse swift diagnostics and do what it suggests.

Real message formats:
  "file.swift:3:9: note: to match this opening '{'"   (unbalanced)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "to match this opening" in stderr or "unexpected ')'" in stderr or "expected '}'" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    return fixes
