# Changelog

All notable changes to KAISEN are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [KAISEN 0.1.8-alpha (routing balance)] — 2026-09-09

The model pool fills every slot instead of hammering the fastest box.

### Fixed

- **Routing no longer starves same-priority peers.** The latency guard
  (`_slowness`, "prefer the box with a fast measured average") was a hard
  sort key ahead of the round-robin cursor — so among equal tier/priority
  servers, the fastest-measured one was ALWAYS picked first and the
  round-robin never spread. Field symptom: three identical gpt-oss boxes
  where one took ~128 requests while the others sat idle (~19 and ~1),
  against a 7-slot pool that never filled (~0.13 req/s). That is the
  opposite of the guard's intent. Now round-robin still spreads the load,
  and a server is only *deprioritized* when it is a genuine wedge — its
  measured average is both >= 3x the group's best AND >= 30 s. A modest
  spread (106 s vs 157 s, same gpt-oss class) no longer overrides
  rotation, so every server's slots get used. Verified live: gpt-oss-a/b/c
  went from 19/128/111 lifetime requests (a starved) to an even 14/10/11
  after restart, and a 1000x wedge (0.2 s vs 200 s) is still avoided.

## [KAISEN 0.1.8-alpha (candidate fallback + raw integrity)] — 2026-09-09

Never lose a generation; never guess the delimiters.

### Added

