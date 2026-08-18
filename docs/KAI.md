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
| `STATUS` | engine + pool overview, per-project |
| `SPEC [id]` | the project's spec: steps, metrics, goal |
| `RUN [<n>] [FOR <secs>] [WITH <k>] [ON <pid>]` | start evolution (forever by default), background |
| `WAIT [<secs>]` | block until the in-flight run finishes (or snapshot) |
| `PAUSE` / `RESUME` / `STOP [ON <pid>]` | engine controls |
| `BEST [id]` | champion source + metrics — resolves real and temp projects (via `/api/projects/{pid}/best`) |
| `SMOKE [pid]` (also `ON <pid>`) | run the pipeline once on the baseline |
| `SERVERS` | LLM servers with tier/smartness/cost/free slots |
| `MODELS [skill]` | per-(model, skill) scoreboard: attempts, one-shots, wins, $ — which model does what best |
| `BASELINE [lang]` + code lines + `END` | stage the starting program |
| `CANDIDATE [lang]` + code lines + `END` | queue code as a generation |
| `SNAPSHOT [LIST\|TAKE\|RESTORE <id>]` | config/project snapshots |
| `SERVERS` | LLM servers with tier/smartness/cost/free slots |
| `GOAL <words> [TEMP]` | build a new project from a goal (suggest loop). TEMP: lives under the `temp/` root, wiped at server close/next startup |
| `ACCEPT <id>` | keep the project built by the last GOAL (carries its TEMP flag) |
| `CREATE <id> [TEMP] <spec-json>` | create a project from an explicit spec; TEMP = temp-rooted |
| `AUTOFIX [tries <n>] [repair <n\|off>]` | per-run compile-loop caps for the session project: deterministic autofix turns (default 5), LLM repair attempts (default 3; `off` = deterministic only, then fail) |
| `FORGE [<n>] [TIER <t>] [ON <pid>] [GOAL <words>]` | n parallel scored drafts (max 12) |
| `HELP` | this reference |

## Grammar notes

- `RUN 20` = stop after 20 scored generations. `RUN FOR 600` = 10-minute
  budget. `RUN WITH 3` = three parallel LLM pipelines. Flags combine in
  any order; `ON <pid>` targets another pool member.
- Multi-line commands (`BASELINE`, `CANDIDATE`) end with a line that is
  exactly `END`.
- `FORGE` blocks until the swarm job finishes (or ~20 minutes).

## Routing

Servers carry tier (`tiny`/`small`/`large`), priority, smartness,
context window and $/Mtoken cost. Requests route **cost-first**: the
lowest tier that can do the job, then priority, then free capacity; busy
servers fall through so the pipeline never stalls. `ESTIMATE` shows the
math before you commit.
