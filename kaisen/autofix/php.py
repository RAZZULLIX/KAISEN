# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""php nudge backend — parse php lint/runtime errors and do what it suggests.

Observed php diagnostics:
  "PHP Fatal error:  Uncaught Error: Undefined constant \"i\" in prog.php:12"
  (a bare identifier used where a variable was intended -> add the leading $)
"""
from __future__ import annotations
import re
from typing import List, Optional, Tuple
from .engine import _parse_line_no


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    m = re.search(r"Undefined constant [\"'](\w+)[\"']", stderr)
    if m:
        name = m.group(1)
        # Prefix bare occurrences of the name with '$' (not ones already '$').
        new_source = re.sub(rf"(?<!\$)\b{re.escape(name)}\b", "$" + name, source)
        if new_source != source:
            fixes.append(("raw", new_source))
    return fixes
