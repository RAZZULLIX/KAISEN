# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Unified skills registry.

Every capability the framework offers, merged from both historical
harnesses (compression project and sorting project) into ONE set, usable by
any project through its spec:

  analyze   — C/any-language code extraction, header injection, dangerous
              pattern scan, semantic normalization + hash (dedup)
  integrity — file equality / similarity check
  prompts   — modular template loading, selection by stagnation, variable
              substitution, generation-prompt assembly
  memory    — history/failure/lesson/memo blob + keyword counters
  deepwork  — agentic multi-turn analysis loop (read programs, query the
              results store, read lessons/memos, produce a memo)
  results   — per-project results store (CSV) with pandas query support
"""

from __future__ import annotations

import hashlib
import os
import re
import random
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .util import load_json, save_json

# ===========================================================================
# ANALYZE — code extraction / safety / semantic dedup
# ===========================================================================

C_STANDARD_HEADERS = [
    "#include <stdint.h>", "#include <stdlib.h>", "#include <string.h>",
    "#include <stdio.h>", "#include <stdbool.h>", "#include <math.h>",
    "#include <immintrin.h>", "#include <assert.h>", "#include <time.h>",
    "#include <limits.h>",
]

# Candidate-code danger patterns PER LANGUAGE.  A candidate program must be
# pure: no process spawning, no filesystem deletion, no network.  These are
# deny-by-default; a project can relax them via guardrails.allow_extra /
# spec-level guardrail configuration.
C_FAMILY_DANGER = [
    "remove(", "unlink(", "rmdir(", "DeleteFile", 'system("rm', 'system("del',
    "fork(", "vfork(", "execv(", "execve(", "execvp(", "execvpe(", "execl(",
    "execlp(", "execle(", "fexecve(", "posix_spawn", "posix_spawnp",
    "popen(", "system(", "mkfifo(", "socket(", "socketpair(", "connect(",
    "listen(", "accept(", "shm_open", "dlopen(", "dlsym(", "ptrace(",
    "syscall(", "clone3(", "pidfd_open",
]
PYTHON_DANGER = [
    "os.remove", "os.unlink", "os.rmdir", "shutil.rmtree", "pathlib.Path.unlink",
    "os.system", "subprocess", "os.popen", "socket", "urllib", "requests",
    "http.client", "ftplib", "aiohttp", "eval(", "exec(", "__import__", "pickle.load",
    "ctypes", "os.startfile", "os.spawn", "os.posix_spawn", "pty.spawn",
]
SHELL_DANGER = [
    "rm -rf", "rm -r ", "rm -fr", "curl ", "wget ", "nc ", "ncat ", "socat ",
    "shred ", "mkfs", "dd ", "chmod -R", "chown ", "sudo ", "ssh ", "scp ",
    "| sh", "| bash", "| zsh", "> /etc", "> ~/", "> /dev/", "killall ", "pkill ",
]
JS_DANGER = [
    "require('fs')", 'require("fs")', "require('child_process')", 'require("child_process")',
    "execsync", "spawn(", "exec(", "fs.rmsync", "fs.unlinksync", "rmsync(", "unlinksync(",
    "fetch(", "http.request", "https.request", "net.connect", "xmlhttprequest",
]
JAVA_DANGER = [
    "runtime.getruntime", "processbuilder", "files.delete", "new socket", "urlconnection",
    "httpclient", "url(",
]
GO_DANGER = [
    "os.removeall", "os.remove(", "os.rename", "exec.command", "net.dial", "net.listen",
    "os.create", "os.openfile", "http.get", "http.post",
]
RUST_DANGER = [
    "std::process::command", "std::fs::remove", "std::fs::rename", "tcpstream",
    "udpsocket", "std::net::", "reqwest",
]
D_DANGER = [
    "std.process", "std.socket", "std.net", "std.file.remove", "std.file.rename",
    "std.file.delTree", "std.file.rmdir", "std.stdio.remove", "system(",
    "spawnShell", "spawnProcess", "execv", "execve", "popen", "mkfifo",
]
GENERIC_DANGER = ["system(", "socket", "rmtree", "deletefile", "httpclient", "net.http"]
DANGER_BY_LANG = {
    "c": C_FAMILY_DANGER, "cpp": C_FAMILY_DANGER, "cuda": C_FAMILY_DANGER,
    "python": PYTHON_DANGER, "shell": SHELL_DANGER,
    "javascript": JS_DANGER, "typescript": JS_DANGER,
    "java": JAVA_DANGER, "go": GO_DANGER, "rust": RUST_DANGER,
    "d": D_DANGER,
}


_STARTERS = {
    "c": (r"#\s*include\b", r"\bint\s+main\s*\(", r"\bvoid\s+\w+\s*\("),
    "cpp": (r"#\s*include\b", r"\bint\s+main\s*\("),
    "cuda": (r"#\s*include\b", r"__global__\b", r"\bint\s+main\s*\("),
    "python": (r"^(?:import |from )", r"^def \w", r"^class \w", r"^if __name__"),
    "java": (r"\bpublic\s+class\b", r"\bclass\s+\w+\s*\{"),
    "go": (r"^package\s+\w+", r"^func\s+main\b"),
    "rust": (r"^fn\s+main\b", r"^use\s+\w"),
    "javascript": (r"^function\s+\w", r"^(?:const|let)\s+\w", r"^(?:import|export)\s"),
    "typescript": (r"^function\s+\w", r"^(?:const|let)\s+\w", r"^interface\s+\w", r"^(?:import|export)\s"),
    "shell": (r"^#!/(?:usr/)?bin/(?:ba)?sh", r"^#!/usr/bin/env\s+"),
    "ruby": (r"^def\s+\w", r"^require\s+['\"]"),
    "php": (r"^<\?php"),
    "csharp": (r"^using\s+System", r"\bclass\s+\w+"),
    "kotlin": (r"^fun\s+\w", r"^import\s+\w"),
    "swift": (r"^import\s+\w+", r"^func\s+\w"),
    "r": (r"^\w+\s*<-\s*function", r"^library\("),
    "lua": (r"^function\s+\w", r"^local\s+\w"),
    "perl": (r"^#!/usr/bin/perl", r"^use\s+strict"),
    "haskell": (r"^module\s+\w+", r"^main\s*::"),
    "zig": (r"^pub\s+fn\s+\w+", r"^const\s+\w+\s*="),
    "scala": (r"^object\s+\w+", r"^import\s+\w"),
    "dart": (r"^void\s+main\b", r"^import\s+['\"]"),
    "d": (r"^module\s+\w+", r"^void\s+main\b", r"^int\s+main\b", r"^unittest\b"),
}


def _starters(language: str):
    from .languages import normalize_lang
    return _STARTERS.get(normalize_lang(language), ())


def extract_code(text: str, language: str = "c") -> Optional[str]:
    """Extract the largest code block from LLM output, language-aware.

    Priority: content after the last <|marker|> that contains language
    markers, then fenced blocks (preferring the language's fence tag),
    then a bare scan from the language's starter patterns."""
    from .languages import fence_from_lang, normalize_lang
    if not text:
        return None
    lang = normalize_lang(language)
    fence = fence_from_lang(lang)
    starters = _starters(lang)
    # 1. Marker-based (both projects used <|...|> chain-of-thought fences)
    markers = list(re.finditer(r"<\|[^|]+\|>", text))
    if markers:
        scope = text[markers[-1].end():]
        starts = []
        for pat in starters:
            m = re.search(pat, scope, re.M)
            if m:
                starts.append(m.start())
        if starts:
            return scope[min(starts):].strip().replace("`", "")
    # 2. Fenced blocks — any tag, but the language's own tag wins.
    blocks = re.findall(r"```([\w.+-]*)\s*\n?(.*?)```", text, re.DOTALL)
    if blocks:
        tagged = [(tag.strip().lower(), b.strip()) for tag, b in blocks if b.strip()]
        pref = [b for t, b in tagged if t == fence or t == lang]
        if pref:
            return max(pref, key=len)
        if starters:
            hits = [b for _t, b in tagged if any(re.search(p, b, re.M) for p in starters)]
            if hits:
                return max(hits, key=len)
        if tagged:
            return max((b for _t, b in tagged), key=len)
    # 3. Bare scan on the language's starters.
    for pat in starters:
        m = re.search(pat, text, re.M)
        if m:
            return text[m.start():].strip().replace("`", "")
    # 4. Last resort: the raw text, backticks stripped.
    return text.strip().replace("`", "")


def ensure_headers(code: str, language: str = "c") -> str:
    if language != "c":
        return code
    missing = [h for h in C_STANDARD_HEADERS if h not in code]
    return ("\n".join(missing) + "\n\n" + code) if missing else code


def extract_function_names(code: str, language: str = "c") -> set:
    """Best-effort list of top-level function names defined in `code`, used
    by the edit-scope guard.  Heuristic (regex, not a parser) and
    language-aware: C-family and D use brace-body declarations; Python uses
    `def`/`async def`; others fall back to any `name(` followed by a body.
    Not exhaustive — a determined obfuscator defeats it, but it catches the
    common whole-file rewrite where unrelated functions are touched."""
    from .languages import normalize_lang
    lang = normalize_lang(language)
    names: set = set()
    if lang in ("c", "cpp", "cuda", "d"):
        for m in re.finditer(
            r"\b(?:static\s+|inline\s+|extern\s+)*"
            r"(?:[\w:*&<>\s]+?)\s+(\w+)\s*\([^;{}]*?\)\s*\{",
            code,
        ):
            names.add(m.group(1))
    elif lang == "python":
        for m in re.finditer(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", code, re.M):
            names.add(m.group(1))
    else:
        for m in re.finditer(r"\b\w+\s+(\w+)\s*\([^;{}]*?\)\s*\{", code):
            names.add(m.group(1))
    return names


def find_dangerous(code: str, language: str = "c") -> Optional[str]:
    """Return the first dangerous API call found, or None.

    Layer 1: the per-language pattern table (substring scan).  Layer 2:
    write-mode file open detection — `fopen(path, "w"/"a"/"+")` or raw
    `open(path, O_WRONLY|O_CREAT|...)` opens a write channel to an
    arbitrary path (the substring scan alone can't see the mode).  The
    checks are a tripwire, not containment: process/time/RSS limits are
    the real fence."""
    from .languages import normalize_lang
    lang = normalize_lang(language)
    patterns = DANGER_BY_LANG.get(lang, GENERIC_DANGER)
    low = code.lower()
    for p in patterns:
        if p.lower() in low:
            return p
    if lang in ("c", "cpp", "cuda"):
        # fopen / freopen with a WRITE-capable mode (w, a, +) — reading is fine.
        m = re.search(r"\bfopen\s*\(\s*[^,]+,\s*[\"']([^\"']*)[\"']\s*\)", code)
        if m and any(ch in m.group(1).lower() for ch in ("w", "a", "+")):
            return f"fopen write mode '{m.group(1)}'"
        m = re.search(r"\bfreopen\s*\(\s*[^,]+,\s*[^,]+,\s*[\"']([^\"']*)[\"']\s*\)", code)
        if m and any(ch in m.group(1).lower() for ch in ("w", "a", "+")):
            return f"freopen write mode '{m.group(1)}'"
        m = re.search(r"\bopen\s*\(\s*[\"'][^\"']*[\"']\s*,\s*(O_WRONLY|O_CREAT|O_APPEND|O_TRUNC|O_RDWR)\b", code)
        if m:
            return f"open write flag '{m.group(1)}'"
    return None
def normalize_code(code: str, language: str = "c") -> str:
    """Semantic normalization for dedup.  C-family languages keep the
    full brace/preprocessor-aware normalizer; hash-comment languages
    (python, shell, …) get a light whitespace+comment normalization."""
    from .languages import normalize_lang
    lang = normalize_lang(language)
    if lang in ("python", "ruby", "perl", "shell", "r", "lua", "haskell"):
        lines = [l for l in code.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        return re.sub(r"\s+", " ", " ".join(lines)).strip()
    # C-family path (unchanged).

    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", " ", code)

    # Split into top-level units (preprocessor lines, blocks, statements).
    units: List[str] = []
    current: List[str] = []
    brace = 0
    in_preproc = False
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if brace == 0 and not in_preproc and c == "#" and (i == 0 or code[i - 1].isspace() or code[i - 1] == "\n"):
            in_preproc = True
        current.append(c)
        if in_preproc:
            if c == "\n" and (i == 0 or code[i - 1] != "\\"):
                units.append("".join(current).strip())
                current = []
                in_preproc = False
        else:
            if c == "{":
                brace += 1
            elif c == "}":
                brace -= 1
                if brace == 0:
                    units.append("".join(current).strip())
                    current = []
            elif c == ";" and brace == 0:
                units.append("".join(current).strip())
                current = []
        i += 1
    if current:
        units.append("".join(current).strip())
    units = [u for u in units if u]

    # Drop preprocessor directives (includes/defines vary harmlessly).
    body = [u for u in units if not u.lstrip().startswith("#")]
    if not body:
        return ""

    TOKEN = re.compile(
        r'("(?:[^"\\]|\\.)*")'
        r"|('(?:[^'\\]|\\.)*')"
        r"|([0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?[uUlLfF]*)"
        r"|([A-Za-z_][A-Za-z0-9_]*)"
        r"|(\S+)"
        r"|(\s+)"
    )
    C_KEYWORDS = {
        "auto", "break", "case", "char", "const", "continue", "default", "do",
        "double", "else", "enum", "extern", "float", "for", "goto", "if",
        "inline", "int", "long", "register", "restrict", "return", "short",
        "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
        "unsigned", "void", "volatile", "while", "_Bool", "_Complex", "_Imaginary",
    }
    STDLIB_KEEP = re.compile(
        r"^(__.*|_mm[0-9a-zA-Z_]*|_MM[0-9a-zA-Z_]*"
        r"|u?int(8|16|32|64|ptr|max)_t|size_t|ssize_t|ptrdiff_t"
        r"|bool|true|false|NULL|[A-Z][A-Z0-9_]{2,})$"
    )

    def keep(name: str) -> bool:
        return name in C_KEYWORDS or bool(STDLIB_KEEP.match(name))

    ident_set: set[str] = set()
    for piece in body:
        for m in TOKEN.finditer(piece):
            _, _, _, ident, _, _ = m.groups()
            if ident and not keep(ident):
                ident_set.add(ident)
    ident_map = {name: f"_I{i}" for i, name in enumerate(sorted(ident_set))}

    norm_units: List[str] = []
    for piece in body:
        parts: List[str] = []
        for m in TOKEN.finditer(piece):
            s_lit, c_lit, num, ident, op, ws = m.groups()
            if ws:
                parts.append(" ")
            elif s_lit:
                parts.append("_STR_")
            elif c_lit:
                parts.append("_CHR_")
            elif num:
                parts.append(num)
            elif ident:
                parts.append(ident_map.get(ident, ident))
            elif op:
                parts.append(op)
        norm = re.sub(r" +", " ", "".join(parts)).strip()
        if norm:
            norm_units.append(norm)

    norm_units.sort()  # order no longer matters
    return re.sub(r" +", " ", " ".join(norm_units)).strip()


def semantic_hash(code: str, language: str = "c") -> str:
    """Content hash through the language-aware normalizer.  Callers MUST pass
    the project language — the default 'c' treats `//` as a comment, so on
    hash-comment languages (python, shell, ruby, …) floor division would be
    stripped before hashing and distinct candidates could collide."""
    return hashlib.sha256(normalize_code(code, language).encode("utf-8")).hexdigest()


def semantic_hash_file(path: str | Path, language: Optional[str] = None) -> str:
    """Hash a file's content with the language inferred from its extension
    (falls back to the raw byte hash when unreadable or unknown)."""
    from .languages import lang_from_ext
    lang = language or lang_from_ext(Path(path).suffix) or "c"
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return semantic_hash(f.read(), lang)
    except Exception:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ===========================================================================
# INTEGRITY — file comparison (merged from sameness-check)
# ===========================================================================


def file_compare(path_a: str | Path, path_b: str | Path) -> Dict[str, Any]:
    """Compare two files: identical, size match, similarity (0-100)."""
    try:
        import difflib
        pa, pb = Path(path_a), Path(path_b)
        identical = pa.read_bytes() == pb.read_bytes()
        text_a = pa.read_text(encoding="utf-8", errors="replace")
        text_b = pb.read_text(encoding="utf-8", errors="replace")
        similarity = round(difflib.SequenceMatcher(None, text_a.lower(), text_b.lower()).ratio() * 100.0, 2)
        return {
            "identical": identical,
            "similarity": similarity,
            "size_a": pa.stat().st_size,
            "size_b": pb.stat().st_size,
        }
    except Exception as e:
        return {"identical": False, "similarity": 0.0, "error": str(e)}


# ===========================================================================
# PROMPTS — modular blocks
#
# A project prompt is assembled from ordered BLOCKS (markdown files in
# projects/<id>/prompts/blocks/).  Structural blocks (contract, metrics,
# memory, code, output format) are always included; VARIANT blocks are
# picked at random, or pinned / steered by the user's instructions in the
# project spec:
#
#   "prompts": {
#     "goal": "...",                      # task description (block 0)
#     "user_instructions": "...",         # always injected verbatim
#     "generation_blocks": ["contract", "metrics", "memory", "current_code", "output_format"],
#     "variant_blocks": ["focus_zipf", "aggressive", "analytical"],
#     "variant_mode": "random",           # random | all | pinned
#     "variant_n": 2,                     # how many random variants
#     "pinned_blocks": []
#   }
#
# Every block is {{VARIABLE}}-substituted; the engine supplies a single
# canonical variable set (plus legacy aliases for ported prompts).
# ===========================================================================


def load_blocks(directory: str | Path) -> Dict[str, str]:
    """Load all .md files in a blocks directory: name (stem) -> content."""
    d = Path(directory)
    blocks: Dict[str, str] = {}
    if not d.exists():
        return blocks
    for path in sorted(d.iterdir()):
        if path.is_file() and path.suffix.lower() == ".md":
            blocks[path.stem] = path.read_text(encoding="utf-8")
    return blocks


def assemble_prompt(
    spec: Dict[str, Any],
    variables: Dict[str, str],
    blocks_dir: str | Path,
    stage: str = "generation",
    rng: Optional[Any] = None,
    blocks: Optional[Dict[str, str]] = None,
    default_structural: Optional[List[str]] = None,
) -> str:
    """Assemble a prompt from the project spec's block configuration.

    `blocks` (name -> content) overrides loading from `blocks_dir` — the
    engine merges framework default blocks with project blocks and passes
    the merged dict.  `default_structural` is used when the spec declares
    no structural block list.
    """
    prompts_cfg = spec.get("prompts", {}) or {}
    rng = rng or random

    # Per-stage block config (falls back to the shared generation config).
    stage_cfg = prompts_cfg.get(f"{stage}_blocks_config") or prompts_cfg
    structural = list(stage_cfg.get(f"{stage}_blocks", stage_cfg.get("generation_blocks", [])) or [])
    if not structural and default_structural:
        structural = list(default_structural)
    variants_pool = list(stage_cfg.get("variant_blocks", []) or [])
    mode = stage_cfg.get("variant_mode", "random")
    n = int(stage_cfg.get("variant_n", 2))
    pinned = list(stage_cfg.get("pinned_blocks", []) or [])

    if blocks is None:
        blocks = load_blocks(blocks_dir)

    names: List[str] = []
    # User instructions always come first — highest authority.
    user_instructions = (stage_cfg.get("user_instructions") or "").strip()
    if user_instructions:
        names.append("__user_instructions__")

    for name in structural:
        if name not in names:
            names.append(name)

    if mode == "pinned":
        for name in pinned:
            if name not in names:
                names.append(name)
    elif mode == "all":
        for name in variants_pool:
            if name not in names:
                names.append(name)
    else:  # random
        pool = [b for b in variants_pool if b not in names and b in blocks]
        chosen = rng.sample(pool, min(n, len(pool))) if pool else []
        for name in chosen:
            names.append(name)

    parts: List[str] = []
    for name in names:
        if name == "__user_instructions__":
            parts.append(user_instructions)
            continue
        content = blocks.get(name)
        if content is None:
            continue
        parts.append(substitute(content, variables))

    # Goal always leads unless explicitly placed.
    goal = (prompts_cfg.get("goal") or spec.get("description") or "").strip()
    if goal:
        parts.insert(0, goal)

    return "\n\n".join(p for p in parts if p.strip())


def substitute(template: str, variables: Dict[str, str]) -> str:
    for key, value in variables.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


# ===========================================================================
# DEEPWORK — agentic analysis loop (merged from the sorting project)
# ===========================================================================

DEFAULT_DEEPWORK_PROMPT = """You are an expert analyst studying the generated programs for the {project_name} project.
You do NOT have access to the full dataset — the tools below give it to you.
Your job: read programs, query the results, find patterns, and write a memo that gives real direction.
YOU HAVE MANY TURNS. EACH TURN: issue commands, then read the outputs next turn. DON'T TRY TO DO EVERYTHING IN ONE TURN.
=================================================================
BENCHMARK CONTEXT
=================================================================
{briefing}
=================================================================
YOUR COMMANDS (plain text, one per line; each MUST be preceded by a "Rationale:" line)
=================================================================
- {YELOOK} <query>        → top results / program listings (syntax: "list top 10" or a results-store query)
- {READ} <folder_number>  → full source of program.c from that folder
- {PANDAS} result = <expr>→ custom pandas query on the results store
- {LESSON} <folder>       → read the lesson written for that folder
- {MEMO} <folder>         → read the deepwork memo for that folder
=================================================================
GOAL
=================================================================
Find what techniques give the best results. Read at least 4 programs.
When done, write:
  <DEEPWORK_MEMO>
  [150-250 word memo with real function names and explanation]
"""


class DeepworkAgent:
    """Multi-turn agentic loop. `tools` maps magic codes to callables."""

    def __init__(
        self,
        prompt: str,
        tools: Dict[str, Callable[[str], str]],
        request: Callable[[str], str],
        max_turns: int = 50,
        min_reads: int = 4,
    ):
        self.prompt = prompt
        self.tools = tools
        self.request = request
        self.max_turns = max_turns
        self.min_reads = min_reads

    def run(self) -> str:
        conversation = self.prompt
        cot_re = re.compile(r"<\|[^|]+\|>")
        tool_re = re.compile(
            r"(" + "|".join(re.escape(c) for c in self.tools) + r")[\s\\\"']*((?:[^\n\"'}\]\\]|\\.)+)",
            re.IGNORECASE,
        )
        reads = 0
        read_folders: set = set()
        cache: Dict[str, str] = {}

        for turn in range(self.max_turns):
            raw = self.request(conversation)
            markers = list(cot_re.finditer(raw))
            clean = raw[markers[-1].end():].strip() if markers else raw.strip()
            conversation += f"\n\nAssistant: {clean}"

            # Memo check
            memo = _extract_memo(clean)
            if memo is not None:
                if reads < self.min_reads:
                    conversation += f"\n\nYou have only read {reads} programs. Read at least {self.min_reads} first."
                    continue
                if read_folders and not any(f in memo for f in read_folders):
                    conversation += f"\n\nThe memo must reference a folder you actually read ({', '.join(sorted(read_folders))}). Rewrite it."
                    continue
                return memo

            clean_no_cot = cot_re.sub("", clean)
            matches = list(tool_re.finditer(clean_no_cot))
            if not matches:
                conversation += "\n\nNo command found. Write 'Rationale: ...' then a command, or <DEEPWORK_MEMO> to finish."
                continue

            results = []
            for m in matches:
                code = m.group(1).lower()
                args = m.group(2).strip().rstrip("\\\"' ").strip()
                key = f"{code} {args}"
                if key in cache:
                    results.append(cache[key])
                    continue
                fn = self.tools.get(code)
                if fn is None:
                    results.append(f"ERROR: unknown command code {code}")
                    continue
                try:
                    out = fn(args)
                except Exception as e:
                    out = f"ERROR: {e}"
                if code.startswith("r"):  # read-like tools count as reads
                    reads += 1
                    mf = re.search(r"\b(\d{4,})\b", args)
                    if mf:
                        read_folders.add(mf.group(1))
                cache[key] = out
                results.append(out)
            conversation += "\n\n" + "\n\n".join(results)

        return "DEEPWORK TIMEOUT -- no memo produced."


def _extract_memo(text: str) -> Optional[str]:
    for marker in ("<DEEPWORK_MEMO>", "**DEEPWORK_MEMO**", "`DEEPWORK_MEMO`", "DEEPWORK_MEMO:"):
        if marker in text:
            return text.split(marker)[1].strip()
    return None


# ===========================================================================
# RESULTS STORE — per-project CSV of scored generations
# ===========================================================================


class ResultsStore:
    """Per-project results table (the sorter project's resultino.csv pattern,
    generalized).  Columns: generation + outcome + fitness + one column per
    metric.  The header widens as new metrics appear; reading is
    header-robust (canonical widest header, positional mapping) so a stale
    or patchwork header can never drop data."""

    def __init__(self, project_dir: str | Path, name: str = "results.csv"):
        self.path = Path(project_dir) / name

    def _canonical_header(self) -> Optional[List[str]]:
        import csv
        if not self.path.exists() or not self.path.stat().st_size:
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = list(csv.reader(f))
        except Exception:
            return None
        if not lines:
            return None
        header = lines[0]
        for r in lines[1:]:
            if len(r) >= 2 and r[0] == "generation" and r[1] == "outcome" and len(r) > len(header):
                header = r
        return header

    def read(self) -> List[Dict[str, Any]]:
        import csv
        if not self.path.exists() or not self.path.stat().st_size:
            return []
        header = self._canonical_header()
        if not header:
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = list(csv.reader(f))
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for r in lines[1:]:
            if not r or (r[0] == "generation" and len(r) >= 2 and r[1] == "outcome"):
                continue  # stray header line
            out.append({name: (r[i] if i < len(r) else "") for i, name in enumerate(header)})
        return out

    def append(self, row: Dict[str, Any]) -> None:
        import csv
        header = list(row.keys())
        existing = self._canonical_header()
        merged = list(dict.fromkeys((existing or []) + header))
        rows = self.read() if existing else []
        rows.append(row)
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=merged)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in merged})

    def query(self, expr: str) -> str:
        """Run a pandas expression over the store (deepwork tool)."""
        try:
            import pandas as pd
        except ImportError:
            return ("ERROR: pandas not installed — this tool needs it "
                    "(pip install pandas). The store itself is plain CSV.")
        rows = self.read()
        if not rows:
            return "ERROR: results store empty"
        df = pd.DataFrame(rows)
        for col in df.columns:
            try:
                df[col] = pd.to_numeric(df[col], errors="ignore")
            except Exception:
                pass
        ns = {"df": df, "pd": pd, "os": os}
        try:
            exec(expr, {"__builtins__": {}}, ns)
            return str(ns.get("result", "(no 'result' variable set)"))
        except Exception as e:
            return f"ERROR: {e}"
