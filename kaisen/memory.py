# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Project memory: history/failure/lesson/memo blob for prompts, plus
keyword counters (the 'how many times did X fail' trick from the notes)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from .projects import Project
from .util import load_json, save_json

LESSONS_FILE = "lessons.txt"
KEYWORDS_FILE = "keywords.txt"
MEMOS_DIR = "memos"

FAILURE_KEYWORDS = ("fail", "error", "timeout", "rejected", "skip", "crash",
                    "violat", "no_metrics", "no_code", "cancelled")


class ProjectMemory:
    def __init__(self, project: Project, state: Any = None):
        self.project = project
        self.state = state  # ProjectState (optional)

    # -- lessons -----------------------------------------------------------
    @property
    def lessons_path(self) -> Path:
        return self.project.path / LESSONS_FILE

    def load_lesson(self) -> str:
        try:
            return self.lessons_path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def save_lesson(self, text: str) -> None:
        self.lessons_path.write_text(text, encoding="utf-8")

    def load_keywords(self) -> List[str]:
        try:
            p = self.project.path / KEYWORDS_FILE
            return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:
            return []

    # -- memos -------------------------------------------------------------
    @property
    def memos_dir(self) -> Path:
        return self.project.path / MEMOS_DIR

    def save_memo(self, generation: int, text: str) -> Path:
        self.memos_dir.mkdir(parents=True, exist_ok=True)
        p = self.memos_dir / f"gen_{generation:06d}.md"
        p.write_text(text, encoding="utf-8")
        return p

    def load_memo(self, generation: int) -> str:
        p = self.memos_dir / f"gen_{generation:06d}.md"
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    # -- history blob ------------------------------------------------------
    def build_history_blob(self, max_entries: int = 8) -> str:
        if self.state is None:
            return "(none yet)"
        recent = self.state.history[-max_entries:]
        if not recent:
            return "(none yet)"
        lines = []
        failures = []
        for h in recent:
            line = f"iter {h.get('generation')}: {h.get('outcome')}"
            if h.get("detail"):
                line += f" | {str(h['detail'])[:500]}"
            lines.append(line)
            outcome = str(h.get("outcome", "")).lower()
            if any(kw in outcome for kw in FAILURE_KEYWORDS):
                failures.append(f"[FAILURE FEEDBACK] Gen {h.get('generation')}: {h.get('outcome')} | {str(h.get('detail'))[:800]}")
        blob = "\n".join(lines)
        if failures:
            blob = "--- EXPLICIT FAILURE FEEDBACK ---\n" + "\n".join(failures) + "\n-----------------------------\n\n" + blob
        return blob

    # -- keyword counters --------------------------------------------------
    def keyword_counts(self, limit: int = 20) -> Dict[str, int]:
        """Count occurrences of keywords across memory artifacts — lets the
        model see how often a technique failed without reading everything."""
        counter: Counter = Counter()
        for path in (self.lessons_path,):
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
            counter.update(words)
        if self.memos_dir.exists():
            for p in list(self.memos_dir.iterdir())[-10:]:
                try:
                    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", p.read_text(encoding="utf-8"))
                    counter.update(words)
                except Exception:
                    pass
        return dict(counter.most_common(limit))
