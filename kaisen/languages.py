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

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LANGUAGES: Dict[str, Dict[str, Any]] = {
    "c":          {"ext": ".c",     "fence": "c",          "kind": "compiled",    "toolchain": "gcc"},
    "cpp":        {"ext": ".cpp",   "fence": "cpp",        "kind": "compiled",    "toolchain": "g++"},
    "cuda":       {"ext": ".cu",    "fence": "cpp",        "kind": "compiled",    "toolchain": "nvcc"},
    "python":     {"ext": ".py",    "fence": "python",     "kind": "interpreted", "toolchain": ""},
    "java":       {"ext": ".java",  "fence": "java",       "kind": "compiled",    "toolchain": "javac"},
    "javascript": {"ext": ".js",    "fence": "javascript", "kind": "interpreted", "toolchain": ""},
    "typescript": {"ext": ".ts",    "fence": "typescript", "kind": "compiled",    "toolchain": "tsc"},
    "csharp":     {"ext": ".cs",    "fence": "csharp",     "kind": "compiled",    "toolchain": "csc (or dotnet build)"},
    "go":         {"ext": ".go",    "fence": "go",         "kind": "compiled",    "toolchain": "go build"},
    "rust":       {"ext": ".rs",    "fence": "rust",       "kind": "compiled",    "toolchain": "rustc"},
    "kotlin":     {"ext": ".kt",    "fence": "kotlin",     "kind": "compiled",    "toolchain": "kotlinc"},
    "swift":      {"ext": ".swift", "fence": "swift",      "kind": "compiled",    "toolchain": "swiftc"},
    "php":        {"ext": ".php",   "fence": "php",        "kind": "interpreted", "toolchain": ""},
    "ruby":       {"ext": ".rb",    "fence": "ruby",       "kind": "interpreted", "toolchain": ""},
    "r":          {"ext": ".r",     "fence": "r",          "kind": "interpreted", "toolchain": ""},
    "zig":        {"ext": ".zig",   "fence": "zig",        "kind": "compiled",    "toolchain": "zig build-exe"},
    "scala":      {"ext": ".scala", "fence": "scala",      "kind": "compiled",    "toolchain": "scalac"},
    "dart":       {"ext": ".dart",  "fence": "dart",       "kind": "compiled",    "toolchain": "dart compile exe"},
    "haskell":    {"ext": ".hs",    "fence": "haskell",    "kind": "compiled",    "toolchain": "ghc"},
    "lua":        {"ext": ".lua",   "fence": "lua",        "kind": "interpreted", "toolchain": ""},
    "perl":       {"ext": ".pl",    "fence": "perl",       "kind": "interpreted", "toolchain": ""},
    "shell":      {"ext": ".sh",    "fence": "bash",       "kind": "interpreted", "toolchain": ""},
    "d":          {"ext": ".d",     "fence": "d",          "kind": "compiled",    "toolchain": "dmd or ldc2"},
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
# Ordered acceptable binaries per compiled language (first hit on PATH wins).
# Mirrors what harness build scripts actually invoke, including common
# fallbacks (cc/clang for C) — preflight must match that reality.
TOOLCHAIN_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "c":          ("gcc", "cc", "clang"),
    "cpp":        ("g++", "c++", "clang++"),
    "cuda":       ("nvcc",),
    "java":       ("javac",),
    "typescript": ("tsc",),
    "csharp":     ("dotnet", "csc", "mcs"),
    "go":         ("go",),
    "rust":       ("rustc",),
    "kotlin":     ("kotlinc",),
    "swift":      ("swiftc",),
    "zig":        ("zig",),
    "scala":      ("scalac",),
    "dart":       ("dart",),
    "haskell":    ("ghc", "runghc"),
    "d":          ("dmd", "ldc2", "gdc"),
}


def toolchain_candidates(lang: Optional[str]) -> Tuple[str, ...]:
    """Acceptable toolchain binaries for a language.  Empty tuple =
    interpreted (nothing to check — the engine's own interpreter suffices)."""
    return TOOLCHAIN_CANDIDATES.get(normalize_lang(lang), ())


def find_toolchain(lang: Optional[str]) -> Optional[str]:
    """First toolchain binary for `lang` present on PATH (None = missing)."""
    for name in toolchain_candidates(lang):
        if shutil.which(name):
            return name
    return None


