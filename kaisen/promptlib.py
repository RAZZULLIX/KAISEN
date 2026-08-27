# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Prompt library — the single source of truth for every LLM interaction.

House rules (applied everywhere, mechanically):
  - ONE output contract per scenario, stated FIRST and LAST.
  - Small models get few-shot examples + zero meta-talk; large models get
    the same contract plus richer context.
  - Every prompt names the exact marker that begins the expected output.
  - Sloppy user input is normalized BEFORE it reaches a prompt: goals are
    expanded into explicit, measurable instructions (expand_goal()).

Model tiers:
  tiny  — < 8k context or tiny parameter counts: maximal scaffolding
  small — local llamas (8k-50k): strict format, one short example
  large — frontier APIs / big-context models: strict format + reasoning
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .languages import ext_from_lang, fence_from_lang, normalize_lang, toolchain_from_lang


def detect_tier(server: Optional[Dict[str, Any]] = None) -> str:
    """Infer a model tier from a server snapshot. Explicit server.tier wins."""
    if not server:
        return "small"
    if server.get("tier") in ("tiny", "small", "large"):
        return str(server["tier"])
    name = f"{server.get('model', '')} {server.get('id', '')}".lower()
    if server.get("type") == "openai":
        return "large"
    if any(t in name for t in ("0.5b", "1b", "1.5b", "3b", "4b", "7b", "8b", "qwen2.5-1", "qwen2.5-3")):
        return "tiny"
    return "small"


def expand_goal(raw_goal: str) -> str:
    """Normalize a sloppy user instruction into a measurable directive.

    Plain-language goals ("make it fast", "use less ram") become explicit
    targets with acceptance phrasing, so ANY model gets a real spec."""
    g = (raw_goal or "").strip()
    if not g:
        return "Improve the program: correctness first, then any metric the harness scores."
    g = g.rstrip(".!?") + "."
    low = g.lower()
    if any(w in low for w in ("fast", "faster", "speed", "quicker", "accelerat")):
        g += " Optimize for SPEED: the score harness reports the time/speed metric; make it as low/fast as possible while keeping identical outputs."
    if any(w in low for w in ("ram", "memory", "smaller", "compress", "size", "lighter")):
        g += " Optimize for MEMORY/SIZE: the score harness reports the size/memory metric; minimize it while keeping identical outputs."
    if not any(w in low for w in ("speed", "ram", "memory", "size", "compress", "faster", "smaller")):
        g += " Prefer strictly better results on every metric the harness scores; never trade correctness."
    return g


# ===========================================================================
# GENERATION — champion improvement (per generation)
# ===========================================================================

def generation_boost(tier: str, language: str) -> str:
    """Extra block appended to the engine's generation prompt, per tier.
    The engine's structural blocks (contract/metrics/memory/code) already
    carry the context; this block fixes the OUTPUT contract for the tier."""
    fence = fence_from_lang(language)
    if tier == "tiny":
        return f"""FORMAT (MANDATORY):
Reply with EXACTLY one code block tagged ```{fence}. Nothing else — no
text, no lists, no "here is". The block contains the COMPLETE program.
If you cannot improve it, output the current program unchanged.
An empty or partial reply is a failure."""
    if tier == "small":
        return f"""FORMAT (MANDATORY):
One code block, tagged ```{fence}, containing the COMPLETE program.
No explanations. No diffs. No text outside the block.
If you cannot improve it, output the current program unchanged."""
    return f"""FORMAT:
One code block, tagged ```{fence}, containing the COMPLETE improved
program. You MAY reason first, outside the block; the block is the
deliverable. Prefer correctness-preserving changes; never silently change
the program's contract."""


# ===========================================================================
# SWARM — planner / executor / critic / synthesizer
# ===========================================================================

