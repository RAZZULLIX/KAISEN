# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""python nudge backend — parse `ast` SyntaxError and do what it says.

Observed ast diagnostics:
  "line 3: expected ':'"
  "line 5: expected an indented block"
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

_PY_HEADER = ("def ", "class ", "if ", "elif ", "else", "for ", "while ",
              "with ", "try", "except", "finally", "async def ", "async for ", "async with ")


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    m = re.search(r"line (\d+): (.*)", stderr)
    if not m:
        return fixes
    ln = int(m.group(1)); msg = m.group(2)
    if "expected ':'" in stderr or "expected ':'" in msg:
        lines = source.split("\n")
        idx = max(0, min(ln - 1, len(lines) - 1))
        l = lines[idx]
        if any(l.strip().startswith(h.rstrip()) for h in _PY_HEADER) and not l.rstrip().endswith(":"):
            lines[idx] = l + ":"
            fixes.append(("raw", "\n".join(lines)))
    if "expected an indented block" in stderr:
        lines = source.split("\n")
        idx = max(0, min(ln - 1, len(lines) - 1))
        lines.insert(idx + 1, "    pass")
        fixes.append(("raw", "\n".join(lines)))
    return fixes
