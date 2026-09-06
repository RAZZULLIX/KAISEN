# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""rustc nudge backend — parse rs stderr and do what it suggests.

Observed rustc diagnostics:
  "error: expected `;`, found `println` ... help: add `;` here"
  "error: this file contains an unclosed delimiter"
  "error[E0433]: cannot find module or crate `fmt` in this scope"
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .engine import _parse_line_no, _scan_delim

_RUST_STDLIB = {
    "fmt", "io", "collections", "ops", "cmp", "path", "net", "sync",
    "thread", "string", "vec", "str", "boxed", "hash", "mem", "iter",
    "ptr", "convert", "default", "marker",
}


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if re.search(r"expected [`'\"];\s*[`'\"]", stderr) or \
       re.search(r"help:?\s*add\s+[`'\"];", stderr) or \
       ("expected `;`" in stderr or "add `;` here" in stderr):
        fixes.append(("semicolon", ln))
    if "unclosed delimiter" in stderr:
        fixes.append(("delim", _scan_delim(source), ln))
    m = re.search(r"cannot find module or crate [`'\"](\w+)[`'\"]", stderr)
    if m and m.group(1) in _RUST_STDLIB:
        fixes.append(("import", m.group(1), "std"))
    return fixes
