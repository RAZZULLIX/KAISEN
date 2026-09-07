# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""dart nudge backend — parse dart diagnostics and do what it suggests.

Real message formats:
  "file.dart:2:13: Error: Expected ';' after this."   (missing ;)
  "file.dart:2:15: Error: Can't find '}' to match '{'."   (unbalanced)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "Expected ';'" in stderr:
        fixes.append(("semicolon", ln))
    if "Can't find" in stderr or "Expected to find" in stderr or "Expected to find ')'" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    return fixes
