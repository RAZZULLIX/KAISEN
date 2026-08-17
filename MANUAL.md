# KAISEN — Complete Manual

This is the full reference for KAISEN, written for both humans and AI
agents. It covers every surface: the dashboard, projects, the pipeline,
the evolution engine, the KAI protocol, servers and routing, safety, and
configuration. For the KAI command-by-command reference see
[`docs/KAI.md`](docs/KAI.md).

---

## 1. What KAISEN is

KAISEN is a local-first framework that **evolves programs automatically**.
You describe a measurable goal ("make this kernel faster", "shrink this
file smaller"), give it a starting program, and it repeatedly:

1. asks an LLM for an improved candidate,
2. builds it,
3. verifies it,
4. scores it against the goal metrics,
5. keeps the champion, feeds it back as the next baseline.

Runs are endless by default, run several projects at once in the
background, and can be driven from the GUI, from the **KAI** text
protocol (designed for other AI agents), or from an AI agent inside the
GUI. Every generated artifact is validated by your own harness and by
hardcoded guardrails — KAISEN never trusts the model's word alone.

---

## 2. Core concepts

| Term | Meaning |
|---|---|
| **Project** | One evolution target: a spec (`project.json`) + harness programs + data. Lives in `projects/<id>/`. |
| **Spec** | The declarative project file: pipeline steps, metric schema, guardrails, prompts, skills. |
| **Harness** | YOUR programs (build/verify/score) that KAISEN runs to judge candidates. The harness is the ground truth. |
| **Candidate** | One LLM-generated version of the program (`runs/gen_XXXXXX/candidate.<ext>`). |
| **Generation** | One candidate + its full pipeline evaluation. |
| **Champion** | The best candidate so far (`best/program.<ext>`), used as the baseline for the next generation. |
| **Metric** | A number extracted from the score step's output (e.g. `time_ms`). Each metric has a direction (`lower`/`higher`) and a weight. |
| **Fitness** | The weighted composite of all metrics; the champion is the best fitness. |
| **Pipeline** | The per-candidate sequence: build → verify* → score*. |
| **Engine** | The background evolution loop for one project. Several engines run concurrently (the pool). |
| **Worker** | A process that executes pipelines; several workers evaluate candidates in parallel. |

---

## 3. Quick start

```bash
pip install -r requirements.txt     # aiohttp, requests, psutil (+ pyflakes, ruff recommended)
python main.py                      # dashboard on http://127.0.0.1:8080
```

First run opens a setup wizard: connect an LLM server (local llama.cpp
`/completion` endpoint or any OpenAI-compatible API), then create a
project — one-click demo, AI-agent setup ("paste code + describe the
goal in words"), or manual (write the spec yourself).

Run without the GUI (headless evolution):

```bash
python main.py --project my-project --multi 2 --workers 4
```

Drive KAISEN from another AI agent via the KAI protocol:

```bash
python main.py --kai
```

---

## 4. The dashboard (GUI)

Four views + a command palette:

### Dashboard
- **Status pill** — engine state, generation counter, best fitness, and
  "SYSTEM — N engines" when the pool is running.
- **ACTIVE ENGINES panel** — every engine in the pool: state dot, name,
  generation, best fitness, multi (parallel LLM pipelines), workers.
  Each row has **Select**, **Pause/Resume**, **Stop** — per project,
  without touching the others.
- **Worker cards** — per-worker live telemetry (the `live_fields` from
  the spec stream in real time).
- **System panel** — CPU/RAM/GPU metrics.

### Projects
- **Project cards** — create, open, delete; each shows its engine state
  and generation progress.
- **Pipeline canvas** — node-editor-style connected nodes. Each node is
  an inline **python or shell script** (or a harness file) with args,
  timeouts, and per-metric parse rules. Click to edit; arrows show the
  flow; **▶ Test pipeline** runs the real thing on the baseline in a
  temp copy.
- **Inject Custom Code** — queue a hand-written candidate as a
  generation (from the GUI or `CANDIDATE` in KAI).
- **Agent** — start a multi-turn AI agent over the project (see §14).

### Notes
- Free-form notes with colors, archive, reorder, comments, and
  similarity checks (the LLM warns when you write something very
  similar to an existing note).

### Settings
- **LLM Servers** — add/remove servers with type, URL, tier, priority,
  context window, smartness, $/Mtoken cost, concurrency, params,
  payload template; health-probe buttons; active-server checkboxes.
- **Autofix (per project)** — on/off/custom fixer script (§7).
- **Snapshots** — list/restore project and config snapshots (§15).
- **Prefs / appearance** — UI preferences.
- **Config agent** — describe what you want in words ("route tiny jobs
  to the 7B"); the LLM proposes config changes you approve.

### Tell KAISEN… (Ctrl+K / command palette)
Natural-language commands for the whole system: start/stop projects,
add servers, configure routing, run swarms — executed through the LLM
with validated tools.

---

## 5. Projects — anatomy and spec reference



```
projects/<id>/
  project.json   — the declarative spec
  harness/       — your build/verify/score programs
  prompts/       — optional modular prompt templates
  original.c     — the user's starting program (sacred baseline)
  data/          — protected test data (optional)
  runs/          — per-generation artifacts (runtime)
  best/          — champion artifacts (runtime)
  state.json     — engine state (runtime)
```

### TEMP root — quick runs that leave no trace

For one-off agent runs ("make this kernel faster, read BEST, move on"),
create the project with the **TEMP** flag: `GOAL <words> TEMP` /
`CREATE <id> TEMP <json>` in KAI, or `{"temp": true}` in the
`POST /api/projects` body. The project then lives entirely under the
**`temp/` root** — never under `projects/` — and everything it writes
(runs, champion, state) is **wiped automatically when the server closes
AND at the next startup** (crash-safe). The real setup — projects/,
config.json, your harnesses — is never touched by temp activity, and a
temp project can never shadow a real one with the same id (real wins).
The GET `/api/projects` list marks temp projects with `"temp": true`.
projects/<id>/
  project.json   — the declarative spec
  harness/       — your build/verify/score programs
  prompts/       — optional modular prompt templates
  original.c     — the user's starting program (sacred baseline)
  data/          — protected test data (optional)
  runs/          — per-generation artifacts (runtime)
  best/          — champion artifacts (runtime)
  state.json     — engine state (runtime)
```

### `project.json` reference

| Field | Meaning |
|---|---|
| `id` | `[a-z0-9_-]+`, unique |
| `name` | display name |
| `description` | free text |
| `language` | any of the 22 languages (§20) |
| `artifact_name` | output file name of the build step |
| `steps.build` | one build command: `program`, `args`, `timeout`, `memory_limit_mb` |
| `steps.verify` | list of verify commands (same shape); all must pass |
| `steps.score` | list of score commands; must emit parseable metrics |
| `metrics` | `{key: {direction: lower\|higher, weight: float, unit?: str, constraint?: float}}` — at least one required. A `constraint` is a HARD gate: violating it rejects the candidate outright (outcome `constraint_violated`) — no fitness weighting can compensate. Enforce "without changing the output" here, not in a prompt |
| `telemetry` | `{enabled, progress_token, live_fields}` — harness progress protocol (§6) |
| `engine` | `{workers, multi}` — startup sizing (project > config > 1/1) |
| `select.hysteresis` | champion replacement threshold; 1 = any improvement, 1.1 = must beat the champion by 10% (values < 1 are clamped to 1) |
| `guardrails` | `{enabled, allow_extra, deny_extra}` — extra command rules |
| `prompts` | `{generation_dir, goal, study, lesson}` — prompt templates |
| `skills` | `{analyze, dedup, deepwork, autofix_build, lessons}` — optional brain features (§7, §9) |
| `data` | `{protected_files: [...]}` — files hashed before every stage |
| `scores` | optional multi-score-type support (`types` + `active`) |
| `files` | (suggest-flow only) harness scripts + baseline bundled at CREATE time |

### Command placeholders

Harness commands use `{candidate}` (source path), `{artifact}` (build
output), `{project_dir}`, `{workdir}` (this generation's scratch dir).
They are substituted per generation.

---

## 6. The pipeline

Per candidate: **build → verify* → score***. Each step runs as its own
process with:

- a **timeout** (per step),
- an **RSS memory limit** (`memory_limit_mb`),
- a **guardrail check before execution** (single choke point, §19),
- **protected-data verification after every stage**: declared data files
  are hashed up front; any modification by any step fails the run.

Steps may be harness programs (executables, typically `python3` scripts
in `harness/`) or **inline scripts** written directly in the spec
(python or bash — the GUI canvas edits these).

### Metrics & parse rules

Score steps print numbers; the pipeline parses them. Each score step can
declare **parse rules** — patterns/JSON keys mapping output to metric
values. If no rules are declared, the pipeline tries to synthesize them
from the metric keys. Extraction is resilient to LLM-written harnesses:
balanced-brace scanning, last-occurrence matching, and unified output
lines all work. If no metric parses, the generation fails with
`no_metrics` — a broken harness can never produce a false champion.
### Telemetry (live fields)

Harness programs can stream progress while they run:

```
KAISEN_PROGRESS rounds=12 best_so_far=0.3
```

Lines matching `telemetry.progress_token` are parsed and streamed to the
worker cards in real time. Score early-abort uses them: a candidate
whose live value already cannot beat the champion is killed mid-run.

### Step semantics

- `build` — produces the artifact. One step.
- `verify` — correctness gates. Every step must pass (exit 0).
- `score` — measurement only. Steps must NOT modify anything; the
  verify prompt teaches harnesses this, and the metric is what the
  pipeline parses.

---

## 7. Autofix ladder

When a build fails, KAISEN repairs in four guarded stages:

1. **Deterministic compiler fixes** — parsed from the real error: missing
   includes ("did you forget…"), "did you mean" token fixes,
   `_GNU_SOURCE` for POSIX APIs, SIMD target-mismatch pragmas
   (`#pragma GCC target("avx2,fma")` / avx512), dropping `const` from
   static SIMD vector initializers, unescaping literal `\n`, rewriting
   `avxintrin.h` → `immintrin.h`. Applied one at a time, rebuilt after
   each; a fix that breaks a working build is reverted.
2. **Python linter fixes** — pyflakes/ruff-backed repair for Python
   candidates.
3. **Custom fixer** — a project's own script
   (`skills.autofix_build: "harness/fixer.py"`), called with
   `<candidate> <artifact> <project_dir> <workdir> -- <build cmd…>`.
4. **LLM last-resort repair** — when 1-3 are exhausted and the build
   still fails, up to `llm_repair_max` (default 3) LLM rewrite passes
   with the real compiler error as feedback. Hardcoded guards: the reply
   is danger-scanned and length-capped (30k chars), only the candidate
   file may change, and the repaired code re-runs the FULL pipeline
   (build+verify+score) before it can count. Never on the user's
   baseline. Global switch: `config.json` → `autofix.llm_repair`.

**Per-run compile-loop knobs (KAI)**: `AUTOFIX tries <n> repair <n|off>`
sets, for the session's project engine, how many deterministic autofix
turns before giving up (default 5) and how many LLM repair attempts
(default 3; `off`/0 = deterministic autofix only, then fail). Overrides
`config.json` (`autofix.max_tries`, `autofix.llm_repair_max`) for that
engine's runs; `autofix.llm_repair: false` is the global off switch.

Per-project control: `skills.autofix_build` = `true` (default), `false`,
or a custom fixer path. Non-C/Python languages surface their compiler
diagnostics through the build stderr without a default fixer.

---

## 8. The engine (evolution loop)

One engine per running project; several engines form the pool.

- **Producer threads** ask the LLM for the next candidate (prompt =
  project prompts + champion code + memory/lessons + tier-aware boost).
- **Workers** (processes) run pipelines in parallel; the queue is
  bounded (`workers.queue_size`).
- **Baseline** — the user's original program is evaluated first; it is
  SACRED: never rewritten, never repaired, always available as the
  comparison point.
- **Champion selection** — weighted composite fitness with hysteresis;
  strict improvements (or factor improvements) replace the champion.
- **Dedup** — semantic-hash duplicate candidates are skipped.
- **Multi** (`multi: k`) — k independent LLM pipelines evolve in
  parallel per project, sharing the champion.
- **Pause/Resume/Stop** — per project; pause drains in-flight work
  cleanly.
- **Workers** — add/remove/kill individually (GUI or API).
- **Prompt override** — replace the generation prompt mid-run without
  touching the spec.
- **Custom code** — hand-written candidates injected as generations
  (`Inject Custom Code`, `CANDIDATE` in KAI, or `POST /api/queue/custom_code`).
- **Outcomes** are recorded per generation in the iteration history:
  `ok` (new best), `valid`, `build_fail`, `verify_fail`, `no_metrics`,
  `no_code`, `llm_repair`, `protected_data_modified`, `guardrail_denied`
  — visible in the GUI and via KAI `WAIT`/`STATUS`.

---

## 9. Memory, lessons, deepwork

Optional brain features, all per project (`skills`):

- **analyze** — the pipeline's per-stage output is analyzed.
- **lessons** — after a new best, the LLM writes a lesson (what worked);
  it joins the generation prompt. Stored in `lessons.txt`.
- **deepwork** — periodic multi-turn agent sessions with tools over the
  results store (read, pandas query, file inspection); saves memos into
  `memos/` that later generations read. (pandas is optional — the query
  tool errors gracefully without it.)
- **memory** — history blob + keyword trends feed the generation prompt.

---

## 10. KAI — the LLM-facing protocol

KAI is the line-oriented protocol that lets **another AI agent** use
KAISEN as an optimization sidecar. Two transports, same grammar:

- stdio: `python3 main.py --kai`
- HTTP: `POST /kai` (text in, text out)

Key commands: `PROJECT`, `STATUS`, `SPEC`, `RUN [n] [FOR secs] [WITH k]
[ON pid]`, `WAIT`, `PAUSE/RESUME/STOP`, `BEST`, `SMOKE`, `BASELINE`/`END`,
`CANDIDATE`/`END`, `SNAPSHOT`, `SERVERS`, `GOAL`, `ACCEPT`, `CREATE`,
`ESTIMATE`, `FORGE`, `HELP`, `QUIT`.

Reliability contract: every reply starts `OK` or `ERR`; parsing is
deliberately tolerant (LLMs decorate commands with quotes, `CMD:`,
`OK?` prefixes — all accepted); engine operations are per-project; an
error never kills the session and always says what to do next.

Full grammar, alias table, and session semantics: [`docs/KAI.md`](docs/KAI.md).

Typical sidecar session:

```text
PROJECT md5-speed
BASELINE c
<code>
END
GOAL make this 2x faster without changing the output
ACCEPT hot-loop-fast
RUN WITH 3
WAIT 600
BEST
STOP
```

---

## 11. Building projects from words (GOAL / suggest)

`GOAL <words>` (KAI) or "AI build my pipeline" (GUI) runs the suggest
loop: a conversational multi-step flow that interviews you, writes the
baseline, designs the harness (driver step for library-style code with
no `main`), assembles the spec, and validates it for real before
anything is created:

- **structure validation** — every required field, step shapes,
  metric schema;
- **guardrail scan** — every pipeline command;
- **score probe** — the score step is executed against a fast
  stand-in candidate and must actually parse a metric;
- **smoke run** — the whole pipeline runs on the baseline in a temp
  copy with capped timeouts;
- **spec repair** — validation failures loop back to the LLM with the
  errors (REVIEW step: you approve the corrected spec).

The verify prompt requires **dense sweeps** (1000+ points across the
input range, max-error reporting) so a champion cannot pass on a
handful of lucky test points. Only then does `ACCEPT` instantiate the
project (data files are copied, never moved; the original stays
untouched).

---

## 12. Swarm

Parallel multi-agent jobs, visible in the GUI, cancellable, every output
validated by the real pipeline:

- **code_forge** — N independent improvement drafts (default 3, max 12),
  each scored by the project's own pipeline, ranked. KAI: `FORGE [n]
  [TIER t] [ON pid] [GOAL words]`.
- **pipeline** — N parallel project specs, each through the full
  suggest validation (structure + guardrails + smoke).
- **answer** — planner → parallel executors → synthesizer for research
  questions.

Reasoning is never passed between steps (token saving); prompts come
from the tier-aware prompt library; concurrency respects each server's
`max_concurrent`.

---

## 13. LLM servers & routing

Servers are managed live in Settings (persisted in `config.json`).

Each server carries: `type` (`llama` = raw `/completion`, or
OpenAI-compatible chat), `url`/`base_url`, `model`, `params`,
`max_concurrent`, timeouts, `payload_template`, `spawn_cmd`, and the
**routing profile**:

| Field | Meaning |
|---|---|
| `tier` | `tiny` / `small` / `large` |
| `priority` | tiebreak inside a tier (higher first) |
| `context_window` | the server's real context (informational) |
| `smartness` | 0-10 score (tier defaults: tiny 2, small 5, large 8) |
| `cost_in` / `cost_out` | $ per 1M tokens (local servers = $0) |

**Routing is cost-first**: every request picks the LOWEST tier that can
do the job, then priority, then free capacity. Busy servers fall
through so the pipeline never stalls; if no qualifying server is free,
it falls back to any usable server rather than deadlocking. Servers
have live state: inflight counters, bans with cooldown, one-shot
reachability probes, per-server stats (requests, failures, tps).

`ESTIMATE <in> [out]` (KAI) or the Servers panel shows per-server
time/cost for a call of that size before you commit.

Secrets: `KAISEN_SERVER_<ID>_API_KEY` (env, per server) or
`KAISEN_OPENAI_API_KEY` (fallback). Never written back to config.

---
## 13b. Model scoreboard — which model does which skill best

Every LLM call is attributed to a **(model, skill)** pair and scored:

- **attempts** — completed calls for that skill (plus accumulated $,
  estimated from the per-Mtoken prices);
- **one-shot** — first-try success with NO corrective loop: a
  generation that passed the pipeline without autofix/repair, a repair
  that fixed the build in one pass, a suggest that validated without
  repair rounds, an agent that completed its mission;
- **win** — the skill's end goal achieved: a generation that became the
  champion, a repair that unblocked a build, a suggest that produced a
  usable spec.

Skills: `generation`, `llm_repair`, `suggest`, `swarm`, `agent`,
`deepwork`, `lesson` — the scoreboard grows with real work. See it in
Settings → LLM Servers → **Model scoreboard**, or via KAI `MODELS
[skill]` / `GET /api/llm/modelstats`. Persisted in
`config.json.skill_stats.json` (gitignored).

Two consequences, both config-driven:

- **Per-skill allowlists** — `llm.allowlists = {"suggest":
  ["frontier-70b"], "llm_repair": ["tier:tiny", "tier:small"]}`. Entries
  are server ids or `tier:<tier>`; a skill with a list may ONLY use
  those models. "CREATE may use frontier models; autofix never" is one
  line.
- **Adaptive routing** — `llm.routing: "adaptive"` ranks each skill's
  eligible models by measured quality-per-dollar (one-shots and wins
  per $, smoothed); `"cost"` (default) keeps the tier-first policy.
  `min_tier` and allowlists always apply first — they are hard rules,
  adaptation only reorders within what is allowed.

In time the scoreboard answers the two questions that matter with many
models: what does what best — and what is better to just not use.

---

## 14. The project agent

"Tell KAISEN…" and the per-project Agent run a multi-turn tool loop
hints on malformed tool calls). Tools:

- `read_spec`, `read_history`, `read_champion`, `read_lesson`,
  `read_file` — inspect everything;
- `run_smoke` — run the real pipeline on the baseline;
- `update_spec` — edit the spec; guardrail-scanned and rejected with
  reasons when invalid.

Every mutation takes a snapshot first. A config-level variant
(config agent) proposes config changes the same way.

---

## 15. Snapshots

Every agent/config mutation snapshots the project (or the global
config) first. Settings → Snapshots: list by reason/date, restore with
one click. KAI: `SNAPSHOT LIST|TAKE|RESTORE <id> [ON <pid>]`. Stored in
`.kaisen_snapshots/` (gitignored, pruned automatically).

---

## 16. Notes

A project-wide scratchpad: color-coded notes, archive, drag-reorder,
comments, and an LLM similarity check that flags near-duplicates.
Backed by `notes.json` (gitignored).

---

## 17. Telegram & GitHub

- **Telegram** — new-best notifications (`🏆 NEW BEST`) with metric
  details, pinned messages, optional file upload. Env-first secrets:
  `KAISEN_TG_TOKEN`, `KAISEN_TG_CHAT_ID`.
- **GitHub upload** — per project (`github` spec block): the champion
  is uploaded to a repo/branch/path with a README report when a new
  best is found. Token via `KAISEN_GITHUB_TOKEN`.

---

## 18. Configuration reference

`config.json` (gitignored; auto-created from defaults on first run).
Complete reference — copy from `config.example.json`:

| Key | Default | Meaning |
|---|---|---|
| `server.host` | `127.0.0.1` | dashboard bind (loopback; §19) |
| `server.port` | `8080` | dashboard port |
| `server.api_key` | `""` | optional server password (Bearer/Basic); empty = no auth; env `KAISEN_API_KEY` wins (§19) |
| `llm.read_timeout` | `1200` | per-request read timeout (s) |
| `llm.connect_timeout` | `15` | connection timeout (s) |
| `llm.nodata_timeout` | `120` | max silence between tokens (s) |
| `llm.max_retries` | `3` | per-server retries |
| `llm.retry_backoff` | `2.0` | backoff multiplier |
| `llm.max_tokens` | `8192` | cap for UNLIMITED generations |
| `llm.active_ids` | `[]` | which servers are active (checkbox set) |
| `llm.servers` | `[…]` | the server registry (§13) |
| `llm.routing` | `"cost"` | `"cost"` (tier-first, default) or `"adaptive"` (best measured score-per-$ per skill, within allowlists) |
| `llm.allowlists` | `{}` | per-skill model allowlists: `{"suggest": ["frontier-70b"], "llm_repair": ["tier:tiny"]}` — entries are server ids or `tier:<t>` |
| `workers.default_count` | `4` | worker processes per engine |
| `workers.max_count` | `32` | hard ceiling |
| `workers.queue_size` | `8` | bounded pipeline queue |
| `engine.start_paused` | `true` | new engines boot paused |
| `telegram.*` | off | notifications (§17) |
| `safety.global_off` | `false` | requires `KAISEN_SAFETY_OFF=1` too (§19) |
| `autofix.build_enabled` | `true` | default for NEW projects |
| `autofix.llm_repair` | `true` | LLM last-resort repair gate (§7) |
| `onboarding.done` | `false` | wizard state |
| `debug_logs` | `true` | verbose engine logs |

CLI: `--project ID`, `--no-server`, `--host`, `--port`, `--kai`,
`--workers N`, `--multi K`.

---

## 19. Safety model

Hardcoded, absolute, and — for the one escape hatch — double-gated:

- **Guardrail choke point** — every pipeline command is checked before
  execution (single choke point in the pipeline runner). Denied:
  process spawning/`shell=True` escapes, file destruction, network
  egress, `exec`/`eval`, and per-language patterns for shell scripts.
  Extra allow/deny rules per project via `guardrails`.
- **Candidate & repair scans** — custom code and LLM-repair replies are
  danger-scanned and length-capped before they can run.
- **Protected data** — declared data files are hashed before every
  stage; any modification fails the run.
- **Resource limits** — per-step timeouts + RSS memory limits; score
  early-abort kills provably-worse candidates mid-benchmark.
- **The user baseline is sacred** — never rewritten, never repaired.
- **Global off** — requires BOTH `config.json` →
  `safety.global_off: true` AND environment `KAISEN_SAFETY_OFF=1`.
  Never toggleable from the GUI.
- **Network exposure** — no authentication by default; bind stays
  `127.0.0.1` (loopback only). Setting `server.host` to `0.0.0.0`
  exposes the full control surface to your LAN — only on a trusted
  network. Optional server password: `server.api_key` in config.json
  (env `KAISEN_API_KEY` wins) — browsers get a standard Basic-auth
  password prompt (any username, the key as password), API clients send
  `Authorization: Bearer <key>`.
- **Secrets** — env-first; `secrets.json` is 0600 and gitignored; API
  keys are never persisted back into `config.json`.

---

## 20. Languages

22 languages, one registry (extensions, code fences, guard patterns):
c, cpp, cuda, python, java, javascript, typescript, csharp, go, rust,
kotlin, swift, php, ruby, r, zig, scala, dart, haskell, lua, perl,
shell — plus aliases (`c++`, `py`, `js`, `cs`, `bash`, …). The gcc/nvcc
compiler-hint autofixer applies to the C family (c/cpp/cuda); Python
gets the linter fixer; other languages surface diagnostics through the
build stderr.

---

## 21. HTTP API (for AI agents)

The GUI itself is an HTTP client; everything is available over
`/api/*`. Highlights:

- `GET /api/projects`, `POST /api/projects` (create, guardrail-scanned),
  `GET /api/projects/{pid}/spec`, `PUT` (update),
  `DELETE /api/projects/{pid}` (409 while its engine runs),
  `POST /api/projects/{pid}/smoke`
- `POST /api/projects/suggest`, `POST /api/suggest/status` — the GOAL flow
- `GET /api/active` — selected engine snapshot + `engines[]` pool
- `POST /api/engine/switch|start|stop|pause` — pool controls
  (all take `project_id`)
- `POST /api/engine/multi` — parallel pipelines per project
- `POST /api/active/custom_code`, `POST /api/queue/custom_code` —
  inject code as a generation
- `GET /api/iterations?project_id=` — generation history
- `GET /api/llm/status`, `GET /api/llm/live` — pool-wide sessions/stats
- `POST /api/servers/add|remove|active|health|label`,
  `GET/PUT /api/config` — server registry + config
- `POST /api/swarm/start`, `GET /api/swarm/{id}`, `POST …/cancel` — swarms
- `POST /api/projects/{pid}/agent/start`, `GET /api/agent/status`,
  `POST /api/agent/cancel` — the project agent
- `POST /api/snapshots`, `GET /api/snapshots`,
  `POST /api/snapshots/restore` — snapshot store
- `POST /kai` — the KAI protocol (text in, text out)
- `GET /api/system`, `GET /api/guardrails`, `GET/POST /api/autofix` —
  health and safety surfaces

**Preferred interface for agents: KAI (§10).** It is designed for LLM
clients: tolerant parsing, compact OK/ERR replies, session state, and
per-project scoping. Use the raw HTTP API only when you need something
KAI does not expose.

---

## 22. Tests & development

```bash
pip install pytest
python -m pytest tests/
```

122 tests, no network, temp dirs only: HTTP API + engine pool scoping,
suggest gates, every deterministic autofix rule, KAI grammar, tier
routing, LLM repair flow. The suite is hermetic — it passes with no
dashboard running.

---

## 23. Performance notes (honest framing)

KAISEN has evolved real kernels autonomously (llama.cpp experiments): an
AVX2 SiLU 1.6× faster than llama.cpp's own AVX2 implementation
(0.242 vs 0.389 ms, max relative error 0.19%, dense-sweep verified),
and an AVX2 GELU ~74× faster than the naive scalar path (max error
2.1% — above the 1% goal, disclosed rather than hidden).

The end-to-end effect on gpt-oss-20b Q8_K inference was zero: decode and
prompt processing are ~90% Q8_K GEMM, and activation kernels are
single-digit percent of the trace. The lesson: **the harness defines the
win**. If the metric you score is not the metric you care about, the
champion will be optimized for the wrong thing. Judge results by
measurement of the real workload, not by headline multipliers.

---

*Manual is the complete reference as of KAISEN 0.1.0-alpha.*
