# KAI — the LLM-facing protocol (spec)

KAI is KAISEN's tool surface for LLM agents. It is **not** MCP: it is a
line-oriented, stateful, session-scoped protocol designed for small local
models. Two transports, same grammar:

- **stdio**: `python3 main.py --kai` (one session per process)
- **HTTP**: `POST /kai` with the command text as the request body and the
  reply as `text/plain` (one session per request)

When the dashboard has a server password set (`server.api_key` /
`KAISEN_API_KEY`), HTTP clients must send
`Authorization: Bearer <key>`; the stdio transport reads
`KAISEN_API_KEY` from the environment and forwards it automatically.

## Reliability contract

Every reply starts with `OK` or `ERR`. An error never kills the session
and always names what went wrong and what to do next. Input parsing is
deliberately tolerant, because LLMs decorate everything:

- `OK STATUS`, `OK? RUN`, `ERR STATUS`, `CMD: STATUS`, `command=STATUS`,
  `"STATUS"`, `` `STATUS` ``, `*STATUS*` all parse as their bare command.
- Commands are case-insensitive and accept common synonyms (alias table
  below). Unknown commands return `ERR unknown command — HELP for the
  reference`, never a traceback.
- Engine operations are scoped by project id; replies report the engine
  they acted on.

## Session state

- `PROJECT <id>` sets the session's project (persists across requests via the
  `kaisen_kai_sid` cookie — curl `-c/-b` keeps it; a plain curl without a
  cookie starts fresh, so send `PROJECT <id>` + the command in ONE body).
- Baseline code staged with `BASELINE` and the last `GOAL` result are
  session state used by the following commands.
- A `RUN` creates an in-flight goal; `WAIT`/`STATUS` report against that
  goal's engine even when other pool engines are also running.  Run goals are
  persisted to disk (`kai_runs.json`), so a daemon restart does not lose the
  budget.
- `PAUSE`/`RESUME`/`STOP`/`SMOKE` accept `ON <pid>` to target another pool
  member without re-selecting it.


## Commands

| Command | Effect |
|---|---|
| `PROJECT <id>` | select the session project (must exist) |
| `STATUS` | engine + pool overview, per-project; includes `LLM PIPELINES x/y (z in flight)` utilization line |
| `SPEC [id]` | the project's spec: steps, metrics, the prompt goal, and the SUCCESS goal (`SUCCESS <metric> <op> <value> THEN <actions> [MET gen N]` — the criterion that ends the project) |
| `RUN [<n>] [FOR <secs>] [WITH <k>] [ON <pid>]` | start evolution (forever by default), background. `<n>` = stop after n SCORED generations; `FOR <secs>` = time budget (paused time excluded — only burns while the engine runs); both = whichever comes first |
| `RUN ALL [FOR <secs>] [WITH <k>]` | start every pool member at once — same budget and `k` parallel generations each; everything about multi-engine mode is optional |
| `BUDGET` | the in-flight run's budget: scored so far vs target + time remaining |
| `BUDGET SERVER [<sid>] [SET max_tokens <n> reset <r> [max_generations <n>]]` | per-server usage budget (optional): caps tokens/generations inside a reset window; an exhausted server drops out of routing until it rolls over. `n` = `1000000` / `1M` / `2.5M`; `r` = `30s` / `5m` / `12h` / `3d` / `1w` / `12:00:00` (= 12h). Blank clears a limit |
| `SCORE <path> [ON <pid>]` | score any file through the project's pipeline — no engine, no run (audit copy under `runs/score_*`) |
| `FUZZY <n> [ON <pid>]` | opt-in prompt diversity: random top-N scored basis per generation; also feeds the prompt the last 10 scored outcomes. 0 = off (default). Runtime only |
| `WAIT [<secs>]` | block until the in-flight run finishes (or snapshot) |
| `PAUSE` / `RESUME` / `STOP [ON <pid>]` | engine controls |
| `BEST [id]` | champion source + metrics — resolves real and temp projects (via `/api/projects/{pid}/best`) |
| `GEN <n> [ON <pid>] [RAW\|CODE\|PROMPT\|DIFF]` | the full per-generation log: prompt sent, RAW LLM reply (reasoning included, un-truncated), extracted program, diff vs champion — reads the persistent `runs/gen_NNNN/` archive (one field arg returns just that part) |
| `SMOKE [pid]` (also `ON <pid>`) | run the pipeline once on the baseline |
| `SERVERS` | LLM servers with tier/smartness/cost/free slots |
| `MODELS [skill]` | per-(model, skill) scoreboard: attempts, one-shots, wins, $ — which model does what best |
| `TOOLCHAINS` | per-language toolchain/compiler status |
| `ESTIMATE <in> [<out>]` | per-server cost/time estimate for a call of that size |
| `MODELCHECK [<sid>]` | verify a server's STREAMING path (first-token latency, content reaches the stream, prefill tps) |
| `LOGS [pid] [lines <n>] [grep <text>]` | engine log lines with filters |
| `BASELINE [lang]` + code lines + `END` | stage the starting program |
| `CANDIDATE [lang]` + code lines + `END` | queue code as a generation |
| `SNAPSHOT [LIST\|TAKE\|RESTORE <id>]` | config/project snapshots |
| `GOAL <words> [TEMP]` | build a new project from a goal (suggest loop). TEMP: lives under the `temp/` root, wiped at server close/next startup |
| `ACCEPT <id>` | keep the project built by the last GOAL (carries its TEMP flag) |
| `CREATE <id> [TEMP] <spec-json>` | create a project from an explicit spec; TEMP = temp-rooted |
| `FACTORY [ALGOS a,b] [LANGS c,python,rust,go] [CASES n] [BUILD_TIMEOUT sec] [CASE_TIMEOUT sec] [FORCE]` | generate algorithm × language projects (each self-checked: baseline builds, passes its seeded fuzz gate vs reference, scores) and register them; existing ids are skipped unless `FORCE` (delete + recreate) |
| `AUTOFIX [tries <n>] [repair <n\|off>] [candidates <n>]` | per-run compile-loop caps for the session project: deterministic autofix turns (default 5), LLM repair attempts (default 3; `off` = deterministic only, then fail), candidate fallback blocks (default 3) |
| `HELP` | this reference |

