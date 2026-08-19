# Changelog

All notable changes to KAISEN are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