def swarm_planner(tier: str, user_request: str, max_tasks: int = 6) -> str:
    rules = (
        "Output ONLY the numbered list. No thinking, no explanations."
        if tier in ("tiny", "small")
        else "Output a numbered list; reasoning may precede it but the list must be unambiguous."
    )
    return f"""You are a strict task decomposition agent. Break the request into independent
sub-tasks that separate agents can execute CONCURRENTLY.

Rules:
- Each sub-task must be self-contained and actionable.
- No sub-task may depend on another sub-task's output.
- Do NOT include a synthesis/final-answer task (a separate agent handles it).
- At most {max_tasks} tasks. Fewer is fine; parallel wins.
- {rules}

REQUEST:
{expand_goal(user_request)}

OUTPUT:
1. """


def swarm_executor(tier: str, user_request: str, task: str, language: str = "c") -> str:
    fence = fence_from_lang(language)
    small_rule = (
        f"""RULES:
1. Your result must be ONE code block tagged ```{fence} with the complete program.
2. Nothing outside the block. No explanations.
3. Preserve the program's contract exactly."""
        if tier in ("tiny", "small")
        else f"""RULES:
1. The deliverable is ONE code block tagged ```{fence} with the complete program.
2. Reasoning may appear outside the block.
3. Preserve the program's contract exactly."""
    )
    return f"""You are an expert {language} programmer executing ONE task of a larger request.

ORIGINAL REQUEST: {expand_goal(user_request)}

YOUR TASK: {task}

{small_rule}

OUTPUT:"""


def swarm_executor_text(tier: str, user_request: str, task: str) -> str:
    """Executor for generic (non-code) answer tasks — no code contract."""
    rule = (
        "Output ONLY the result of the task. No explanations, no meta-commentary."
        if tier in ("tiny", "small")
        else "Output the result of the task; concise reasoning first is allowed."
    )
    return f"""You are a task execution agent. Complete ONE task of a larger request.

ORIGINAL REQUEST: {expand_goal(user_request)}

YOUR TASK: {task}

RULES:
- {rule}
- Answer the task directly; do not invent context or mention other tasks.

OUTPUT:"""


def swarm_critic(tier: str, user_request: str, candidates: List[Dict[str, str]]) -> str:
    listing = "\n\n".join(
        f"CANDIDATE {i}:\n{c.get('code', c.get('text', ''))[:6000]}"
        for i, c in enumerate(candidates, 1)
    )
    fmt = (
        'Reply with ONLY: "BEST: <number>\\nREASON: <one sentence>"'
        if tier in ("tiny", "small")
        else 'Reply with "BEST: <number>" and a REASON line, one sentence each. Reasoning first is allowed.'
    )
    return f"""You are the final judge. Several agents produced candidate programs for the
same request. Choose the single best candidate.

Judging criteria, in order: correctness (contract preserved), how directly it
fulfills the request, then quality/simplicity.

REQUEST: {expand_goal(user_request)}

{listing}

{fmt}

OUTPUT:"""


# ===========================================================================
# STEPWISE PROJECT BUILDER — one small job per prompt, assembled at the end
# ===========================================================================

