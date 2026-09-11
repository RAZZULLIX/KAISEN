#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Family pack FULL-FAMILY audit: grade the pack against EVERY problem the
harness knows (not just the search slice).

Not wired into the generation pipeline — run it by hand (or from KAI SCORE)
to measure what the current champion can actually do family-wide:

    python3 harness/score_full.py <candidate-or-best/program.py>

Usage: score_full.py <candidate>
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fam_score  # noqa: E402


if __name__ == "__main__":
    if "--full" not in sys.argv:
        sys.argv.append("--full")
    raise SystemExit(fam_score.run("full"))
