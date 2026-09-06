# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""javascript/typescript nudge backend — parse `node --check` stderr.

Observed node diagnostics:
  "prog.js:6" + "SyntaxError: Unexpected end of input"
  "prog.js:2" + "SyntaxError: Unexpected identifier 'console'"
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .engine import _parse_line_no, _scan_delim


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "Unexpected end of input" in stderr or "Unexpected identifier" in stderr:
        closers = _scan_delim(source)
        if closers:
            fixes.append(("delim", closers, ln))
    return fixes