# Interpreters for the interpreted languages.  The framework treats these
# as "always available" (the engine's own runtime is the toolchain), but the
# OS-aware status check reports them so the operator can see what would
# actually RUN a candidate on this machine.
INTERPRETER_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "python": ("python3", "python"),
    "javascript": ("node",),
    "php": ("php",),
    "ruby": ("ruby",),
    "r": ("Rscript", "R"),
    "lua": ("lua", "lua5.4", "lua5.3", "luajit"),
    "perl": ("perl",),
    "shell": ("bash",),
}


def _runtime_candidates(lang: str) -> Tuple[str, ...]:
    """Binaries that must exist for a candidate to build/run on this host:
    compiled -> the compiler chain; interpreted -> the interpreter."""
    key = normalize_lang(lang)
    toolchain = TOOLCHAIN_CANDIDATES.get(key)
    if toolchain:
        return toolchain
    return INTERPRETER_CANDIDATES.get(key, ())


def _os_family(os_name: Optional[str] = None) -> str:
    """One of 'linux' | 'macos' | 'windows' | 'other' — the family the
    install hint and PATH probe should use.  os_name is injectable for
    tests; defaults to the running host."""
    import platform as _plat
    if os_name and os_name != os.name:
        # caller passed an explicit family string or 'posix'/'nt'
        if os_name in ("linux", "macos", "windows"):
            return os_name
        if os_name == "nt":
            return "windows"
    if _plat.system().lower() == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


# Extra per-OS install dirs to probe (in addition to PATH) — toolchains
# that land outside PATH (e.g. Apple's Swift in /opt, Homebrew's cellar,
# snap's /snap/bin already on PATH but harmless to include).  The status
# check looks here so an installed-but-not-on-PATH compiler is still seen.
_EXTRA_DIRS: Dict[str, Tuple[str, ...]] = {
    "linux": ("/opt/swift/usr/bin", "/usr/local/bin", "/usr/bin", "/snap/bin",
              "/usr/lib/dart/bin"),
    "macos": ("/opt/homebrew/bin", "/usr/local/bin", "/opt/swift/usr/bin"),
    "windows": (),
}


def _probe_binary(name: str, os_fam: str) -> Optional[str]:
    """Locate `name` on PATH, then in the OS's extra install dirs.

    Tolerates unreadable/inaccessible candidate paths (e.g. a toolchain
    tree that needs elevated perms, like Swift in /opt): a candidate that
    cannot be stat'd is skipped, never an exception."""
    hit = shutil.which(name)
    if hit:
        return hit
    for d in _EXTRA_DIRS.get(os_fam, ()):
        candidate = str(Path(d) / name)
        try:
            p = Path(candidate)
            if p.is_file() or p.is_symlink():
                return candidate
        except OSError:
            continue
    return None