## Grammar notes

- `RUN 20` = stop after 20 scored generations. `RUN FOR 600` = 10-minute
  budget. `RUN WITH 3` = three parallel LLM pipelines. Flags combine in
  any order; `ON <pid>` targets another pool member.
- Multi-line commands (`BASELINE`, `CANDIDATE`) end with a line that is
  exactly `END`.
- `FACTORY`
  blocks while it generates + self-checks every project (a full 40-project
  run takes a few minutes).

## Success goals vs run budgets

Two different things end a run, and a client must not confuse them:

- **Run budget** (`RUN <n>`, `RUN FOR <secs>`) — a LIMIT you set with the
  command. `BUDGET` counts against it, `WAIT` blocks on it, it is persisted
  in `kai_runs.json` so a daemon restart keeps the remaining budget, and it
  is cleared when the run ends. Reaching it ends *the run*.
- **Success goal** — the project's own criterion, declared once in
  `project.json`: `"goal": {"when": {"metric": …, "op": …, "value": …},
  "then": [...]}`, with `then` defaulting to `["stop", "ping"]` (manual §5).

When the project's CHAMPION satisfies that goal the engine acts by itself,
with no command in flight: it appends a `goal_met` row to the project
history, pings (Telegram when configured), and by default **stops the
project**. The latch lives in the project's `state.json`, so a restart
neither resumes a finished project nor re-pings it; **editing the goal
re-arms it**. `SPEC` shows the criterion and whether it already fired:

```
SUCCESS proved_open >= 2 THEN stop, ping [MET gen 147]
```

So a run can end because its budget ran out *or* because the project is
done. `STATUS`/`BUDGET` describe the budget; a project stopped by its goal
is the project saying it has nothing left to reach — check `SPEC`, and
edit the goal if you want it to keep going.

## Routing

Servers carry tier (`tiny`/`small`/`large`), priority, smartness,
context window and $/Mtoken cost. Requests route **cost-first**: the
lowest tier that can do the job, then priority, then free capacity; busy
servers fall through so the pipeline never stalls. `ESTIMATE` shows the
math before you commit.
