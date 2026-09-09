"""Per-model usage budgets — a safety valve so a frontier model cannot
silently burn your allowance.

A customer may have (say) 1M free tokens every 3 hours.  Budget lets them
declare, per LLM server:

    "budget": {
      "max_tokens": "1M",        // or 1000000 / "1,000,000" / "2.5M"
      "max_generations": 50,     // LLM calls (a hard safety limit; 0 = off)
      "reset": "3h"              // 30s | 5m | 12h | 3d | 1w | 12:00:00 (12h)
    }

When the server exceeds max_tokens OR max_generations inside the current
reset window, it is automatically dropped from routing (its budget bucket
reads "exhausted") until the window rolls over.  This is per-server, always
optional, and counts REAL tokens observed on the wire (streaming included),
not estimates.

Parsing is deliberately forgiving so a human can type the number however it
comes to hand:
  * tokens:  1000000  1M  1,000,000  2.5M  500K  1m  1e6
  * reset:   30s  5m  12h  3d  1w  12:00:00  (HH:MM:SS / HH:MM durations)
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

# --------------------------------------------------------------------------- #
# parsers
# --------------------------------------------------------------------------- #

def parse_tokens(value: Any) -> Optional[int]:
    """Human token count -> int, or None for unset/blank.

    Accepts: "1000000", "1M", "1,000,000", "2.5M", "500K", "1m", 1_000_000,
    1e6.  The suffix is case-insensitive; comma/underscore separators are
    stripped.  Returns None for empty/None (meaning "no limit")."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    s = str(value).strip().replace(",", "").replace("_", "")
    if not s:
        return None
    low = s.lower()
    mult = 1
    if low.endswith("k"):
        mult, low = 1_000, low[:-1]
    elif low.endswith("m"):
        mult, low = 1_000_000, low[:-1]
    elif low.endswith("b"):
        mult, low = 1_000_000_000, low[:-1]
    if low.endswith("tokens") or low.endswith("token"):
        low = low[: -len("tokens") if low.endswith("tokens") else -len("token")].strip()
    try:
        num = float(low)
    except ValueError:
        return None
    n = int(round(num * mult))
    return n if n > 0 else None


def parse_duration(value: Any) -> Optional[float]:
    """Human duration -> seconds, or None for unset/blank.

    Accepts:
      * bare numbers      : 90  (= 90 s)
      * unit suffixes     : 30s  5m  12h  3d  1w  (case-insensitive)
      * clock-style       : 12:00:00 (= 12 h)  01:30:00 (= 90 min)
                            12:00    (= 12 h)  00:30    (= 30 min)
    Returns None for empty/None (meaning "never resets — one-shot limit")."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value).strip().lower()
    if not s:
        return None
    unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    # clock-style HH:MM[:SS]
    if ":" in s:
        parts = s.split(":")
        if 2 <= len(parts) <= 3:
            try:
                h = int(parts[0])
                m = int(parts[1])
                sec = int(parts[2]) if len(parts) == 3 else 0
            except ValueError:
                return None
            if m >= 60 or sec >= 60:
                return None
            total = h * 3600 + m * 60 + sec
            return float(total) if total > 0 else None
        return None
    # number + unit
    m = s[-1]
    num_str = s[:-1].strip()
    if m in unit_map and num_str:
        try:
            num = float(num_str)
        except ValueError:
            return None
        total = num * unit_map[m]
        return float(total) if total > 0 else None
    # bare number -> seconds
    try:
        num = float(s)
    except ValueError:
        return None
    return float(num) if num > 0 else None


# --------------------------------------------------------------------------- #
# the bucket — per-server usage within one reset window
# --------------------------------------------------------------------------- #

class Budget:
    """Tracks a server's token/generation usage inside a rolling window
    and answers "can this server take another call right now?".

    State is re-derived from config each time (the limits live in the
    server spec) plus a small persisted usage file that survives restarts,
    so a reset doesn't have to be re-armed after a daemon restart."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self._max_tokens = parse_tokens((cfg or {}).get("max_tokens"))
        self._max_generations = parse_tokens((cfg or {}).get("max_generations"))
        self._window = parse_duration((cfg or {}).get("reset"))
        # live usage for the CURRENT window
        self._tokens = 0
        self._generations = 0
        self._window_start = time.time()

    # -- config ----------------------------------------------------------
    @classmethod
    def from_spec(cls, spec: Optional[Dict[str, Any]]) -> "Budget":
        """Build from a server's budget config (or a no-limit budget when
        the field is absent)."""
        return cls((spec or {}).get("budget") if isinstance(spec, dict) else None)

    @property
    def configured(self) -> bool:
        return self._max_tokens is not None or self._max_generations is not None

    @property
    def config(self) -> Dict[str, Any]:
        """The config dict as given (for persistence round-trip)."""
        out: Dict[str, Any] = {}
        if self._max_tokens is not None:
            out["max_tokens"] = self._max_tokens
        if self._max_generations is not None:
            out["max_generations"] = self._max_generations
        if self._window is not None:
            out["reset"] = self._window
        return out

    # -- window / limits -------------------------------------------------
    def _roll_if_needed(self) -> None:
        if self._window is not None and (time.time() - self._window_start) >= self._window:
            self._tokens = 0
            self._generations = 0
            self._window_start = time.time()

    def exhausted(self) -> bool:
        """True if any limit is hit inside the current window."""
        if not self.configured:
            return False
        self._roll_if_needed()
        if self._max_tokens is not None and self._tokens >= self._max_tokens:
            return True
        if self._max_generations is not None and self._generations >= self._max_generations:
            return True
        return False

    def record(self, tokens: int = 0, generations: int = 0) -> None:
        self._roll_if_needed()
        self._tokens += max(0, int(tokens))
        self._generations += max(0, int(generations))

    # -- status ----------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        self._roll_if_needed()
        return {
            "configured": self.configured,
            "exhausted": self.exhausted(),
            "tokens_used": self._tokens,
            "max_tokens": self._max_tokens,
            "generations_used": self._generations,
            "max_generations": self._max_generations,
            "window_s": self._window,
            "window_reset_in_s": round(
                max(0.0, (self._window_start + self._window) - time.time()), 1)
                if self._window is not None else None,
            "window_start": self._window_start,
        }

    def snapshot(self) -> Dict[str, Any]:
        return self.status()


def budget_status(servers: Dict[str, "Budget"]) -> Dict[str, Any]:
    """Per-server budget snapshot for the GUI/API."""
    return {sid: b.status() for sid, b in servers.items()}