def step_goal_analysis(tier: str, goal: str, code: str = "") -> str:
    """STEP 1 — turn the user's words into a crisp engineering spec.

    When the user provided a program, the analysis MUST be about THAT
    program: the entry contract comes from the code, never invented."""
    strict = "Reply with ONLY the JSON object. No text before or after." if tier in ("tiny", "small") else "Reply with one JSON object."
    code_block = ""
    if code.strip():
        code_block = f"""
THE USER PROVIDED THIS EXACT PROGRAM (it already exists and works — the
project will evolve THIS file, keeping its interface):
```
{code.strip()[:4000]}
```
CRITICAL:
- Derive summary and entry_contract from THIS code, not from your
  imagination. Read what it actually does and how it is invoked.
- If the file has no main entry point (a library/kernel source file), the
  harness may add an entry point (compiled languages) or call the
  functions directly (interpreted languages) — describe the exact function
  signature(s) and semantics in entry_contract so the harness can invoke
  them.
- You MUST NOT change the interface in entry_contract: the candidates
  keep the same signatures, only internals may change.
"""
    return f"""You are the ANALYSIS step of a project builder. Translate the user's goal
into a precise specification. This is your ONLY job — do not write code.

GOAL:
{goal}
{code_block}
Reply with a JSON object with EXACTLY these keys:
{{
  "summary": "one sentence — what the program does",
  "entry_contract": "the exact entry point and I/O contract (how a harness invokes it: CLI args, stdin/stdout format, function signature)",
  "artifact_name": "program",
  "metrics": [
    {{"key": "time_ms", "label": "Time per run", "unit": "ms", "direction": "lower", "weight": 1, "constraint": null}}
  ],
  "verify_idea": "how to prove the output is correct (2 sentences: reference method + test cases)",
  "score_idea": "what the benchmark measures and how, deterministically (2 sentences)"
}}

RULES:
- metrics: the numbers the goal wants to optimize. direction is "lower" or
  "higher". At least one metric; speed and size get "lower".
  `constraint` (optional number) is a HARD gate: candidates that violate
  it are REJECTED no matter how good the other metrics are. Use it for
  "without changing the output": e.g. max error <= 0.01, min compression
  ratio >= 2.0, identical output size. Leave null when the metric is a
  pure objective.
- entry_contract must be testable from outside the program.


{strict}

OUTPUT (JSON only):"""


def step_baseline_program(tier: str, goal: str, analysis: str, language: str) -> str:
    """STEP 2 — write the baseline program from scratch (no user code)."""
    from .languages import fence_from_lang
    fence = fence_from_lang(language)
    strict = "Reply with ONLY one code block, tagged ```%s. Nothing outside." % fence if tier in ("tiny", "small") else "Reply with one code block, tagged ```%s." % fence
    return f"""You are the BASELINE step of a project builder. Write the complete,
CORRECT, SIMPLE {language} program that fulfills the specification below.
Performance comes LATER through evolution — correctness and clarity first.

GOAL:
{goal}

ANALYSIS (from the previous step):
{analysis}

RULES:
- Complete program, no placeholders, no TODOs.
- Satisfy the entry contract EXACTLY.
- Use ONLY the standard library — no external libraries (boost, gmp, etc.).
  The build machine has just the compiler. Implement any bignum/arithmetic
  you need yourself.
- The code block contains ONLY machine-readable code. NO commentary, NO
  prose, NO bullet lists inside it — every line must compile.
- Implement the computation with a real algorithm (e.g. spigot, Machin-like
  series, integer arithmetic). Do NOT paste precomputed digit strings.
- {strict}

OUTPUT:"""


