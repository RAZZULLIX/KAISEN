# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Language registry — the single source of truth for per-language facts.

The pipeline itself is language-agnostic (build/verify/score are
spec-defined commands).  What needs a registry:
  - candidate / champion file extensions on disk
  - markdown fence tags for LLM prompts + code extraction
  - the "kind" (compiled vs interpreted) for suggest prompting
  - the extension whitelist for user-attached program files
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

LANGUAGES: Dict[str, Dict[str, Any]] = {
    "c":          {"ext": ".c",     "fence": "c",          "kind": "compiled"},
    "cpp":        {"ext": ".cpp",   "fence": "cpp",        "kind": "compiled"},
    "cuda":       {"ext": ".cu",    "fence": "cpp",        "kind": "compiled"},
    "python":     {"ext": ".py",    "fence": "python",     "kind": "interpreted"},
    "java":       {"ext": ".java",  "fence": "java",       "kind": "compiled"},
    "javascript": {"ext": ".js",    "fence": "javascript", "kind": "interpreted"},
    "typescript": {"ext": ".ts",    "fence": "typescript", "kind": "compiled"},
    "csharp":     {"ext": ".cs",    "fence": "csharp",     "kind": "compiled"},
    "go":         {"ext": ".go",    "fence": "go",         "kind": "compiled"},
    "rust":       {"ext": ".rs",    "fence": "rust",       "kind": "compiled"},
    "kotlin":     {"ext": ".kt",    "fence": "kotlin",     "kind": "compiled"},
    "swift":      {"ext": ".swift", "fence": "swift",      "kind": "compiled"},
    "php":        {"ext": ".php",   "fence": "php",        "kind": "interpreted"},
    "ruby":       {"ext": ".rb",    "fence": "ruby",       "kind": "interpreted"},
    "r":          {"ext": ".r",     "fence": "r",          "kind": "interpreted"},
    "zig":        {"ext": ".zig",   "fence": "zig",        "kind": "compiled"},
    "scala":      {"ext": ".scala", "fence": "scala",      "kind": "compiled"},
    "dart":       {"ext": ".dart",  "fence": "dart",       "kind": "compiled"},
    "haskell":    {"ext": ".hs",    "fence": "haskell",    "kind": "compiled"},
    "lua":        {"ext": ".lua",   "fence": "lua",        "kind": "interpreted"},
    "perl":       {"ext": ".pl",    "fence": "perl",       "kind": "interpreted"},
    "shell":      {"ext": ".sh",    "fence": "bash",       "kind": "interpreted"},
    "d":          {"ext": ".d",     "fence": "d",          "kind": "compiled"},
}

# Friendly aliases -> canonical id.
ALIASES = {
    "c++": "cpp", "cxx": "cpp", "cc": "cpp", "cpp17": "cpp", "cpp20": "cpp",
    "py": "python", "python3": "python",
    "js": "javascript", "node": "javascript",
    "ts": "typescript",
    "cs": "csharp", "c#": "csharp",
    "rb": "ruby",
    "sh": "shell", "bash": "shell", "zsh": "shell",
    "cuda-c": "cuda", "cuda-c++": "cuda",
    "dlang": "d", "d2": "d",
}

# Compiler-message fixer family: gcc/nvcc "did you forget / did you mean".
C_COMPILER_FAMILY = {"c", "cpp", "cc", "cxx", "cuda"}


def normalize_lang(lang: Optional[str]) -> str:
    """Canonical language id; unknown values map to a lowercased id."""
    if not lang:
        return "c"
    key = str(lang).strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    if key in LANGUAGES:
        return key
    return key


def lang_info(lang: Optional[str]) -> Dict[str, Any]:
    key = normalize_lang(lang)
    return LANGUAGES.get(key) or {"ext": f".{key}", "fence": key, "kind": "compiled"}


def ext_from_lang(lang: Optional[str]) -> str:
    return str(lang_info(lang)["ext"])


def fence_from_lang(lang: Optional[str]) -> str:
    return str(lang_info(lang)["fence"])


def lang_from_ext(ext: Optional[str]) -> Optional[str]:
    """Detect a language id from a filename or extension ('py', '.py',
    'foo.py', or a full path all work)."""
    if not ext:
        return None
    suffix = Path(str(ext).lower()).suffix
    if suffix:
        e = suffix
    else:
        e = str(ext).lower()
        if not e.startswith("."):
            e = "." + e
    for key, info in LANGUAGES.items():
        if info["ext"] == e:
            return key
    extra = {".h": "c", ".hpp": "cpp", ".cuh": "cuda", ".m": "c", ".txt": None, ".md": None}
    return extra.get(e)

def code_exts() -> set:
    """Every extension the framework accepts as an attached program file."""
    return {info["ext"] for info in LANGUAGES.values()} | {".h", ".hpp", ".cuh", ".txt", ".md"}


def c_compiler_like(lang: Optional[str]) -> bool:
    return normalize_lang(lang) in C_COMPILER_FAMILY


def lang_from_goal(goal: str) -> Optional[str]:
    """Detect a language mentioned in a plain-words goal
    ('in c++', 'a rust program', 'cuda kernel', 'node script'...)."""
    import re as _re
    if not goal:
        return None
    # Punctuation becomes spaces so "in D," and "a C program!" still match.
    g = " " + _re.sub(r"[^\w\s+]", " ", goal.lower()) + " "
    # Ordered: longer/more specific phrases first.
    for phrase, lang in (
        ("cuda", "cuda"), ("c++", "cpp"), ("c++17", "cpp"), ("c++20", "cpp"),
        ("typescript", "typescript"), ("javascript", "javascript"), ("java", "java"),
        ("python", "python"), ("golang", "go"), (" go ", "go"), ("rust", "rust"),
        ("c#", "csharp"), ("csharp", "csharp"), ("kotlin", "kotlin"), ("swift", "swift"),
        ("php", "php"), ("ruby", "ruby"), ("zig", "zig"), ("scala", "scala"),
        ("dart", "dart"), ("haskell", "haskell"), ("lua", "lua"), ("perl", "perl"),
        ("shell script", "shell"), ("bash", "shell"), (" in c ", "c"), ("c program", "c"),
        ("dlang", "d"), ("d language", "d"), ("d program", "d"), (" in d ", "d"),
    ):
        if phrase in g:
            return lang
    return None

