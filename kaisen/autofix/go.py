# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""go nudge backend — parse `go build` stderr and do what it suggests.

Observed go diagnostics:
  "main.go:8:1: syntax error: unexpected }, expected expression"
  "main.go:9:1: syntax error: unexpected EOF, expected }"
  "main.go:4:2: undefined: Println"          (only packages are importable)
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .engine import _parse_line_no, _scan_delim

_GO_STDLIB = {
    "fmt": "fmt", "os": "os", "strconv": "strconv", "strings": "strings",
    "math": "math", "sort": "sort", "time": "time", "errors": "errors",
    "io": "io", "io/ioutil": "io/ioutil", "bufio": "bufio", "bytes": "bytes",
    "sync": "sync", "regexp": "regexp", "unicode": "unicode",
    "container/list": "container/list", "path/filepath": "path/filepath",
    "log": "log", "encoding/json": "encoding/json", "net/http": "net/http",
}


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "unexpected EOF" in stderr or \
       re.search(r"syntax error: unexpected [^,]+, expected", stderr) or \
       re.search(r"expected \}", stderr):
        fixes.append(("delim", _scan_delim(source), ln))
    m = re.search(r"undefined: (\w+)", stderr)
    if m and m.group(1) in _GO_STDLIB:
        fixes.append(("import", m.group(1), "go"))
    return fixes