def step_driver_program(tier: str, analysis: str, language: str) -> str:
    """STEP 2b — the candidate is a LIBRARY file (no main): write the
    driver entry point that exercises it. The build step compiles/links it
    TOGETHER with the candidate, so it must be written in the SAME language
    as the candidate (never a hardcoded C/C++ driver)."""
    fence = fence_from_lang(language)
    driver_file = f"harness/driver{ext_from_lang(language)}"
    strict = "Reply with ONLY one code block, tagged ```%s. Nothing outside." % fence if tier in ("tiny", "small") else "Reply with one code block, tagged ```%s." % fence
    return f"""You are the DRIVER step of a project builder. The candidate file is a
LIBRARY source file with NO main entry point — it only defines the
function(s) in the entry contract. Write {driver_file}: a complete
{language} program whose main entry point drives those functions. The build
step combines it WITH the candidate, so:
- use the SAME language ({language}) as the candidate file,
- call the functions with EXACTLY the entry contract's signatures
  (declare/call them precisely as the contract describes),
- use ONLY the standard library — NO external crates/packages (rand,
  rayon, num, gmp, boost, … are NOT installed and NOT downloadable: the
  build machine has only the bare {language} toolchain, no package
  manager, no network).  If you need pseudo-random inputs, implement a
  tiny deterministic generator (xorshift/LCG) inline with std only;
  no files, no network.

HOW IT LINKS (read this — the build step follows the SAME rule):
- The build step copies the candidate file to candidate{ext_from_lang(language)} in
  the SAME directory as this driver, then compiles/links them together.
- If the {language} compiler accepts multiple source files in one
  invocation (C/C++/CUDA/D/Go/Java/Kotlin/Swift/Scala/Haskell/Zig...), the
  build passes both files and you call the entry-contract functions
  directly.
- If the {language} compiler takes ONE entry file (Rust/rustc), the build
  compiles THIS driver only, so you must declare the candidate as a module
  of the same directory — `mod candidate;` (file candidate{ext_from_lang(language)}) —
  and call its functions as `candidate::<function>(...)`.

ANALYSIS (read the entry_contract carefully — use EXACTLY those signatures):
{analysis}

WHAT THE DRIVER MUST DO:
- CLI CONTRACT (the python harness invokes this driver as a program):
  * NO argument, or an argument that is NOT "1" and NOT a number ->
    benchmark mode (below);
  * FIRST argument "1" -> verify mode (below);
  * FIRST argument a NUMBER (including "0") -> compute the entry-contract
    function for that input and print the result as ONE line (the python
    verifier compares these lines against its own reference), then exit 0.
- benchmark mode: a FIXED deterministic input set of AT LEAST 1<<20
  elements within a BOUNDED range the naive baseline handles FAST (each
  call microseconds-to-milliseconds; e.g. inputs in 0..1_000_000 — NEVER
  full-range random 64-bit values, which make a naive O(n√n) baseline
  take hours). Loop rounds until a wall-clock budget of AT MOST ~5 s
  elapses (the score step has a hard timeout — the ENTIRE benchmark mode
  must finish within a few seconds), then print the metric line(s)
  the analysis declares (e.g. time_ms=..., the AVERAGE per call measured
  INSIDE the driver) plus a KAISEN_PROGRESS rounds=... line, flushing
  output after each progress line.
- verify mode: call the function(s) and compare against a simple scalar
  reference computed IN the driver, print "OK" or "FAIL", exit non-zero
  on FAIL.  Test a sweep spanning the FULL input range (a few extreme
  values, small inputs, and points spread across the range), NOT a
  handful of hand-picked values — report the MAX error.
  CRITICAL: the ENTIRE verify run must finish in a few SECONDS even for
  the naive baseline — scale the number of points (a few dozen to a few
  hundred) and the largest input to the function's speed.
  IMPORTANT: optimized math reorders float operations — use a RELATIVE
  tolerance of at least 1e-5 (abs(got - ref) <= 1e-5 * (1 + abs(ref))).
  Never require bit-exact equality: FMA vs scalar differs in the last bits.

RULES:
- {strict}

OUTPUT:"""


