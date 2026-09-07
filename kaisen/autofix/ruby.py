# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""ruby nudge backend — parse ruby errors and do what it suggests.

Observed ruby diagnostics:
  "prog.rb:2:in `exit': no implicit conversion from nil to integer (TypeError)"
  (exit(puts(0)) — puts returns nil; exit needs an integer -> call expr, exit 0)
  "prog.rb:5: syntax error, unexpected end-of-input, expecting 'end'"
"""
from __future__ import annotations
import re
from typing import List, Optional, Tuple
from .engine import _parse_line_no


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    if "no implicit conversion from nil to integer" in stderr:
        # `exit(puts(X))` -> `(puts(X); exit 0)` — guard (the trailing
        # `if ...` / `unless ...` stays, so the print only happens when the
        # original exit did).
        m = re.search(r'exit\s*\(\s*(puts\s*\([^)]*\))\s*\)', source)
        if m:
            new_source = source[:m.start()] + f"({m.group(1)}; exit 0)" + source[m.end():]
            fixes.append(("raw", new_source))
    if (re.search(r"expecting ['`]?end['`]", stderr) or "expecting 'end'" in stderr or
            "unexpected end-of-input" in stderr or "unexpected $end" in stderr or
            "unexpected end" in stderr):
        # append the missing end(s)
        fixes.append(("raw", source.rstrip("\n") + "\nend\n"))
    return fixes
