# Changelog

All notable changes to KAISEN are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