def step_build_script(tier: str, language: str, kind: str, with_driver: bool = False) -> str:
    """STEP 3 — build.py: candidate -> artifact."""
    strict = "Reply with ONLY one python code block. Nothing outside." if tier in ("tiny", "small") else "Reply with one python code block."
    if kind == "compiled":
        toolchain = toolchain_from_lang(language) or f"the {language} compiler"
        if with_driver:
            driver_part = (f" The candidate is a LIBRARY file: copy it to "
                           f"harness/candidate{ext_from_lang(language)} (the SAME directory as "
                           f"harness/driver{ext_from_lang(language)}), then compile the DRIVER as the "
                           f"entry point together with the candidate per the {language} rules — "
                           f"multi-input compilers (gcc, g++, javac, go build, ghc, swiftc, kotlinc, "
                           f"scalac, zig): pass BOTH files in one command; single-entry compilers "
                           f"(rustc): compile ONLY the driver, which declares the candidate as "
                           f"`mod candidate;`.")
        else:
            driver_part = ""
        how = (f"compile the candidate {language} file (argv[1]) into the artifact (argv[2]) with the "
               f"{language} toolchain ({toolchain}). Use the language's standard optimization flags "
               f"(e.g. -O2/-O3 for gcc/g++/clang, -O or -C opt-level=3 for rustc, -O2 for zig, release "
               f"mode for go/dart, defaults for javac/ghc/swiftc; NOTE: --release is a CARGO flag, "
               f"NOT a rustc flag).{driver_part} "
               "Always write the compiler's stderr to YOUR stderr (success AND failure). Exit non-zero on failure.")
    else:
        how = ("validate the candidate (argv[1]) — e.g. py_compile / node --check — then copy it to the "
               "artifact path (argv[2]) as an EXECUTABLE script: prepend a shebang line if missing "
               "(#!/usr/bin/env python3, node, ruby, …) and chmod +x, so harness steps can run the "
               "artifact directly (e.g. subprocess.run([artifact, arg])). Exit non-zero on failure.")
    return f"""You are the BUILD step of a project builder. Write build.py (python3, stdlib only).

CONTRACT:
- argv: [build.py, candidate, artifact]
- {how}


RULES:
- No subprocess with shell=True, no os.system.
- {strict}

OUTPUT:"""


def step_verify_script(tier: str, analysis: str, language: str, data_note: str = "",
                       with_driver: bool = False) -> str:
    """STEP 4 — verify.py: prove correctness."""
    strict = "Reply with ONLY one python code block. Nothing outside." if tier in ("tiny", "small") else "Reply with one python code block."
    driver_rule = ("\n- THE ARTIFACT IS A STANDALONE EXECUTABLE (driver main inside it). Its FIRST "
                   "argument is: '1' = verify (prints \"OK\"/\"FAIL\" after an internal dense sweep); "
                   "a NUMBER (including 0) = it prints the function's result for that input as one "
                   "line — compare those lines against your reference; no argument = benchmark "
                   "(runs seconds, never prints OK). NEVER use ctypes/CDLL on it — "
                   "it is not a shared library.") if with_driver else ""
    return f"""You are the VERIFY step of a project builder. Write verify.py (python3, stdlib +
numpy if needed) that proves a candidate artifact is CORRECT for the goal.

ANALYSIS:
{analysis}

CONTRACT:
- argv: [verify.py, artifact]
- The artifact is a single executable/script (the build step made it
  runnable): run it with subprocess.run([artifact, arg...]).
- Run the artifact against a deterministic reference and a REPRESENTATIVE,
  SMALL test set (seconds even for a naive baseline).
- Print ONLY "OK" on success; explain failures on stderr; exit non-zero.
- Float comparisons with sensible tolerances; binary outputs compared byte-exact.{data_note}{driver_rule}
- On failure, print the artifact's first 200 output characters (repr) to
  stderr BEFORE the mismatch detail — that output is fed back to the
  program's author so they can fix it.

RULES:
- No subprocess with shell=True, no os.system.
- {strict}

OUTPUT:"""