# Per-OS install command.  '' = nothing to install (already present, or no
# package needed / must be built from source).
_INSTALL: Dict[str, Dict[str, str]] = {
    "linux": {
        "c": "sudo apt install gcc", "cpp": "sudo apt install g++",
        "cuda": "sudo apt install nvidia-cuda-toolkit",
        "java": "sudo apt install default-jdk",
        "typescript": "npm install -g typescript",
        "csharp": "sudo apt install mono-complete",
        "go": "sudo apt install golang-go",
        "rust": "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
        "kotlin": "sudo snap install kotlin --classic",
        "swift": "see swift.org (Ubuntu tarball)",
        "zig": "sudo snap install zig --beta",
        "scala": "sudo apt install scala",
        "dart": "sudo snap install dart --classic",
        "haskell": "sudo apt install ghc",
        "d": "sudo apt install ldc",
        "python": "sudo apt install python3",
        "javascript": "sudo apt install nodejs",
        "php": "sudo apt install php-cli",
        "ruby": "sudo apt install ruby",
        "r": "sudo apt install r-base",
        "lua": "sudo apt install lua5.4",
        "perl": "sudo apt install perl",
        "shell": "",
    },
    "macos": {
        "c": "brew install gcc", "cpp": "brew install g++",
        "cuda": "brew install --cask nvidia-cuda-toolkit",
        "java": "brew install openjdk",
        "typescript": "npm install -g typescript",
        "csharp": "brew install mono",
        "go": "brew install go",
        "rust": "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
        "kotlin": "brew install kotlin",
        "swift": "xcode-select --install",
        "zig": "brew install zig",
        "scala": "brew install scala",
        "dart": "brew install --cask dart",
        "haskell": "brew install ghc",
        "d": "brew install ldc",
        "python": "brew install python",
        "javascript": "brew install node",
        "php": "brew install php",
        "ruby": "brew install ruby",
        "r": "brew install r",
        "lua": "brew install lua",
        "perl": "brew install perl",
        "shell": "",
    },
    "windows": {
        "c": "winget install BrechtSanders.WinLibs.POSIX.UCRT64",
        "cpp": "winget install BrechtSanders.WinLibs.POSIX.UCRT64",
        "cuda": "winget install Nvidia.CUDA",
        "java": "winget install EclipseAdoptium.Temurin.21.JDK",
        "typescript": "npm install -g typescript",
        "csharp": "winget install Microsoft.DotNet.SDK.8",
        "go": "winget install GoLang.Go",
        "rust": "winget install Rustlang.Rustup",
        "kotlin": "winget install JetBrains.Kotlin",
        "swift": "see swift.org (Windows toolchain)",
        "zig": "winget install zig.zig",
        "scala": "winget install Scala.Scala",
        "dart": "winget install Dart.Dart",
        "haskell": "winget install Haskell.Stack",
        "d": "winget install Dlang.DMD",
        "python": "winget install Python.Python.3.12",
        "javascript": "winget install OpenJS.NodeJS",
        "php": "winget install PHP.PHP",
        "ruby": "winget install RubyInstallerTeam.Ruby",
        "r": "winget install RProject.R",
        "lua": "winget install Lua.Lua",
        "perl": "winget install StrawberryPerl.StrawberryPerl",
        "shell": "",
    },
}


def install_hint(lang: Optional[str], os_name: Optional[str] = None) -> str:
    """Recommended install command for `lang` on the current OS ('' = none)."""
    key = normalize_lang(lang)
    fam = _os_family(os_name)
    return str(_INSTALL.get(fam, {}).get(key, ""))


def toolchain_status(lang: Optional[str], os_name: Optional[str] = None) -> Dict[str, Any]:
    """Per-language toolchain availability on this OS.

    Returns a dict: {id, name, kind, installed, binary, candidates,
    toolchain, os, hint}.  OS-aware: the binary is probed on PATH then in
    the OS's extra install dirs, the probe list reflects this platform's
    candidates, and `hint` is the install command for the current OS
    family.  `binary` is the first candidate found, or None."""
    key = normalize_lang(lang)
    info = lang_info(key)
    kind = info.get("kind", "compiled")
    cands = _runtime_candidates(key)
    fam = _os_family(os_name)
    binary = next((b for c in cands if (b := _probe_binary(c, fam))), None)
    return {
        "id": key,
        "name": str(info.get("fence", key)),
        "kind": kind,
        "installed": binary is not None,
        "binary": binary,
        "candidates": list(cands),
        "toolchain": str(info.get("toolchain", "")),
        "os": fam,
        "hint": install_hint(key, os_name),
    }


def toolchain_status_all(os_name: Optional[str] = None,
                         langs: Optional[list] = None) -> list:
    """Status for every registered language (or a subset)."""
    ids = [normalize_lang(l) for l in (langs or LANGUAGES)]
    return [toolchain_status(i, os_name) for i in ids]


def artifact_basename(artifact_name: str, lang: Optional[str], os_name: str = os.name) -> str:
    """Real on-disk name of a built artifact.

    Windows compilers (MinGW gcc & co.) append .exe to extensionless -o
    targets, so compiled languages get the explicit suffix there — build,
    verify and score all receive the same {artifact} token and agree on one
    file.  Interpreted artifacts and names that already carry an extension
    are left as-is."""
    if os_name == "posix" or Path(artifact_name).suffix:
        return artifact_name
    if lang_info(lang).get("kind") == "compiled":
        return artifact_name + ".exe"
    return artifact_name


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


def toolchain_from_lang(lang: Optional[str]) -> str:
    """Primary compiler/toolchain for a language ('' = interpreted: the
    build step validates and copies instead of compiling)."""
    return str(lang_info(lang).get("toolchain", ""))


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