- **Candidate fallback — try the latest block, then the previous.** A
  reasoning model often writes a working program in an earlier ```` ``` ````
  block while the final one is truncated/broken. When a generation's build
  fails AND the deterministic autofix is exhausted, the engine now falls
  back to the previous candidate block and re-runs the pipeline — up to
  `autofix.max_candidates` (default **3**). Configure via config.json
  `autofix.max_candidates` or KAI `AUTOFIX candidates <n>`. `1` = old
  single-block extraction. `extract_code_candidates` returns the ordered
  best-first list (latest block first, duplicates/trivial fragments
  dropped).
- **llm_raw.txt NEVER loses output.** It now stores the full streamed
  reply (`reasoning_content`/``...`` included), not the
  `strip_reasoning`'d extract. Before, a thinking model that never closed
  its reasoning block yielded an empty raw file and the whole reasoning
  trace was gone — a lost generation AND paid-for tokens. Now:
  - `_save_raw` persists every streamed token, uncapped (falls back to the
    return only if nothing streamed);
  - OpenAI-compatible streaming captures `delta.reasoning_content` natively
    (the API tells us the delimiter — no guessing);
  - llama.cpp `reasoning_format` is learned from a FREE `/props` call, so
    we strip reasoning only when the server inlines it in content, not when
    it already separates it.
- **`extract_code` returns the LAST working block for every language** (not
  the largest reasoning snippet), and a working program inside reasoning is
  picked when the final block is absent/truncated. Regression tests in
  `tests/test_extract_code.py`.

### Fixed

- **`BUDGET` alias collision** — `"budget"` was an alias of both `BUDGET`
  and `ESTIMATE`, so `BUDGET` silently ran `ESTIMATE`; the alias index is now
  first-wins.
- `llm_raw.txt` was empty for error generations in some cases (the raw was
  written from the stripped return, or not written on request failure).

## [KAISEN 0.1.8-alpha (generation log)] — 2026-09-09

The full per-generation log is readable on demand.

### Added

- **KAI `GEN <n> [ON <pid>] [RAW|CODE|PROMPT|DIFF]`** — the complete record
  of a single generation: the prompt sent to the LLM, its RAW reply
  (reasoning included, un-truncated — the LIVE GENERATIONS window scrolls
  away too fast to read a long thinking trace), the extracted program, the
  diff vs the champion/baseline, and any repair feedback. One field arg
  (`GEN 246 CODE`) returns just that part. Backed by
  `GET /api/projects/{pid}/gen/{n}`.
- **MANUAL §5 "Where a generation goes"** — documents `runs/gen_NNNNNN/`
  (`prompt.txt`, `llm_raw.txt`, `candidate.<ext>`, `diff.json`, `program`,
  `repair.txt`) and the `GEN` command.

## [KAISEN 0.1.8-alpha (any-model extraction)] — 2026-09-09

The code extractor now returns the FINAL answer for every language, not the
largest fenced snippet.

### Fixed

- **`extract_code` picks the last real code block, language-agnostic.** The
  old logic returned `max(..., key=len)` — the LARGEST fenced block. But
  reasoning models (Qwen, DeepSeek, gpt-oss) write many ```` ``` ````
  scratch snippets while thinking, then the actual program LAST; when a
  scratch snippet happened to be longer, it won and the real program was
  discarded. Now the LAST block that contains real code wins by position
  ("largest AND last working codeblock", matching the original hand-made
  KAISEN), with a fallback to the largest only when the trailing block is a
  genuinely trivial fragment (an aborted turn). Works for all 23 languages
  via their starter patterns (`#include`/`int main`/`fn main`/`def`/…).
  Regression tests in `tests/test_extract_code.py`. Verified live: a
  DeepSeek reply (```python fenced, thinking about it, then the program)
  extracts exactly `for i in range(1, 6): print(i)` with no reasoning leak.
- **`BUDGET` command truly routes to BUDGET.** `"budget"` was an alias of
  both `BUDGET` and `ESTIMATE`; the dict-order collision made `BUDGET`
  silently run `ESTIMATE`. The alias index is now first-wins so a real
  command name is never shadowed by a synonym. `BUDGET SERVER <sid>` status
  and `…SET max_tokens N reset R` now work.

## [KAISEN 0.1.8-alpha (budget + polish)] — 2026-09-09

Per-model usage budgets + a sticky topbar.

### Added

- **Per-server usage budget.** `llm.servers[].budget` = `{max_tokens,
  max_generations, reset}` (optional). Caps how many tokens / generations a
  model may consume inside a reset window; an exhausted server drops out of
  routing until the window rolls over — the "1M free tokens every 3 hours"
  safety valve for frontier models. Values parse forgivingly: tokens
  `1000000` / `1M` / `1,000,000` / `2.5M` / `500K`; reset `30s` / `5m` /
  `12h` / `3d` / `1w` / `12:00:00` (= 12 h). Counts are real (streaming
  included); reset is a rolling window. Configure via GUI → Settings → LLM
  Servers → **budget**, KAI `BUDGET SERVER <sid> SET max_tokens 1M reset
  3h`, or `GET/POST /api/servers/budget/{sid}`. (`kaisen/budget.py`, wired
  into `Server.record`/`acquire` so routing skips exhausted servers.)
- **Sticky topbar.** The `.topbar` (brand + nav + status pill) now stays
  pinned while scrolling — before, the bar scrolled away while the logo and
  status pill stayed fixed, leaving them floating alone over content.
  Brand/status are anchored to the sticky bar (absolute, not fixed); the
  expanded status dropdown stays fixed so it floats above everything.
- **KAI `MODELCHECK [<sid>]`** — verify a server's STREAMING path (what a
  generation + the GUI live view use): first-token latency, whether content
  reaches the stream, prefill tps. Catches "generates in the model log but
  the KAISEN chat stays empty" (`POST /api/servers/modelcheck/{sid}`).
- **`LOGS [pid] [lines <n>] [grep <text>]`** KAI command + `GET
  /api/engine/logs` — engine log lines with filters.

### Fixed

- **Live-session prefill visibility.** A session that has started but not
  yet received a token reports `prefill: true` (with elapsed wait), so the
  live view shows "prefilling" instead of an empty box during a slow
  single-slot prefill.
- **Concurrency capped at real slot count.** Config `max_concurrent` can
  over-subscribe a single-slot server (queuing N generations invisibly
  behind one slot); the effective capacity is now capped at the detected
  `/slots` count, learned at health/probe time.
- **Thinking-model streaming regression test** — the engine's streaming path
  returns clean code (reasoning stripped) for Qwen3/DeepSeek-R1 `…`
  replies.

## [KAISEN 0.1.8-alpha (multi-model)] — 2026-09-09

Every instruct model works on a raw `/completion` server — not just gpt-oss.

### Added

- **Native framing for every instruct model.** KAISEN now opens each raw
  `/completion` prompt in THAT server's resolved `chat_template` (gptoss,
  qwen/chatml, llama3, gemma, mistral, deepseek, …) — previously only
  gpt-oss got native framing. A bare prompt degrades instruct models:
  gpt-oss emits erratic continuations, Qwen3 emits **nothing** (2
  whitespace tokens on a raw code-gen prompt — verified live). Roll-your-own
  multi-turn loops (deepwork, project agent) pass `templated=True` so their
  own continuation format is preserved; `chat_template: "none"` keeps
  historical raw behavior.
- **Qwen3/DeepSeek-R1 think-block stripped.** `strip_reasoning` now also
  drops the `<think>...</think>` reasoning block (not just gpt-oss's
  `<|channel|>final` marker). An unclosed `<think>` (budget exhausted
  mid-thought) yields nothing usable → `""`. Qwen3 via KAISEN now returns
  clean code with no reasoning in the captured reply (verified live).
- **KAI `LOGS` command** — `LOGS [pid] [lines <n>] [grep <text>]` returns
  recent engine log lines, backed by `GET /api/engine/logs` (reads the
  engine's in-memory `_last_log` deque, filters by lines/grep). HELP +
  aliases (`LOG`/`TAIL`).

### Changed

- `docs/MODELS.md` / README: the "other models keep raw prompts" claim is
  gone — every instruct model is framed natively; `chat_template: "none"`
  opts out.

## [KAISEN 0.1.8-alpha (follow-up)] — 2026-09-09

Second-pass fixes from the review of the gpt-oss work.

### Fixed

- **Add-server modal no longer pre-fills `{"temperature": 0.7}`** — the
  Params field is now blank by default (blank = server/model defaults),
  with an example in the placeholder. This was the actual source of the
  "base temperature 0.7" impression from the user discussion.
- **Trailing `<|end|>` token dropped from captured gpt-oss replies.**
  The model closes its turn with `<|end|>` after the final answer; left in
  place, marker-scanning consumers (DeepworkAgent's CoT cut) truncated a
  clean reply to nothing. `strip_reasoning` now removes it — captured
  answers contain no `<token>` markup at all (verified live).

### Changed

- `docs/MODELS.md`: GPT-OSS row now documents hybrid reasoning, native
  framing, channel stripping, and the `reasoning_effort` knob; the `auto`
  bullet reflects init-time resolution.
- README KAI notes: gpt-oss thinks on KAI turns too — set
  `reasoning_effort: "low"` for snappy command turns.

## [KAISEN 0.1.8-alpha] — 2026-09-09

gpt-oss reasons where it should, and the inference knobs are yours.

### Added

- **Native gpt-oss framing + reasoning-channel stripping.** Raw prompts
  sent to a llama.cpp `/completion` endpoint are now opened in gpt-oss's
  native channel format (`chat_template: auto` → `gptoss`; other models
  and explicit `"none"` keep the historical raw behavior — verified
  against a live b10402 box that gpt-oss degrades on bare prompts). The
  model's analysis channel is stripped from the captured answer (everything
  up to the last `<|channel|>final<|message|>` marker), so thinking never
  pollutes extracted code; live token streaming still shows the reasoning.
  `params.reasoning_effort` (`low`/`medium`/`high`) passes straight through
  per request — document the quality trade-offs in MANUAL §13 "Tuning
  inference quality".
- **First-run wizard: optional inference params.** The onboarding model
  form no longer bakes `{"temperature": 0.6}` into every new server; a new
  *Inference params (JSON)* field may stay blank, which means "the
  server/model's own defaults" (gpt-oss: temperature 0.65 per its model
  file). The Add-server modal already accepted free-form JSON.
- **Pipeline flowchart** (mermaid) in MANUAL §6 — prompt → routing →
  extraction → guardrails → build/verify/score → autofix ladder → LLM
  repair → scoring → champion, with every failure exit labelled.
- **MANUAL §13 "Tuning inference quality"** — temperature semantics (blank
  = server default), reasoning_effort guidance, and the n_predict budget
  note for thinking tokens.

### Fixed

- README autofix ladder now lists all five stages (per-compiler nudges
  were missing) and states LLM repair correctly: up to `llm_repair_max`
  (default 3) passes per generation, **on by default** — a failed build
  gets the candidate source plus the compiler error fed back to the model
  unless `autofix.llm_repair: false`. MANUAL §7 carries the same note.
- `kaisen/__init__.py` version drift (0.1.5 → 0.1.8, matching the
  changelog).

## [KAISEN 0.1.6-alpha] — 2026-09-06

The factory speaks every language in the registry, and every project's
timeouts are measured, not guessed.

### Added

- **Factory covers all 23 registry languages.** Baselines now exist for
  C, C++, CUDA, Python, Java, JavaScript, TypeScript, C#, Go, Rust,
  Kotlin, Swift, PHP, Ruby, R, Zig, Scala, Dart, Haskell, Lua, Perl,
  Shell and D. C++/CUDA derive from the C baselines through a verified
  transform layer (void* casts for the C++ frontend — any transform bug
  fails the self-check sweep and never ships); JavaScript, Perl and
  Shell are hand-written and proven by the same gate on this machine;
  the remaining languages are hand-written against the C reference.
  Build scripts for all 23 probe the registry's toolchain candidates in
  order (compiled: first compiler found wins; interpreted: shebang is
  insured with the first interpreter found; JVM/.NET/TS/Zig emit a small
  wrapper so the artifact contract — runs directly with argv — holds
  uniformly). `create_all` now PREFLIGHTS each language: missing
  toolchain ⇒ skip row reported as `NO TOOLCHAIN (skipped): …`, never a
  broken registration. A machine that has the toolchain provisions and
  proves those languages itself.
- **Empirical per-language timeouts, user-overridable.** Every baseline
  is built and run on its full workload before registration (the
  self-check IS the trial run — `check_project` now respects each
  project's own step timeouts instead of a blanket 1200 s). Measured
  worst cases feed `factory.LANG_BUILD_TIMEOUT` / `LANG_CASE_TIMEOUT`;
  policy caps: **2 min max compile, 10 min max execution** per project.
  Dictate the expected times via KAI (`FACTORY … BUILD_TIMEOUT <s>
  CASE_TIMEOUT <s>`) or GUI → Settings → *Factory timeouts*
  (`config.json` `factory.build_timeout` / `factory.case_timeout`,
  null = per-language empirical default). Slow-interpreted projects get
  scaled workloads/domains from the same measurements (shell: C-scale
  workloads measured at 478 s total → scaled to ~80 s; fuzz domains for
  prime-count/fib-mod/sum-range capped so a full 200-case gate stays
  inside the verify timeout).

### Fixed

- Workload registry: explicit workloads named only c/rust/go/python —
  every other language now inherits the C scale, so `make_project` can
  no longer KeyError on a new language.


## [KAISEN 0.1.7-alpha] — 2026-09-06

Auto-fix speaks every compiler's language, no LLM turn needed.

### Added

- **Per-compiler nudge backends (no LLM turn).** `kaisen/autofix/` is
  now a package with one module per language, each parsing THAT
  compiler's own diagnostics and doing exactly what it suggests — the
  same idea as the gcc fixer (parse "did you forget to include…?" /
  "did you mean…?"), but for every toolchain:
  - **rustc** — `expected \`;\` … add \`;\` here` (append `;`),
    `unclosed delimiter` (close the block), `cannot find module or
    crate \`fmt\`` → `use std::fmt;`.
  - **go** — `unexpected EOF, expected }` (close the block),
    `undefined: fmt` → `import "fmt"` (only known stdlib packages).
  - **bash/sh** — `unexpected EOF while looking for matching \`)\``
    (close the paren), `unexpected token \`fi\`` / `\`done\`` (insert
    the missing `then` / `do`).
  - **node (JavaScript/TypeScript)** — `Unexpected end of input`
    (close the block).
  - **python (ast)** — `expected ':'` (append it), `expected an
    indented block` (insert `pass`).
  - **perl** — `syntax error` with an unbalanced delimiter (close it).
  Every fix is error-driven: one fix per turn, rebuilt after each,
  never re-applied, and reverted if it breaks a build that previously
  succeeded (`autofix_nudge`, `kaisen/autofix/engine.py`). The default
  `resolve_mode` now maps every language with a backend to `"nudge"`;
  the C family keeps the gcc fixer and Python keeps the linter fixer.
- **One-change diff guard (P1.13).** `data.max_changed_lines: N` counts
  how many lines a candidate touches vs the champion and rejects any that
  exceed N (outcome `diff_violation`) before the pipeline — a guardrail,
  not a prompt, so "change ONE value" is enforceable. Line-level
  (`difflib`); absent/0 = off.
- **The factory self-check found and we fixed three real baseline bugs**
  (in `kaisen/factory.py`): `reverse-str-perl` (unparenthesized
  `reverse` swallowed the newline argument), `levenshtein-perl` (lexical
  `$a`/`$b` shadowed sort's special vars → broken min), and
  `caesar-shift-shell` (the non-POSIX `%c` form quoted the numeric
  string; now octal escapes). All three now pass the gate.

### Fixed

- `kaisen/autofix` restructured from a single `autofix.py` module into a
  package (`c_family.py` + one module per language); the public API
  (`autofix_build`, `parse_hints`, `resolve_mode`, `apply_fix`, …) is
  re-exported unchanged, so the pipeline and existing tests were
  updated only where the behavior intentionally changed (non-C/Python
  languages that had no fixer now get one).


## [KAISEN 0.1.8-alpha] — 2026-09-06

Every factory baseline is now proven on a real toolchain, and toolchain
availability is visible per OS.

### Added

- **All 23 registry languages now have verified baselines.** The factory
  test (`test_registry_is_complete`) enforces a baseline + workload for
  every (algorithm, language) pair. The ten languages that previously
  relied on "the self-check gate on a capable machine" now have
  hand-written, toolchain-verified baselines in `kaisen/factory_langs.py`
  (Java, TypeScript, C#, Kotlin, Swift, Zig, Scala, Dart, Haskell, D —
  25 algorithms each). Every baseline was compiled and run against the
  trusted Python reference on this machine; `tools/verify_factory_baselines.py`
  is the reusable per-(algo,lang) gate for any baseline module.
- **OS-aware toolchain status.** `kaisen/languages.py` learns the running
  OS family and reports, per language, whether the compiler/interpreter is
  present (PATH + the OS's standard install dirs), which binary, and the
  exact install command for that platform (apt/snap on Linux, brew on
  macOS, winget on Windows). Surfaced everywhere:
  - KAI `TOOLCHAINS` — a per-language table of OK / MISSING + install hint;
  - GUI → Settings → **Toolchains** tab (`GET /api/toolchains`);
  - `toolchain_status`/`toolchain_status_all`/`install_hint` in Python.
- **Per-compiler build contracts fixed** (found by verifying the new
  baselines): the Java build script's `javap` now uses the class basename
  (the absolute path broke entry-point discovery), and Kotlin uses `-d`
  (not `-o`) for the output jar.

### Fixed

- Interpreted-language check now reports status for python/javascript/php/
  ruby/r/lua/perl/shell interpreters too (compiled used to be the only
  probed kind).


## [KAISEN 0.1.5-alpha] — 2026-09-06

Campaign release: the "fast but wrong" hole is closed with a differential
fuzz gate, and long multi-project campaigns get first-class tooling —
factory, driver, bug capture, triage.

The campaign driver adopts newly registered projects live (no restart),
  and `FACTORY ... FORCE` re-provisions existing projects so factory fixes
  reach already-registered specs.

### Added

- **Fuzz gate — correctness on seeded cases, not just fixed tests.** A
  verify step that checks five hard-coded inputs can be fooled by a
  candidate that is right on the test slice and wrong everywhere else.
  New `kaisen/fuzzlib.py` generates SEEDED case sets per problem family
  (`int`, `pair_int`, `str`): boundary values (0/1, powers of two ±1,
  domain edges, empty string) plus seeded randoms — same seed gives
  identical cases forever, so a failing case is reproducible exactly.
  Projects carry `fuzz_cases.json` (inputs + reference outputs computed
  from a trusted reference at creation time); the shared verify step
  `harness/fuzz_verify.py` replays every case against the candidate and
  fails on the first mismatch with a machine-readable diagnostic
  (`FUZZ MISMATCH case=42 tag=rand:7 input=[...] expected='...' got='...'`,
  plus `FUZZ CRASH` / `FUZZ TIMEOUT`). Compare modes: `exact` (default),
  `sorted_lines`, `float_last` (relative tolerance 1e-6, understands
  `key=value` metric tokens).
- **Project factory — algorithm × language campaigns in one command.**
  `kaisen/factory.py` + KAI `FACTORY [ALGOS a,b] [LANGS c,python,rust,go]
  [CASES n]`: 25 algorithm families × 4 languages = 100 projects. The
  original ten (prime counting, popcount, GCD, Fibonacci mod, divisor
  count, Collatz stopping time, range sum, string reverse, palindrome
  check, run-length encoding) are joined by fifteen from integer math
  (primality, integer sqrt, digital root, trailing zero bits, total prime
  factors, nth prime, happy-number steps, modular exponentiation), strings
  (Levenshtein distance, LCS length, longest palindromic substring, KMP
  prefix function, Caesar shift) and lists (maximum subarray sum, count
  inversions). Every project ships a naive baseline, its own
  build/fuzz/score harness and a seeded fuzz gate; three new input
  families (`pair_str`, `triple_int`, `intlist`) feed the string-pair,
  triple-argument and list problems. Before registration the factory
  PROVES each project works: baseline must build, pass its full fuzz gate
  against the reference, and score — broken combinations are reported,
  never shipped. `FACTORY ... FORCE` re-provisions projects that are
  already registered (delete + recreate), so a factory fix — new contract
  text, repaired baseline — reaches the live pool without manual surgery.
  Per-language fuzz-domain overrides let slow-baseline languages scale
  their domain down (prime-count-python: n=10^6 costs ~30-90s per naive
  case; its projects now fuzz to n=2×10^4, self-check dropped 244s → 7s).
- **Campaign driver — resumable multi-project runs.** `kaisen/campaign.py`
  (CLI: `python3 -m kaisen.campaign [TARGET n] [PARALLEL k] [POLL s] |
  STATUS | STOP`) runs every pool project to N generations each, filling
  at most K engine slots at a time. State lives in `campaign.json` with a
  per-project history anchor: crash or restart mid-campaign and it resumes
  exactly where it left off (engines persist via `engine_pool.json`;
  progress before the crash counts, nothing double-counts). Semantics
  match `RUN <n>`: only SCORED generations (fitness measured) count
  toward the target — failed attempts never burn budget. The driver also
  adopts projects registered in the pool mid-campaign (live `FACTORY`
  scale-ups need no restart), and if a project's history shrinks below its
  anchor (re-provisioning wiped its runs) it re-anchors at the new start
  instead of replaying ghost generations.
- **Bug capture + triage.** Every stage failure of a running campaign
  project is appended to `campaign_bugs.jsonl` (exactly once per
  generation — anchored, restart-safe). `python3 -m kaisen.triage` groups
  rows by (project, outcome, signature), separates expected candidate
  noise (a generation's code wrong on one fuzz input) from HARNESS
  SUSPECTS (same stage failing the same way ≥3 generations in a row —
  toolchain/spec problems the LLM cannot fix), and prints a reproduction
  pointer (`runs/gen_NNNN/` + the exact failing input) for every group.
- **C builds now reject implicit function declarations**: the C build
  script compiles with `-Werror=implicit-function-declaration`. This
  class of bug was silent before: a missing `#include <stdlib.h>` makes
  gcc assume `atol`/`strtoul` return `int`, truncating 64-bit results —
  the digital-root baseline returned garbage for n = 10^15 while passing
  every small case. All fourteen affected baselines now include
  `<stdlib.h>`, and the build fails fast with a clear message instead of
  shipping a binary that lies about half its inputs.

### Fixed

- **FACTORY shipped empty harnesses (silent)**: the registration payload
  put bundled `files` at the top level of the POST body, but the server
  reads them from `spec["files"]` — so 40 projects registered with zero
  files on disk and every build failed with "No such file or directory".
  The triage tool caught it on the first live campaign run (harness-
  suspect heuristic); payload now nests files in the spec, surfaces
  server warnings, and a regression test pins the shape.
- **Campaign budget burned on failures (silent)**: driver progress counted
  every iteration-history entry, but failed generations land there too —
  four projects failing every generation hit "50/50" in ten minutes and
  were marked done with zero champions. Progress now counts SCORED
  generations only (fitness measured), matching `RUN <n>` semantics; a
  regression test pins it (an all-failing project must keep running).
- **Factory prompts never said where the input comes from**: the contract
  text described the task but not the I/O protocol, so candidates
  defaulted to stdin while the fuzz gate passes argv — whole projects
  failed every generation with empty output and an empty champion block
  to learn from. Every factory project now carries an explicit
  `I/O PROTOCOL` (argv[1..], per-language pointers, "NEVER read from
  stdin") plus a concrete example computed from the reference, in
  `data.contract_text`; the collatz goal text also dropped a misleading
  "memoization is the main win" hint.
- **Triage re-flagged fixed bugs forever**: the harness-suspect heuristic
  ran over all history, so an already-fixed failure kept shouting in
  every report. Suspects are now freshness-gated (last row ≤15 min) and
  every group shows "last seen Nm ago".


## [0.1.4-alpha] — 2026-09-06

LLM-layer resilience release: a slow or crashing llama.cpp box no longer
silently eats generations, and the GUI config save no longer corrupts
`config.json`.

### Fixed

- **GUI config save wiped fields the panel doesn't show**: `PUT /api/config`
  replaced whole sections, so every "Save" from Settings dropped
  `llm.retry_backoff`, `llm.routing`, `llm.allowlists` and — worst —
  `server.api_key` (the dashboard password silently stopped working after
  the next save). Sections now deep-merge: nested dicts merge key-by-key,
  scalars and lists replace.
- **Generations discarded while the server was still working** (field
  report: "generations don't get counted most of the time"): client-side
  deadline *guesses* — a before-first-token timeout scaled from prompt
  size and measured prefill speed, plus a total stream budget — killed
  slow-but-alive generations on slow or loaded boxes; the model was still
  chewing the prompt when its own output got thrown away. The policy is
  now simple and honest: **by default there is NO before-first-token
  limit** — KAISEN waits as long as prefill needs and accepts the output
  whenever it arrives; a stream that keeps producing tokens is never cut
  off by a total time budget. The only remaining time policy is
  `nodata_timeout` BETWEEN tokens, so a genuinely stalled decode still
  fails fast. Opt-in protection: new `llm.first_token_timeout` (default
  0 = no limit) hard-fails when no token arrives within N seconds — for
  llama.cpp the error says whether `/slots` showed visible work. Exposed
  in Settings as "First-token timeout".
- **Server death mid-stream misclassified**: a llama.cpp crash while
  streaming (OOM is common with several instances on one GPU) surfaced as
  a generic failure. The error now carries kind `stream`, the endpoint is
  marked offline + banned, and the request retries elsewhere.
- **401/403 marked the server offline**: an auth error means the server
  ANSWERED — marking it offline hid the real problem (wrong key) behind a
  "server down" state. Kind `auth` now keeps the server online with a
  longer ban; the operator sees the key problem, not a phantom outage.
- **Every engine re-hammered dead servers**: reachability was tracked per
  orchestrator instance. Health is now one shared record per endpoint for
  the whole process — every engine/pipeline sees one truth and only the
  re-probe loop touches an offline server.
- **Pool concentration: N identical boxes, only box #1 ever called**: the
  cost-first sort keyed on (tier, priority, inflight) — for identical
  servers with sequential calls that's always (equal, equal, 0), so a
  stable sort returned the first server in config order forever. A shared
  round-robin cursor now rotates among equals (tier and priority still
  win outright; load breaks remaining ties). Field-verified: three boxes
  went from 100%/0%/0% to an even split.

### Added

- **Error-kind classification** (`connection` / `stream` / `auth` /
  `http` / `timeout` / `cancelled`) driving the orchestrator reaction:
  `connection`/`stream` → offline + ban; `auth` → online, longer ban;
  `timeout`/`http` → ban only, never offline. Cancellation is its own kind
  and never penalizes the server.
- **Background re-probe of offline servers**: every offline-but-active
  server is re-probed every `llm.reprobe_interval` seconds (default 30;
  `GET /health` for llama.cpp, `GET /models` for OpenAI-type). Restart the
  crashed instance and it rejoins the pool on its own — no GUI toggle —
  with a `[KAISEN] LLM server … back online` log line. `0` disables.
- **`llm.nodata_timeout`** (default 120 s): max silence BETWEEN tokens —
  a stalled decode fails after this long. Exposed in Settings as
  "No-data timeout".
- **Prefill visibility**: per-server `last_ttft` (time to first token) and
  `prefill_tps` in the LLM server panel — "how long does filling take on
  this box" is visible instead of guessed.
- **Resilience regression suite** (`tests/test_llm_resilience.py`, 13
  tests): no-limit-by-default and flat opt-in cap, long-prefill survival
  with unhelpful `/slots` telemetry, hard-cap failure kind, streaming
  completion while tokens flow, mid-stream drop kind, error-kind
  classification, shared health across orchestrators, re-probe recovery
  + loop idempotency, cancel-during-silence latency, and the config
  deep-merge contract.
- **`install.sh`**: one-command setup (venv + deps) that works on PEP-668
  "externally managed" Pythons (Ubuntu 24.04/26.04, Debian 12+, Fedora).

Field-verified against three live llama.cpp instances (one healthy, two
crashed): SMOKE passed, 13 generations evolved a C prime-counter from
134.7 ms to 1.3 ms (100×) while routing worked around both dead servers;
their offline state was recorded without error spam and the re-probe loop
kept watching for their return.

## [0.1.3-alpha] — 2026-09-04

Windows compatibility release + field-crash fixes from user reports.

### Fixed

- **Suggest crash on gate failure** (`UnboundLocalError: notes`): when a
  validation gate failed BEFORE the smoke run, the repair section read
  `stage`/`notes`/`reason`, which only exist after a smoke run — the whole
  suggest flow died (hit in the wild with gpt-oss replies carrying
  `<|channel|>` reasoning tokens that trip JSON extraction). The variables
  now default before the branch; gate errors become the repair feedback.
- **charmap codec crash on non-ASCII model output**: lint/autofix wrote
  candidate code to temp files without an explicit encoding — under a
  non-UTF-8 locale (cp1252 "charmap" on Windows) characters like U+202F
  (narrow no-break space, which gpt-oss emits) raised
  `UnicodeEncodeError` ("Suggest failed: 'charmap' codec can't encode
  character '\u202f'"). All temp-file writes are now explicit UTF-8 and all
  toolchain output decodes as UTF-8 with replacement.
- **400 spam in the dashboard console**: with no engine running,
  `/api/active`, `/api/state` and `/api/workers` (polled every 2 s) — plus
  `/api/llm/live`, `/api/model/status`, `/api/debug/logs` — returned 400.
  No-engine is a NORMAL state: all read endpoints now answer 200 with
  `no_engine: true` / empty payloads, and the frontend treats it as the
  welcome state instead of an error.
- **Demo build crash when no C compiler is installed** (field report: box
  without gcc — every generation `build_fail` with a raw
  `FileNotFoundError` traceback instead of a readable error): the demo
  harness now resolves the compiler as `gcc → cc → clang`, and a missing
  toolchain exits with an OS-specific install hint (Miniconda/MSYS2 on
  Windows) instead of an unhandled traceback.
- **Toolchain preflight at engine start**: a compiled-language project no
  longer starts when its toolchain is absent from PATH — the engine stays
  stopped and surfaces the actionable error (dashboard fleet row + modal
  on play/resume) instead of burning generations of build_fail.
- **Garbled generation-list rows when details contain quotes** (every
  Python traceback does: `File "…"`): `escapeHtml` now escapes quotes too,
  so `title="…"` attributes can no longer break out of the markup.
- **Windows artifact naming**: MinGW gcc appends `.exe` to extensionless
  `-o` targets; on Windows the `{artifact}` token for compiled projects
  now carries the suffix so build, verify and score agree on one file.

### Added

- **Windows process supervision**: `run_subprocess` no longer drains pipes
  with `select()` (POSIX-only — on Windows it deadlocks as soon as a child
  fills the 64 KiB pipe buffer); each pipe now has its own reader thread on
  every OS. Live-telemetry (`KAISEN_PROGRESS`) parsing is unchanged.
- **Cross-platform tree kill**: timeouts, aborts and worker kills now take
  down the whole harness + candidate tree (`taskkill /T` with psutil
  fallback on Windows, process-group SIGKILL elsewhere).
- **Windows path recognition in guardrails**: drive paths (`C:\…`) are
  recognized as absolute on every OS (in-project trusted, out-of-project
  denied — never falling through to the bare-launcher allowlist), and
  relative programs with separators resolve against the PROJECT dir at scan
  time, matching execution-time resolution.
- `.py` step programs run through the active interpreter on non-POSIX hosts;
  the custom-fixer invocation uses `sys.executable` (not hardcoded
  `python3`); the file-open endpoint falls back to `os.startfile`.
- Regression tests: `tests/test_windows_compat.py` — no-deadlock drain of
  >64 KiB output, tree kill on timeout, guardrail path policy (POSIX +
  Windows drive paths), the suggest gate-failure path, and U+202F linting
  under a forced ASCII locale.

### Changed

- Frontend `loadActive`/`fetchState` handle the clean no-engine response
  (welcome picker + config-based server registry pre-launch).

## [0.1.2-alpha] — 2026-08-19

Second field-test release: the operator loop made first-class, budget and
staleness correctness, and opt-in diversity — all from real campaign pain.

### Added

- **`SCORE <path> [ON <pid>]`** (KAI + `POST /api/projects/{pid}/score`):
  score any file through the project's full build+verify+score pipeline with
  no engine and no run — the operator writes a candidate, the harness scores
  it, the result lands as an audit copy + `result.json` under `runs/score_*`.
- **`FUZZY <n> [ON <pid>]`** (KAI + `POST /api/engine/fuzzy`): opt-in prompt
  diversity — each generation is seeded with a random one of the top N scored
  iterations instead of the champion, and the prompt also carries the mule's
  own last 10 scored outcomes with delta vs champion.  Default off; runtime
  only (resets on restart).
- **`BUDGET`** KAI command: in-flight run's budget at a glance (scored so far
  vs target + time remaining).
- **`RUN ALL [FOR <secs>] [WITH <k>]`**: start every pool member at once —
  same budget and pipeline count each, multi-engine orchestration as opt-in
  only (single-project `RUN` unchanged).
- **Pool utilization line**: `STATUS` now shows `LLM PIPELINES x/y (z in
  flight)` so a glance tells you how much of your active LLM capacity is
  actually in use (GUI unchanged — the server list already carries it).
- **Engine crash recovery**: the running pool is persisted to
  `engine_pool.json` (gitignored) on start/stop/multi changes; the next
  daemon boot restores it — a restart no longer silently kills in-flight
  runs.  Only real projects restore; temp/ is wiped at startup.
- **Two-stage scoring** (`stage: screen|confirm` on score steps): the
  confirm metric — not the noisy screen — is what selects the champion;
  early-abort never kills a confirm benchmark.  GUI-safe (spec field passes
  through untouched).
- **Smoke outcomes persisted** to `projects/<id>/smoke_results.json` (capped
  history) and the daemon log — a smoke that outlives the HTTP read timeout
  is still readable afterwards.
- **Per-generation diff summary**: `runs/gen_NNNN/diff.json` with line-level
  diff counts against `data.baseline_source` for every scored generation.
- **Valid-rate telemetry**: STATUS/API show rolling valid-rate and per-outcome
  counts — a toxic run is visible at a glance.
- **Baseline drift guard**: `data.baseline_source` hash recorded in state.json;
  a change since the last run is logged loudly (outcome
  `baseline_source_changed`) instead of scoring against a stale reference.
- **Retention policy**: opt-in `engine.retention {enabled, keep_last,
  keep_best}` prunes old `runs/gen_*` dirs (champion + in-flight always kept).
- **Autofix knobs declarative**: project spec `engine.autofix {tries, repair}`
  participates in the cap chain (KAI override > spec > config) and STATUS
  shows the active policy.
- **Opt-in build caching**: `engine.build_cache: true` routes the build
  step through a per-project ccache masquerade (`CCACHE_DIR` =
  `projects/<id>/.kaisen_cache`), reusing unchanged translation units across
  generations — roughly an order of magnitude fewer compile seconds per
  generation.  caches the compile (`-c`) phase (links are not cached by
  ccache — by design); the bundled demo harness now splits compile+link so it
  benefits out of the box.  Requires ccache on PATH; falls back to uncached
  builds with a warning.  Off by default (`cache: false` escape for
  non-deterministic toolchains).  Validated with a real ccache direct-hit
  test and a one-shot fallback test.

### Fixed

- **`RUN FOR <secs>` is a time budget only** — no longer also parses as
  "21600 generations".  `RUN <n>` = generations, `RUN FOR <secs>` = time,
  both = whichever comes first.  Paused time no longer burns the wall-clock
  budget (WAIT slides the deadline while the engine is paused).
- **Spec changes apply at the next generation**: engine and workers re-read
  `project.json` per generation, so step timeout bumps take effect without a
  restart; STATUS shows the active `spec_revision`.
- **Engine switch uses the temp registry** for temp projects (workers resolve
  the project's own root) — temp runs no longer leak into `projects/`.

### Changed

- `engine.autofix` and `engine.retention` are validated spec fields;
  `smoke_results.json` and `runs/score_*` audit dirs are gitignored.

## [0.1.1-alpha] — 2026-08-18

First field-test release.  Feedback from real runs (compressor-speedup task,
multi-model runs) drove a focused hardening pass without changing the core
evolution loop.

### Added

- **D language support** (`kaisen/languages.py`): D is now a first-class
  project language — `.d` extension, `d` fence tag, `dlang`/`d2` aliases,
  goal detection, D-specific danger patterns, and D toolchain launchers
  (`dmd`, `ldc2`, `gdc`, `rdmd`) in the guardrail allowlist.
- **Multi-model chat templates** (`kaisen/llm.py`): the LLM layer is no longer
  gpt-oss-only.  Raw `/completion` servers now render the model's native chat
  format via a per-server `chat_template` option (`auto | gptoss | chatml |
  qwen | llama3 | llama2 | gemma | mistral | deepseek | none`), auto-inferred
  from the model name.  OpenAI-compatible servers continue to use server-side
  templating.  See `docs/MODELS.md` for the compatibility matrix.
- **Edit-scope guard** (`kaisen/engine.py`, `kaisen/prompts_blocks/scope.md`):
  projects can declare `data.edit_scope: ["fname", ...]` so candidates that
  touch functions outside the allowed set are rejected before the pipeline.
- **Quiet benchmarking / CPU affinity** (`kaisen/config.py`, `kaisen/workers.py`):
  `workers.affinity` pins worker processes to cores and `workers.quiet` runs
  them at lower priority, so score timings stop swinging with load on shared
  boxes.
- **KAI sticky sessions** (`kaisen/kai.py`, `kaisen/server.py`): `PROJECT` now
  persists across HTTP requests via the `kaisen_kai_sid` cookie (curl `-c/-b`).
- **KAI `ON <pid>` parsing**: `STOP`, `SMOKE`, `PAUSE`, `RESUME` now accept
  `ON <pid>` to target another pool member without re-selecting it.
- **Run-goal persistence**: KAI run budgets survive a daemon restart
  (`kai_runs.json`, gitignored).
- **Hardened danger scan** (`kaisen/skills.py`): added exec-family calls
  (`execl`, `execve`, `posix_spawn`, `fexecve`, …), sockets, `dlopen`, `shm_open`,
  `ptrace`, and write-mode `fopen`/`open` detection to the candidate guardrail.

### Fixed

- **User program is sacred in project creation** (`kaisen/suggest.py`): when a
  user attaches a program, the suggested project always evolves that exact
  file — repair rounds may rewrite harness scripts but can no longer replace
  the user's program with an AI variant.
- **Temp-project visibility** (`kaisen/kai.py`, `kaisen/server.py`): `BEST`
  and the new `GET /api/projects/{pid}/best` endpoint now resolve projects
  under `temp/` too, and `POST /api/engine/switch` uses the temp registry so
  a temp project's engine actually runs against its own files.  KAI can now
  fish a temp run's champion while it evolves instead of only reading
  `projects/`.
- `lang_from_ext` now accepts full filenames/paths (not just bare extensions);
  `lang_from_goal` normalizes punctuation so "in D," matches.
- `data.baseline_source` defaults to the correct language extension for every
  language (previously D/Python-correct only for a hardcoded pair).

### Changed

- `config.example.json` documents the new `workers.affinity` / `workers.quiet`
  options and per-server `chat_template`.

### Notes for field testers

- The dangerous-call check remains a tripwire, not containment: process
  timeouts, RSS caps, and the worker isolation are the real fence.  A
  statically-undetectable trick (e.g. taking a function pointer to `socket`
  instead of calling it) can still slip past — treat untrusted model output
  accordingly.
- Scoring is only as honest as the harness: a loose verify gate will accept
  a fast-but-wrong program as a real speedup.  Gate on real output hashes.