def step_score_script(tier: str, analysis: str, data_note: str = "", with_driver: bool = False) -> str:
    """STEP 5 — score.py + metric declarations + parse rules."""
    strict = "Code block first, then METRICS JSON and PARSE lines — nothing else." if tier in ("tiny", "small") else "Code block first, then METRICS JSON and PARSE lines."
    driver_rule = ("\n- THE ARTIFACT IS A STANDALONE EXECUTABLE, not a shared library: run it with "
                   "subprocess.run([artifact]) (NO arguments — no-args means benchmark mode); NEVER "
                   "use ctypes/CDLL on it. It loops internally for ~5 s and prints its metric "
                   "line(s) — capture stdout and parse the metric FROM ITS OUTPUT. NEVER time the "
                   "subprocess call (startup overhead dwarfs the kernel).") if with_driver else ""
    return f"""You are the SCORE step of a project builder. Write score.py (python3) that
measures EXACTLY what the goal wants to optimize — nothing else.

ANALYSIS:
{analysis}
CONTRACT:
- The verify step ALREADY checks correctness. score.py must NEVER fail or
  raise because of a small numeric difference — it only MEASURES. No
- CRITICAL — the artifact's own printed numbers are UNTRUSTED: a lying
  candidate can print anything. MEASURE the scored quantity yourself
  (subprocess wall-time via time.perf_counter, output size, file size)
  and report YOUR measurement as the metric. The artifact's line is a
  cross-check only — a huge disagreement is a red flag, not a score.
- argv: [score.py, artifact]
- Deterministic benchmark: the same fixed workload every run. Loop rounds
  until a wall-clock budget (~5-15 s) elapses, count completed rounds.
- The artifact is a single executable/script (the build step made it
  runnable): run it with subprocess.run([artifact, arg...]). If the
  artifact can never be executed (zero completed rounds), that is a
  FAILURE, not a zero metric — print a clear error to stderr and exit
  non-zero.
- While running, print live updates every 0.5-2 s, ALWAYS with flush=True:
  KAISEN_PROGRESS <key>=<value> <key>=<value>
- At the end print ONE final line per metric: <key>=<value>
- Then, AFTER the code block, output EXACTLY:
  METRICS JSON: {{"key": {{"label": "...", "unit": "...", "direction": "lower|higher", "weight": 1}}}}
  PARSE:
  <key>=(?P<<key>>[\\\\d.]+)
  one PARSE line per metric — a regex whose named group equals the metric key.{data_note}{driver_rule}

RULES:
- No subprocess with shell=True, no os.system.
- IMPORTANT: the METRICS JSON and PARSE lines must go AFTER the closing
  fence of the code block, OUTSIDE it — never inside the script.
- {strict}

OUTPUT:"""


def step_spec_repair(tier: str, spec_json: str, errors: str) -> str:
    """STEP R — repair the assembled spec after validation failures."""
    strict = "Reply with ONLY the corrected JSON object. No text before or after." if tier in ("tiny", "small") else "Reply with the corrected JSON object."
    return f"""You are the REPAIR step of a project builder. The assembled project spec
was validated and REJECTED. Fix ONLY what the errors describe and return
the complete corrected JSON (same schema, all fields).

CURRENT SPEC:
{spec_json}

VALIDATION ERRORS:
{errors}

RULES:
- Fix exactly the reported problems; keep everything else identical.
- {strict}

OUTPUT (JSON only):"""


def repair_prompt(lang: str, goal: str, source: str, stderr: str) -> str:
    """LLM LAST-RESORT build repair: the candidate failed to compile and
    the deterministic fixer is exhausted. One guarded rewrite attempt —
    the reply is danger-scanned and re-runs the full pipeline."""
    fence = fence_from_lang(lang)
    return f"""You are the last-resort BUILD REPAIRER of an automatic evolution pipeline.
A generated {lang} program fails to compile. The deterministic fixer
could not repair it. Fix the code so it builds while keeping its
behavior, interface, and the exact metrics the harness measures.

PROJECT GOAL (context only — do not add features):
{goal}

COMPILER ERROR (tail):
{stderr}

CURRENT PROGRAM:
```{fence}
{source}
```

HARD RULES — violations mean the fix is discarded:
- Reply with ONLY the complete corrected program inside one ```{fence}
  fenced block. No commentary outside the fence.
- Fix ONLY the build problem. Never redesign the algorithm.
- No system(), no network, no file deletion, no shell escape.
- Keep every function signature the harness may call.

OUTPUT:"""


