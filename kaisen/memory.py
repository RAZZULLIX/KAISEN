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

# Stopwords for the keyword counter: English function words + generic
# code tokens.  Filtering these out is what makes the line read as
# "technique frequency" instead of prose word count.  Hardcoded by design —
# the counter must never cost an LLM call.
_STOPWORDS = {
    "the", "and", "for", "int", "void", "return", "char", "float", "double",
    "long", "short", "unsigned", "signed", "const", "static", "struct", "if",
    "else", "while", "do", "switch", "case", "break", "continue", "include",
    "define", "endif", "ifdef", "ifndef", "size_t", "uint", "bool", "true",
    "false", "null", "this", "that", "with", "from", "was", "are", "but",
    "not", "have", "has", "had", "all", "can", "will", "would", "should",
    "could", "then", "than", "there", "their", "they", "his", "her", "him",
    "she", "you", "your", "its", "out", "into", "over", "under", "about",
    "also", "very", "just", "get", "got", "put", "set", "one", "two", "three",
    "first", "second", "third", "new", "old", "use", "used", "using", "way",
    "thing", "things", "much", "many", "more", "most", "some", "any", "each",
    "every", "only", "same", "different", "other", "another", "such", "no",
    "yes", "end", "file", "data", "code", "test", "tests", "make", "made",
    "run", "runs", "running", "time", "times", "line", "lines", "value",
    "values", "result", "results", "add", "added", "change", "changed",
    "changes", "try", "tries", "patch", "bit", "bits", "byte", "bytes",
    "loop", "loops", "function", "functions", "compiler", "compile",
    "compiled", "using", "inside", "call", "calls", "called", "check",
    "checking", "better", "best", "worse", "worst", "fast", "faster",
    "slow", "slower", "big", "small", "large", "larger", "smaller",
    "much", "little", "less", "least", "great", "good", "bad", "well",
    "here", "there", "where", "what", "which", "who", "whom", "whose",
    "how", "why", "when", "while", "was", "were", "been", "being", "am",
    "is", "are", "be", "been", "did", "does", "doing", "done", "own",
    "mine", "ours", "theirs", "hers", "self", "off", "on", "in", "at",
    "by", "to", "of", "as", "or", "nor", "so", "yet", "up", "down",
}



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
        """Count occurrences of technique-signal tokens across lessons and
        recent memos — lets the model see how often a technique came up
        without reading everything.  A user-supplied keywords.txt (if
        present) names the exact tokens to track; otherwise a hardcoded
        stopword filter removes prose noise.  No LLM calls — deterministic
        and cheap."""
        counter: Counter = Counter()
        # User-supplied technique names (keywords.txt) act as an explicit
        # allowlist when present — the reader is cheap and deterministic.
        allowed = {w.lower() for w in self.load_keywords() if w.strip()}
        for path in (self.lessons_path,):
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
            if allowed:
                counter.update(w for w in words if w.lower() in allowed)
            else:
                counter.update(w for w in words if w.lower() not in _STOPWORDS)
        if self.memos_dir.exists():
            for p in list(self.memos_dir.iterdir())[-10:]:
                try:
                    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", p.read_text(encoding="utf-8"))
                    if allowed:
                        counter.update(w for w in words if w.lower() in allowed)
                    else:
                        counter.update(w for w in words if w.lower() not in _STOPWORDS)
                except Exception:
                    pass
        return dict(counter.most_common(limit))
