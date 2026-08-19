# KAISEN

改善 AI システム — **squeeze every bit out of every watt.**

An agentic AI coding harness: describe your program in words, and a swarm of
local (or frontier) models evolves it through a guarded pipeline —
build → verify → score — while you watch every generation live.

**📖 Complete reference for humans and AI agents:
[`MANUAL.md`](MANUAL.md)** — every panel, every command, the spec
reference, routing, safety, and the full config. For the LLM-facing
protocol specifically: [`docs/KAI.md`](docs/KAI.md). Model
compatibility matrix: [`docs/MODELS.md`](docs/MODELS.md). Release
history: [`CHANGELOG.md`](CHANGELOG.md).


## The flow

1. **First run** — a setup wizard connects your model (local llama.cpp
   endpoint or any OpenAI-compatible API) and creates a project: one-click
   demo, AI agent setup (paste code + goal in words), or manual.
2. **The pipeline canvas** — node-editor-style connected nodes. Each node is
   an inline **python or shell script** (or a harness file) with args,
   timeouts, and per-metric parse rules. Click to edit, arrows show flow.
   **▶ Test pipeline** runs the real thing on the baseline in a temp copy.
   **✨ AI build my pipeline** designs + validates it for you to review.
3. **Evolution** — the engine feeds the champion to your models with
   tier-aware prompts (tiny/small/large), extracts their code, guardrails
   it, and scores every candidate for real. Linters (pyflakes/ruff,
   `bash -n`, `node --check`, gcc-family) auto-fix mechanical errors.
4. **⚡ Swarm** — parallel agents across all active servers: forge N drafts
   (each scored by the real pipeline), build N validated pipeline designs,
   or plan → execute → synthesize.
5. **🧠 Agent** — a multi-turn tool loop over a project: reads the spec,
   history, champion, lessons; runs the pipeline; edits the spec with
   validation. Every mutation snapshots first.
6. **⌘ Tell KAISEN…** (Ctrl+K) — the GUI flows with the LLM: natural
   language reconfigures appearance, project specs, metrics, and more, as
   one validated action at a time — always revertible.

## KAI — the LLM-facing API (optimization sidecar)

KAISEN is not only a human tool. **KAI** is its line-oriented protocol that
lets an LLM agent spawn KAISEN as a *sidecar*: hand over the
performance-critical part of a program, keep working on something else, and
read back the improved code later. KAISEN evolves **forever by default** —
stopping only when the agent says so (or a goal flag is set).

```text
$ python main.py --kai                # stdio server (spawns the dashboard if needed)
  BASELINE c                          # the hot function / kernel the AI owns
  static inline void hot_loop(...) { ... }
  END
  GOAL make this 2x faster without changing the output
  ACCEPT hot-loop-fast                 # review the spec JSON first, then instantiate
  RUN                                 # evolve in the background — forever by default
  WAIT 300                            # the AI works on the GUI meanwhile
  BEST                                # read the champion back into the project
  STOP
```

HTTP transport: `POST /kai` on the dashboard (text/plain in, text/plain out;
one command per line, `PROJECT <id>` inside the body for stateless clients).

Command reference (case-insensitive; `HELP` prints this):

```text
PROJECT <id>                set session project
STATUS                      engine + project status
SPEC                        pipeline steps, metrics, goal
RUN [<n>] [FOR <secs>] [ON <pid>]   start evolving NOW (background).
                            No goal = run FOREVER until STOP. <n> = stop
                            after n generations; FOR <secs> = time budget.
WAIT [<secs>]               block until the in-flight run finishes
PAUSE | RESUME | STOP       engine control
BEST                        champion source code
SMOKE                       run pipeline once on the baseline
BASELINE [lang]             stage starting code: lines until END
CANDIDATE [lang]            queue code into evolution: lines until END
SNAPSHOT [LIST|TAKE|RESTORE <id>] [ON <pid>]
SERVERS                     LLM servers + active set
GOAL <words...>             AI designs + validates a project (blocking)
ACCEPT <id>                 create the project from the last GOAL spec
CREATE <id> <spec-json>     create a project from a hand-written spec
```

Designed for small models: every reply starts with `OK` or `ERR`; command
words have loose aliases; leading `OK `, quotes, and punctuation are
tolerated on input; a session remembers the project, staged baseline, and
the in-flight run. `RUN` reports progress in *scored* generations (the
queue counter can run ahead of the workers).

Agent-writer notes: for local llama.cpp servers the raw `/completion`
endpoint has no chat-template reasoning wrapper — command turns are cheap
and direct. On reasoning OpenAI-compatible models, prefer
`reasoning_effort: "none"` (or `chat_template_kwargs: {"enable_thinking":
false}` on llama.cpp chat endpoints) for KAI tool-call turns, and never
prefix model output with `OK` — the server does that.

