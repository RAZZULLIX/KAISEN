# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""d nudge backend — parse d diagnostics and do what it suggests.

Real message formats:
  "file.d(6): Error: matching '}' expected following compound statement, not 'End of File'"  (unbalanced)
  "file.d(4): Error: semicolon needed to end declaration of 'x'"  (missing ;)
  "file.d(4): Error: lower case integer suffix 'l' is not allowed. Please use 'L' instead"
  "file.d(4): Error: undefined identifier `uint64_t`"
  "file.d(3): Error: identifier expected following `package`"
"""
from __future__ import annotations
import re
from typing import List, Optional, Tuple
from .engine import _parse_line_no, _scan_delim

# C-style types the LLM writes when it doesn't know D's aliases -> D names.
_D_TYPE_FIX = {
    "uint64_t": "ulong",
    "uint64": "ulong",
    "int64_t": "long",
    "int64": "long",
    "uint32_t": "uint",
    "uint32": "uint",
    "int32_t": "int",
    "int32": "int",
    "uint16_t": "ushort",
    "int16_t": "short",
    "uint8_t": "ubyte",
    "int8_t": "byte",
    "size_t": "size_t",  # D has size_t; safe no-op via std
}


def _fix_d_suffixes(source: str) -> str:
    """Lower-case integer suffix 'l' -> 'L' (D is case-sensitive).  D wants
    `uL` (lowercase u, uppercase L) — keep the u as-is, only uppercase the l."""
    # matches a lowercase l/uL as an integer literal suffix, not an identifier
    # e.g. `1000000007uL` (ok), `1000000007ul` (bad, -> uL), `0x1000l` (bad, -> 0x1000L)
    return re.sub(r"(?<=\d)([uU]?)l\b", lambda m: m.group(1).lower() + "L", source)


def _fix_d_types(source: str) -> str:
    for bad, good in _D_TYPE_FIX.items():
        source = re.sub(rf"\b{re.escape(bad)}\b", good, source)
    return source


def parse(stderr: str, source: str) -> List[Tuple[str, ...]]:
    fixes: List[Tuple[str, ...]] = []
    ln = _parse_line_no(stderr)
    if "semicolon needed" in stderr or "expected ';'" in stderr:
        fixes.append(("semicolon", ln))
    if "matching '}' expected" in stderr or "unmatched '{'" in stderr or "expected '}'" in stderr:
        c = _scan_delim(source)
        if c:
            fixes.append(("delim", c, ln))
    if "lower case integer suffix" in stderr or "case integer suffix" in stderr:
        fixed = _fix_d_suffixes(source)
        if fixed != source:
            fixes.append(("raw", fixed))
    if "undefined identifier" in stderr:
        fixed = _fix_d_types(source)
        if fixed != source:
            fixes.append(("raw", fixed))
    return fixes
