# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""csharp nudge backend — parse csharp diagnostics and do what it suggests.

Real message formats:
  "file.cs(6,246): error CS1525: Unexpected symbol 'end-of-file'"  (unbalanced)
  "file.cs(4,4): error CS1525: Unexpected symbol 'System'"        (missing ;)
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "CS1002" in stderr or "; expected" in stderr:
        fixes.append(("semicolon", ln))
    if "Unexpected symbol" in stderr and ("end-of-file" in stderr or "}" in stderr or "EOF" in stderr.upper()):
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    return fixes
