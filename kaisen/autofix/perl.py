# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""perl nudge backend — parse `perl -c` stderr and do what it suggests.

Observed perl diagnostics:
  "syntax error at /path/prog.pl line 1, near "2;""
  "/path/prog.pl had compilation errors."
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "syntax error" in stderr or "unterminated" in stderr:
        closers = _scan_delim(source)
        if closers:
            fixes.append(("delim", closers, ln))
    return fixes
