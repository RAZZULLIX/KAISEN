# SETS — project workspaces

Status: SPEC (approved design, not yet implemented).

## 1. Problem

KAISEN today has one flat project list. To research a bounded goal ("speed up
these 25 LLM-decoding features") the user must Open + start each project's
engine by row, and Stop them one by one afterwards — out of a list that also
contains every unrelated project.

## 2. Idea

A **set** is a named, loadable bundle of projects. Entering a set puts the
dashboard into that set's **workspace**: only its projects are visible, and
only its engines can run. Everything outside the set is stopped and hidden
until the user exits the set or enters another one.

Membership is by **tags**: a project carries a list of set tags and can
belong to several sets at once. A project with **no tags** is an *orphan*: it
lives in the **default workspace** (the unnamed set), which is exactly
today's flat list.

## 3. Core rules

1. **Tags, not folders.** `project.json` gains `tags: [set-id, …]`. A project
   may be in 0..N sets. Untagged ⇒ default workspace.
2. **One workspace at a time.** The dashboard has an `active` set (or `null`
   = default). While a set is active, engines of projects outside it MUST NOT
   run and MUST NOT be visible.
3. **Entering a set stops everything outside it** (killing in-flight
   generations) — behind a confirmation dialog that lists the engines that
   will stop.
4. **Entering does NOT start the set.** The set page opens first (editor +
   **Start** button at the top): rename, edit description, manage members,
   create new projects — then Start launches the fleet when ready.
5. **Exiting a running set asks confirmation** and warns that the set will
   stop; on confirm its engines stop and the default workspace shows.
6. **Every destructive action confirms.** Generations can be slow and costly:
   entering a set that kills engines, exiting a running set, stopping a set,
   removing a running member, deleting a set, and the existing per-row Stop
   (which today stops without asking) all require an explicit confirm.
7. **New projects join the active set.** Created while a set is active ⇒
   tagged with it (GUI create, suggest, onboarding demo, KAI — all go through
   the same `_create_project` choke point). Created in default ⇒ untagged.
8. **Deleting a set never deletes projects**: its tag is stripped from every
   member; members with no remaining tags become orphans.

## 4. Data model

### 4.1 `sets.json` (repo root, gitignored — user data like `engine_pool.json`)

```json
{
  "version": 1,
  "active": "llm-decode",
  "sets": {
    "llm-decode": {
      "name": "LLM Decode Speedups",
      "description": "25 features to speed up in the decoding engine",
      "created_at": 1760000000.0
    }
  }
}
```

- Set id: slug `[a-z0-9_-]+` (same `re_ident` rule as projects), derived from
  the name, deduplicated with `-2`, `-3`. Ids are immutable; rename changes
  `name` only, so tags never break.
- `active`: set id or `null`. Persisted ⇒ a restart reopens the same
  workspace.

### 4.2 `project.json`

- `DEFAULT_SPEC` gains `"tags": []`.
- `validate_spec`: `tags` must be a list of strings, each `[a-z0-9_-]+`.
- Membership travels with the project: deleting a project removes its
  membership automatically; no orphan references to clean up.

### 4.3 New module `kaisen/sets.py`

- `SETS_FILE = FRAMEWORK_ROOT / "sets.json"`.
- `class SetRegistry`: load/save (atomic via `util.save_json`), `list()`,
  `create(name, description)`, `rename`, `delete`, `get`, `active()`,
  `set_active()`.
- Tag helpers over `ProjectRegistry` (write `project.json`, then
  `Project.reload()` so a live engine picks the change up):
  `members(set_id)` → project ids whose spec tags contain the set;
  `add_tag(pid, set_id)`, `remove_tag(pid, set_id)`,
  `strip_tag(set_id)` → remove from all projects.

## 5. Server API (`kaisen/server.py`)

`DashboardServer.__init__` owns `self.sets = SetRegistry()`.

| Method + path | Behavior |
|---|---|
| `GET /api/sets` | `{sets: [{id, name, description, created_at, members, running}], active}` |
| `POST /api/sets` `{name, description?}` | Create. 400 on empty name. Slug auto-derived/deduped. |
| `PATCH /api/sets/{sid}` `{name?, description?}` | Edit metadata. Id immutable. |
| `DELETE /api/sets/{sid}` | 409 while any member engine runs. Strips the tag from all members; if active ⇒ workspace returns to default. |
| `POST /api/sets/active` `{id \| null}` | Enter set / exit to default. Stops every engine outside the target workspace. Returns `{ok, active, stopped: [pid…]}`. |
| `POST /api/sets/{sid}/start` | 400 unless `sid` is the active set. Starts each member engine; **skips goal-met projects** (done projects stay stopped, existing semantics). Offloaded with `asyncio.to_thread` (control-plane rule). |
| `POST /api/sets/{sid}/stop` | Stops all member engines. |
| `POST /api/sets/{sid}/members` `{project_ids: […]}` | Tag existing projects (multi-membership allowed). 400 on unknown/temp ids. |
| `DELETE /api/sets/{sid}/members/{pid}` | Strip tag; stops the project's engine if it is running (it is leaving the workspace). |

Changes to existing endpoints:

- `GET /api/projects` — every row gains `"tags": [...]`. The server keeps
  returning ALL projects; scoping is a UI concern.
- `POST /api/projects` (`_create_project`) — when a set is active, the new
  project is tagged with it. Single choke point ⇒ GUI, suggest, onboarding
  demo and KAI all inherit this.
- `POST /api/engine/switch` — 409 `"project is not in the active set"` when
  the target project is not a member of the active workspace. (KAI-created
  projects are auto-tagged, so the KAI flow keeps working.)
- Engine-pool restore (`engine_pool.json`) — unchanged format; after restore,
  engines outside the active workspace are stopped and dropped.

## 6. UI (`pages/dashboard.html`, `pages/script.js`, `pages/style.css`)

The Projects view becomes **workspace-aware**:

- **Workspace bar** (top of the view): chip `Default`, one chip per set,
  `+ New set`. The active chip is highlighted.
- **Default workspace**: today's list, scoped to untagged projects (+ temp
  projects, which are never taggable).
