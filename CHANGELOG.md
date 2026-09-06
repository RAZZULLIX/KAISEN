# Changelog

All notable changes to KAISEN are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