def swarm_synthesizer(tier: str, user_request: str, outputs: List[str]) -> str:
    context = "\n\n".join(f"SUB-TASK RESULT {i}:\n{o[:8000]}" for i, o in enumerate(outputs, 1))
    rule = (
        "Output ONLY the final answer. Do not mention sub-tasks or the process."
        if tier in ("tiny", "small")
        else "Output the final answer; do not mention sub-tasks or the process."
    )
    return f"""You are the synthesis agent. Combine the sub-task results into one coherent
final answer that directly addresses the request.

REQUEST: {expand_goal(user_request)}

{context}

RULES:
- {rule}
- If results conflict, prefer the most correct/complete one and note the choice.

OUTPUT:"""


# ===========================================================================
# CONFIG AGENT — natural language reconfiguration of the GUI/backend
# ===========================================================================

CONFIG_AGENT_ACTIONS = """{
  "action": "set_pref",          // ui prefs: {"path": "theme.accent", "value": "#ff6b35"} | {"path": "layout.density", "value": "comfortable"}
  "action": "edit_project",      // project spec edit: {"id": "my-project", "changes": {spec fields...}}
  "action": "add_metric",        // {"id": "my-project", "key": "bytes", "label": "Size", "unit": "bytes", "direction": "lower", "weight": 1}
  "action": "run_smoke",         // {"id": "my-project"}
  "action": "answer",            // {"text": "..."}
}"""


def config_agent_prompt(tier: str, user_request: str, context: str) -> str:
    strict = (
        "Reply with ONLY one JSON object. No text before or after."
        if tier in ("tiny", "small")
        else "Reply with one JSON object; reasoning may precede it."
    )
    return f"""You are KAISEN's configuration agent. The user configures the app in plain
words; you translate that into ONE validated action.

You MAY choose one of these actions (exact schema):
{CONFIG_AGENT_ACTIONS}

APP STATE:
{context}

USER SAID:
{user_request}

Rules:
- Never invent keys not listed; unknown requests become {{"action": "answer", "text": "..."}}.
- For edits, only include the fields that must CHANGE.
- {strict}

OUTPUT (JSON only):"""


# ===========================================================================
# PROJECT AGENT — multi-turn tool loop over a project
# ===========================================================================

AGENT_TOOLS = """TOOLS (call with JSON lines, ONE object per line):
{"tool": "read_spec"}                                  -> current project spec
{"tool": "read_history", "n": 20}                      -> last N iteration outcomes
{"tool": "read_champion"}                              -> champion source (first 8000 chars)
{"tool": "read_lesson"}                                -> the lesson memory
{"tool": "run_smoke"}                                  -> run the pipeline on the baseline
{"tool": "update_spec", "changes": {spec fields}}      -> apply a spec change (validated)
{"tool": "done", "summary": "..."}                     -> finish and report
After each call you receive the tool output, then issue the next line."""


def project_agent_prompt(tier: str, project_name: str, goal: str, language: str) -> str:
    if tier in ("tiny", "small"):
        intro = (
            "You are a careful project engineer. Work in SMALL steps: read the spec, "
            "check history, then make ONE change and test it. Prefer update_spec "
            "tweaks over ambitious rewrites."
        )
    else:
        intro = (
            "You are a senior project engineer. Investigate, then act: fix what the "
            "history shows is failing, tighten the goal, or improve the harness. "
            "Every change is validated by the framework."
        )
    return f"""You are KAISEN's autonomous project agent for "{project_name}" ({language}).

GOAL: {expand_goal(goal)}

{intro}

{AGENT_TOOLS}

Always end with {{"tool": "done", "summary": "..."}}."""


# ===========================================================================
# LESSON — memory distillation after a generation
# ===========================================================================

def lesson_prompt(tier: str, project_name: str, history_blob: str) -> str:
    rule = (
        "Output ONLY the lesson, 3-6 lines, no headers."
        if tier in ("tiny", "small")
        else "Output the lesson, 3-6 lines."
    )
    return f"""You are the memory agent for the {project_name} project. Read the recent
generation history and distill ONE lesson: what worked, what failed, what to
try next. Concrete and short.

HISTORY:
{history_blob[:6000]}

RULES:
- {rule}
- Reference real outcomes from the history.

OUTPUT:"""