- **Set workspace** (after clicking a set chip):
  - Header card: set name (inline rename), description, `N projects · M
    running`, buttons **▶ Start set**, **■ Stop set**, **← Exit set**.
  - Member table: same row renderer as the default list (state, best,
    valid-rate, goal) plus a **Remove** action (strip tag).
  - **Add existing project** → modal with a searchable checkbox list of all
    projects (showing their current tags).
  - **New project in set** → the existing create/suggest modals; the server
    auto-tags.
- **Fleet panel**: rows filtered to active-workspace members (belt & braces —
  the invariant already guarantees only those can run).
- **Confirmations** (`systemConfirm`, listing affected engines):
  - entering a set while engines outside it run,
  - exiting a running set ("Set X will stop"),
  - switching directly from set A to set B (A stops, B's page opens),
  - Stop set, remove running member, delete set,
  - per-row **Stop** (new: today it stops silently — violates the
    always-confirm rule).

## 7. Edge cases

- **Goal-met member**: `Start set` skips it; the row keeps its DONE chip.
- **Empty set**: Start is a no-op with a toast; the page shows an empty state.
- **Selected engine dies on a workspace switch**: existing
  `_fallback_engine` promotion applies; may become `null` (welcome state).
- **Restart inside a set**: `sets.json` restores the workspace;
  `engine_pool.json` restore keeps only workspace engines.
- **Temp projects**: default workspace only, never tagged, excluded from the
  member picker.
- **Rename**: id stays; tags keep pointing at the id.
- **Delete set while active with no running members**: allowed; workspace
  returns to default.
- **Concurrency**: the server process is the single writer of `sets.json`;
  writes go through `save_json` (atomic).

## 8. Non-goals (v1)

- KAI commands for managing sets (KAI just inherits auto-tagging + switch
  scoping).
- Set-level aggregate scoreboard (cross-project score comparison).
- Set export/import.
- Per-set engine sizing (workers/gens caps live per project as today).
- Editing `tags` from the project spec editor (membership is managed from
  the set UI).

## 9. Tests (`tests/test_sets_api.py`, reuses `api`/`registry` fixtures + `FakeEngine`)

1. Set CRUD; bad/duplicate ids; rename keeps id.
2. `GET /api/projects` rows expose `tags`.
3. Create project while a set is active ⇒ auto-tagged; in default ⇒ not.
4. Add/remove members; a project in two sets appears in both.
5. Enter set ⇒ engines outside it stop (`stopped` list); exit ⇒ set engines stop.
6. Start set ⇒ member engines start; goal-met member skipped.
7. Stop set ⇒ all members stopped.
8. Delete set ⇒ tag stripped from every member; 409 while a member runs.
9. Remove running member ⇒ its engine stops.
10. `engine/switch` to a non-member while a set is active ⇒ 409.
11. Pool restore keeps only active-workspace engines.
12. UI smoke (browser): create set → add members → start → fleet/list scoping
    → exit confirm.

## 10. Docs

- `CHANGELOG.md` entry.
- `MANUAL.md`: "Sets — project workspaces" section (Projects chapter).
- `README.md`: feature bullet.