Full protocol reference: [`docs/KAI.md`](docs/KAI.md) — command table,
grammar, session semantics, and the reliability contract.

## Safety model

- **Per-language candidate guards**: process spawning, file deletion, and
  network egress are denied by default for every language (C-family,
  Python, shell, JS/TS, Java, Go, Rust, …). Inline pipeline nodes are
  scanned with the rule set of *their* script language.
- **Protected data**: declared data files are hashed before every stage;
  any modification fails the run.
- **Resource limits**: per-step timeouts + RSS memory limits; score
  early-abort kills provably-worse candidates mid-benchmark.
- **Revert**: every agent/config mutation snapshots the project (or the
  global config) first — Settings → Snapshots restores with one click.
- **Secrets**: API keys live in `secrets.json` (0600, gitignored), never in
  `config.json`; env vars (`KAISEN_SERVER_<ID>_API_KEY`) take precedence.


### Network exposure (read this before opening the port)

The dashboard, `/kai` and `/api` have **no authentication by default**:
the default bind is `127.0.0.1` — loopback only. Setting `server.host`
to `0.0.0.0` in `config.json` exposes the full control surface to your
LAN; do it only on a trusted network.

Optional server password: set `server.api_key` in `config.json` (the
environment variable `KAISEN_API_KEY` wins). When set, every page and
endpoint requires it — browsers get the standard Basic-auth password
prompt (any username, the key as password), API clients send
`Authorization: Bearer <key>`. Leave it empty for the open,
loopback-only default.

The global safety switch additionally requires BOTH `config.json`
`safety.global_off: true` AND the environment variable
`KAISEN_SAFETY_OFF=1` before any guardrail is relaxed — neither is ever
toggleable from the GUI.

### Autofix ladder

When a build fails, KAISEN repairs in four guarded stages:

1. deterministic compiler-hint fixes (missing includes, "did you mean",
   `_GNU_SOURCE`, SIMD-target pragmas, literal `\n`, avxintrin rewrite);
2. linter-backed fixes for Python candidates;
3. a project's own custom fixer script (`skills.autofix_build: "path"`);
4. **LLM last-resort repair** — one LLM rewrite with the real compiler
   error as feedback. The reply is danger-scanned and length-capped,
   only the candidate file may change, and the result re-runs the FULL
   pipeline (build + verify + score) before it can count. Once per
   generation, never on the user's baseline, and switchable off
   (`config.json` → `autofix.llm_repair: false`).

## Performance notes (honest framing)

KAISEN has evolved real kernels autonomously (llama.cpp experiments):
an AVX2 SiLU 1.6x faster than llama.cpp's own AVX2 implementation
(0.242 vs 0.389 ms, max relative error 0.19%, verified by dense sweep),
and an AVX2 GELU ~74x faster than the naive scalar path (max error
2.1% — above the 1% goal, disclosed here rather than hidden).

The end-to-end effect on gpt-oss-20b Q8_K inference is **zero**: decode
and prompt processing are ~90% Q8_K GEMM, and activation kernels are
single-digit percent of the trace (measured stock vs patched parity on
CPU and GPU). Real end-to-end wins on GEMM-bound models require
improving the GEMM itself — which is exactly the kind of target KAISEN
is built to grind. Judge results by measurement, not by headline
multipliers.

## Tests

```bash
pip install pytest
python -m pytest tests/
```

Covers the HTTP API + engine pool scoping, suggest gates, every
deterministic autofix rule, the KAI grammar, tier routing, chat templates,
the D language registry, temp-project best resolution, two-stage scoring,
budget semantics, and the LLM repair flow (206 tests, no network, temp
dirs only).

## Layout

```
kaisen/
  engine.py      evolution loop (producer → workers → champion)
  pipeline.py    build → verify* → score* runner (inline steps, autofix)
  swarm.py       parallel multi-agent coordinator (3 kinds)
  agent.py       JSON-action tool loop per project
  kai.py         KAI protocol — the LLM-facing API (stdio + /kai HTTP)
  suggest.py     guarded AI project/pipeline builder
  promptlib.py   tier-aware prompt library (the single source of truth)
  linters.py     local lint/autofix backends
  languages.py   23-language registry (extensions, fences, guard patterns)
  snapshots.py   project/config revert store
  ui_prefs.py    appearance standard + defaults
pages/           the dashboard (single-page, no build step)
projects/        one directory per project: project.json + harness + data
```

## Run

```bash
pip install -r requirements.txt   # aiohttp, requests, psutil (+ pyflakes, ruff recommended)
python main.py                    # dashboard on http://127.0.0.1:8080 (loopback only)
python main.py --project my-project --multi 2 --workers 4
```

Config lives in `config.json` (gitignored; see `config.example.json`).
