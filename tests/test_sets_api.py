"""SETS API: named, loadable bundles of projects (workspaces).

Contract under test is docs/SETS.md §5 — membership by `tags` in
project.json, one active workspace at a time, entering a set stops and
hides everything outside it.  Every destructive surface must return its
exact keys: `stopped`, `started`, `skipped_goal_met`, `stripped`, `added`,
`removed`, `tags`, `active`, `running`.

HERMETIC — no test boots a real engine: pool members are FakeEngines, and
the only path that constructs a real ProjectEngine (set start over a
goal-met member) stops at the goal latch before start() can spawn workers.
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from kaisen.goals import signature as goal_signature
from kaisen.server import DashboardServer
from kaisen.sets import add_tag
from kaisen.state import ProjectState

POLL = 0.05


class FakeEngine:
    """Minimal stand-in for ProjectEngine over the pool-control surface."""

    def __init__(self, pid, engine_state="running", generation=4,
                 best=None, parallel_gens=2, workers=3, paused=False):
        self.project = SimpleNamespace(id=pid, name=pid)
        self.engine_state = engine_state
        self._st = SimpleNamespace(generation=generation, paused=paused,
                                   best=best or {})
        self._parallel_gens = parallel_gens
        self.start_calls = []
        self.pool = SimpleNamespace(_procs={i: None for i in range(workers)})
        self.set_parallel_gens_calls = []

    @property
    def state(self):
        return self._st

    def snapshot(self):
        return {"project_id": self.project.id,
                "engine_state": self.engine_state,
                "state": {"generation": self._st.generation,
                          "paused": self._st.paused,
                          "best": self._st.best}}

    def stop(self):
        self.engine_state = "stopped"

    def request_pause(self):
        self._st.paused = True

    def request_resume(self):
        self._st.paused = False

    def set_parallel_gens(self, n):
        self._parallel_gens = n
        self.set_parallel_gens_calls.append(n)
        return n

    def start(self, parallel_gens=1, paused=False):
        self.start_calls.append({"parallel_gens": parallel_gens,
                                 "paused": paused})
        self.engine_state = "running"

    def set_share(self, max_parallel=None, reserve=None):
        if max_parallel is not None:
            self._max_parallel = int(max_parallel) or None
        if reserve is not None:
            self._reserve = bool(reserve)
        return {"max_parallel": self._max_parallel, "reserve": self._reserve}

    def set_fuzzy(self, n):
        self.fuzzy_top_n = n
        return n


class _FakeBootEngine:
    """ProjectEngine stand-in for the set-start path: records start() calls
    instead of spawning worker subprocesses (hermetic)."""

    def __init__(self, project, orchestrator, registry, worker_count=1,
                 events=None):
        self.project = project
        self.state = SimpleNamespace(goal_done=lambda: False)
        self.start_calls = []

    def start(self, parallel_gens=1, paused=False):
        self.start_calls.append({"parallel_gens": parallel_gens,
                                 "paused": paused})


def _live_server(tmp_cfg, registry):
    """(server, base_url) for a live DashboardServer on an ephemeral port."""
    srv = DashboardServer(registry, tmp_cfg, engine=None,
                          host="127.0.0.1", port=8080,
                          temp_root=tmp_cfg.path.parent / "temp")
    holder = {}

    def _serve():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["loop"] = loop
        runner = __import__("aiohttp").web.AppRunner(srv.app)
        loop.run_until_complete(runner.setup())
        site = __import__("aiohttp").web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        holder["port"] = site._server.sockets[0].getsockname()[1]
        holder["runner"] = runner
        loop.run_forever()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    deadline = time.time() + 10
    while "port" not in holder and time.time() < deadline:
        time.sleep(POLL)
    assert "port" in holder, "server did not bind"
    # /kai's self-connect must hit THIS server, not the configured port.
    srv.port = holder["port"]
    base = f"http://127.0.0.1:{holder['port']}"
    yield srv, base
    loop = holder["loop"]
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)


@pytest.fixture
def api(tmp_cfg, registry):
    """(server, base_url) for a live DashboardServer on an ephemeral port."""
    yield from _live_server(tmp_cfg, registry)


def _spec(pid, name="X"):
    return {"id": pid, "name": name,
            "steps": {"build": {"program": "gcc", "args": []}, "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}


# A goal this spec can actually hit — `ms` is declared in _spec's metrics.
GOAL_MS = {"when": {"metric": "ms", "op": "<=", "value": 10}}


def _seed_engines(srv, pids):
    for pid in pids:
        srv.engines[pid] = FakeEngine(pid)
    srv._selected_project_id = pids[0]


def _create_set(base, name, description=""):
    r = requests.post(base + "/api/sets",
                      json={"name": name, "description": description},
                      timeout=5)
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r.json()["set"]


def _rows_by_id(base):
    rows = requests.get(base + "/api/projects", timeout=5).json()["projects"]
    return {p["id"]: p for p in rows}


def _disk_spec(registry, pid):
    path = registry.root / pid / "project.json"
    return json.loads(path.read_text(encoding="utf-8"))

# ----------------------------------------------------------------------
# 1. Set CRUD
# ----------------------------------------------------------------------

def test_set_create_slug_and_dedupe(api):
    _, base = api
    s1 = _create_set(base, "LLM Decode Speedups", "25 decoding features")
    assert s1["id"] == "llm-decode-speedups"   # slug derived from the name
    for key in ("id", "name", "description", "created_at"):
        assert key in s1
    assert s1["name"] == "LLM Decode Speedups"
    assert s1["description"] == "25 decoding features"

    s2 = _create_set(base, "LLM Decode Speedups")
    assert s2["id"] == "llm-decode-speedups-2"  # deduped, id never collides


def test_set_create_bad_names_rejected(api):
    _, base = api
    for name in ("", "   ", "!!!"):
        r = requests.post(base + "/api/sets", json={"name": name}, timeout=5)
        assert r.status_code == 400, f"name {name!r}"
        d = r.json()
        assert d["ok"] is False and "error" in d


def test_set_rename_keeps_id(api):
    _, base = api
    sid = _create_set(base, "Rename Me")["id"]
    assert sid == "rename-me"
    r = requests.patch(base + f"/api/sets/{sid}",
                       json={"name": "Renamed Set",
                             "description": "the new description"}, timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["set"]["id"] == sid                # id is immutable — tags keep pointing at it
    assert d["set"]["name"] == "Renamed Set"
    assert d["set"]["description"] == "the new description"


def test_set_update_unknown_404(api):
    _, base = api
    r = requests.patch(base + "/api/sets/no-such-set", json={"name": "X"}, timeout=5)
    assert r.status_code == 404 and r.json()["ok"] is False


def test_set_delete_removes_definition(api):
    _, base = api
    s = _create_set(base, "Throwaway")
    r = requests.delete(base + f"/api/sets/{s['id']}", timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["stripped"] == []                  # no members — only the definition goes
    listed = requests.get(base + "/api/sets", timeout=5).json()
    assert all(row["id"] != s["id"] for row in listed["sets"])


def test_set_list_contract(api, registry):
    """GET /api/sets rows carry exactly the fields the workspace bar renders."""
    srv, base = api
    sid = _create_set(base, "Shape Set")["id"]
    registry.create("shape-proj", _spec("shape-proj"))
    r = requests.post(base + f"/api/sets/{sid}/members",
                      json={"project_ids": ["shape-proj"]}, timeout=5)
    assert r.json()["ok"]

    d = requests.get(base + "/api/sets", timeout=5).json()
    assert d["active"] is None
    row = next(s for s in d["sets"] if s["id"] == sid)
    for key in ("id", "name", "description", "created_at", "members", "running"):
        assert key in row
    assert row["members"] == 1
    assert row["running"] == 0


# ----------------------------------------------------------------------
# 2. GET /api/projects rows expose `tags`
# ----------------------------------------------------------------------

def test_project_rows_carry_tags(api, registry):
    _, base = api
    registry.create("tag-proj", _spec("tag-proj"))
    rows = _rows_by_id(base)
    assert rows["tag-proj"]["tags"] == []       # untagged = default workspace


# ----------------------------------------------------------------------
# 3. New projects join the ACTIVE set (the _create_project choke point)
# ----------------------------------------------------------------------

def test_create_in_active_set_is_tagged(api, registry):
    srv, base = api
    sid = _create_set(base, "Tagged Set")["id"]
    r = requests.post(base + "/api/sets/active", json={"id": sid}, timeout=5)
    assert r.json() == {"ok": True, "active": sid, "stopped": []}

    r = requests.post(base + "/api/projects",
                      json={"id": "born-tagged", "spec": _spec("born-tagged")},
                      timeout=5)
    assert r.status_code == 200 and r.json()["ok"]

    assert _rows_by_id(base)["born-tagged"]["tags"] == [sid]
    assert _disk_spec(registry, "born-tagged")["tags"] == [sid]


def test_create_in_default_is_untagged(api, registry):
    srv, base = api
    r = requests.post(base + "/api/sets/active", json={"id": None}, timeout=5)
    assert r.json() == {"ok": True, "active": None, "stopped": []}

    r = requests.post(base + "/api/projects",
                      json={"id": "born-bare", "spec": _spec("born-bare")},
                      timeout=5)
    assert r.status_code == 200 and r.json()["ok"]

    assert _rows_by_id(base)["born-bare"]["tags"] == []
    assert _disk_spec(registry, "born-bare")["tags"] == []


# ----------------------------------------------------------------------
# 4. Membership — tags in project.json, multi-set membership allowed
# ----------------------------------------------------------------------

def test_members_add_updates_api_and_disk(api, registry):
    srv, base = api
    sid = _create_set(base, "Member Set")["id"]
    registry.create("mem-a", _spec("mem-a"))

    r = requests.post(base + f"/api/sets/{sid}/members",
                      json={"project_ids": ["mem-a"]}, timeout=5)
    assert r.status_code == 200 and r.json() == {"ok": True, "added": ["mem-a"]}

    assert _rows_by_id(base)["mem-a"]["tags"] == [sid]   # the API sees it…
    assert _disk_spec(registry, "mem-a")["tags"] == [sid]  # …and project.json has it


def test_members_add_unknown_id_reports_added(api, registry):
    _, base = api
    sid = _create_set(base, "Member Set 2")["id"]
    registry.create("mem-b", _spec("mem-b"))

    r = requests.post(base + f"/api/sets/{sid}/members",
                      json={"project_ids": ["mem-b", "ghost-proj"]}, timeout=5)
    assert r.status_code == 400
    d = r.json()
    assert d["ok"] is False
    assert d["added"] == ["mem-b"]          # the known one was tagged before the 400
    assert "ghost-proj" in d["error"]


def test_member_in_two_sets_counts_both(api, registry):
    _, base = api
    sa = _create_set(base, "Dual Set A")["id"]
    sb = _create_set(base, "Dual Set B")["id"]
    registry.create("dual-proj", _spec("dual-proj"))
    requests.post(base + f"/api/sets/{sa}/members",
                  json={"project_ids": ["dual-proj"]}, timeout=5)
    requests.post(base + f"/api/sets/{sb}/members",
                  json={"project_ids": ["dual-proj"]}, timeout=5)

    listed = {s["id"]: s for s in
              requests.get(base + "/api/sets", timeout=5).json()["sets"]}
    assert listed[sa]["members"] == 1        # counted in BOTH sets
    assert listed[sb]["members"] == 1
    assert _rows_by_id(base)["dual-proj"]["tags"] == [sa, sb]


def test_members_remove_strips_tag(api, registry):
    srv, base = api
    sa = _create_set(base, "Strip Set A")["id"]
    sb = _create_set(base, "Strip Set B")["id"]
    registry.create("strip-proj", _spec("strip-proj"))
    requests.post(base + f"/api/sets/{sa}/members",
                  json={"project_ids": ["strip-proj"]}, timeout=5)
    requests.post(base + f"/api/sets/{sb}/members",
                  json={"project_ids": ["strip-proj"]}, timeout=5)

    r = requests.delete(base + f"/api/sets/{sa}/members/strip-proj", timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["removed"] == "strip-proj"
    assert d["tags"] == [sb]                 # remaining membership reported
    assert d["stopped"] == []                # no engine ran — nothing to stop
    assert _disk_spec(registry, "strip-proj")["tags"] == [sb]


def test_spec_save_preserves_tags(api, registry):
    """The GUI editor PUTs a whole spec that knows nothing about tags;
    saving it must never silently evict the project from its sets."""
    _, base = api
    sid = _create_set(base, "Preserve Set")["id"]
    registry.create("keep-tag", _spec("keep-tag"))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["keep-tag"]}, timeout=5)

    edited = {**_spec("keep-tag"), "description": "edited in the GUI"}
    r = requests.put(base + "/api/projects/keep-tag/spec",
                     json={"spec": edited}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"]

    assert _disk_spec(registry, "keep-tag")["tags"] == [sid]
    assert _rows_by_id(base)["keep-tag"]["tags"] == [sid]
    assert _rows_by_id(base)["keep-tag"]["description"] == "edited in the GUI"


# ----------------------------------------------------------------------
# 5. Workspace isolation — entering stops everything outside the set
# ----------------------------------------------------------------------

def test_enter_set_stops_outside_engines(api, registry):
    srv, base = api
    sid = _create_set(base, "Isolated Set")["id"]
    for pid in ("iso-in", "iso-out"):
        registry.create(pid, _spec(pid))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["iso-in"]}, timeout=5)

    _seed_engines(srv, ["iso-in", "iso-out"])
    before = srv.engines["iso-in"]

    r = requests.post(base + "/api/sets/active", json={"id": sid}, timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["active"] == sid
    assert d["stopped"] == ["iso-out"]

    assert "iso-out" not in srv.engines          # outside engine stopped + dropped
    assert srv.engines["iso-in"] is before       # member engine untouched
    listed = requests.get(base + "/api/sets", timeout=5).json()
    assert listed["active"] == sid


def test_exit_set_stops_set_engines(api, registry):
    srv, base = api
    sid = _create_set(base, "Exit Set")["id"]
    for pid in ("exit-in", "exit-out"):
        registry.create(pid, _spec(pid))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["exit-in"]}, timeout=5)

    _seed_engines(srv, ["exit-in"])
    r = requests.post(base + "/api/sets/active", json={"id": sid}, timeout=5)
    assert r.json()["ok"] and r.json()["stopped"] == []

    # an untagged engine running in the default workspace while the set is
    # active: it must survive the exit.
    srv.engines["exit-out"] = FakeEngine("exit-out")

    r = requests.post(base + "/api/sets/active", json={"id": None}, timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["active"] is None                   # back to the default workspace
    assert d["stopped"] == ["exit-in"]           # the set's engines stop on exit
    assert "exit-in" not in srv.engines
    assert "exit-out" in srv.engines            # untagged engines survive

# ----------------------------------------------------------------------
# 6. Start set — members boot, goal-met stay stopped (existing latch)
# ----------------------------------------------------------------------

def test_set_start_launches_member_engines(api, registry, monkeypatch):
    """The real ProjectEngine is swapped for a recorder: the endpoint's
    bookkeeping (started list, pool membership) is what's under test — no
    worker subprocesses ever spawn."""
    srv, base = api
    monkeypatch.setattr("kaisen.engine.ProjectEngine", _FakeBootEngine)
    sid = _create_set(base, "Start Fleet")["id"]
    for pid in ("fleet-a", "fleet-b"):
        registry.create(pid, _spec(pid))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["fleet-a", "fleet-b"]}, timeout=5)
    r = requests.post(base + "/api/sets/active", json={"id": sid}, timeout=5)
    assert r.json()["ok"] and r.json()["active"] == sid

    r = requests.post(base + f"/api/sets/{sid}/start", json={}, timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["started"] == ["fleet-a", "fleet-b"]
    assert d["skipped_goal_met"] == []
    assert srv.engines["fleet-a"].start_calls     # booted through the endpoint
    assert srv.engines["fleet-b"].start_calls


def test_set_start_rejects_inactive_and_unknown(api, registry):
    _, base = api
    sid = _create_set(base, "Not Entered")["id"]
    registry.create("inactive-a", _spec("inactive-a"))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["inactive-a"]}, timeout=5)

    # entering is a precondition: only the ACTIVE set can be started.
    r = requests.post(base + f"/api/sets/{sid}/start", json={}, timeout=5)
    assert r.status_code == 400 and r.json()["ok"] is False

    r = requests.post(base + "/api/sets/no-such-set/start", json={}, timeout=5)
    assert r.status_code == 404 and r.json()["ok"] is False


def test_set_start_skips_pool_and_goal_met(api, registry):
    """No monkeypatching here: the handler really walks the members.  One is
    already in the pool (FakeEngine — must not double-boot), one is DONE
    (state.json carries the goal latch — a real ProjectEngine is CONSTRUCTED
    and skipped at the latch, never started)."""
    srv, base = api
    sid = _create_set(base, "Skip Fleet")["id"]
    registry.create("skip-a", _spec("skip-a"))
    done = registry.create("skip-b", {**_spec("skip-b"), "goal": GOAL_MS})
    state = ProjectState(done)
    state.set_goal_met(goal_signature(done.spec["goal"]),
                       {"generation": 3}, "ms <= 10 (seen 9)")
    state.save()
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["skip-a", "skip-b"]}, timeout=5)

    _seed_engines(srv, ["skip-a"])
    before = srv.engines["skip-a"]
    r = requests.post(base + "/api/sets/active", json={"id": sid}, timeout=5)
    assert r.json()["ok"] and r.json()["stopped"] == []

    r = requests.post(base + f"/api/sets/{sid}/start", json={}, timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["started"] == []
    assert d["skipped_goal_met"] == ["skip-b"]

    assert srv.engines["skip-a"] is before       # pool member untouched
    assert "skip-b" not in srv.engines          # done project stays stopped

    # the latch round-trips through state.json (the row keeps its DONE chip)
    assert _rows_by_id(base)["skip-b"]["goal"]["met"] is True


def test_set_start_restarts_stopped_member(api, registry):
    """A stopped member (goal reached earlier, manual stop, or error) stays
    in the pool dict; Start-set must RESTART it — the old code skipped every
    dict member as "already in the pool" and started nothing."""
    srv, base = api
    sid = _create_set(base, "Restart Fleet")["id"]
    registry.create("rs-a", _spec("rs-a"))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["rs-a"]}, timeout=5)
    _seed_engines(srv, ["rs-a"])
    srv.engines["rs-a"].engine_state = "stopped"
    r = requests.post(base + "/api/sets/active", json={"id": sid}, timeout=5)
    assert r.json()["ok"]

    r = requests.post(base + f"/api/sets/{sid}/start", json={}, timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["started"] == ["rs-a"]
    assert srv.engines["rs-a"].start_calls
    assert srv.engines["rs-a"].engine_state == "running"

# ----------------------------------------------------------------------
# 7. Stop set — every member engine stops
# ----------------------------------------------------------------------

def test_set_stop_stops_member_engines(api, registry):
    srv, base = api
    sid = _create_set(base, "Stop Fleet")["id"]
    for pid in ("stop-a", "stop-b"):
        registry.create(pid, _spec(pid))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["stop-a", "stop-b"]}, timeout=5)
    registry.create("stop-outside", _spec("stop-outside"))
    _seed_engines(srv, ["stop-a", "stop-b", "stop-outside"])

    listed = {s["id"]: s for s in
              requests.get(base + "/api/sets", timeout=5).json()["sets"]}
    assert listed[sid]["members"] == 2 and listed[sid]["running"] == 2

    r = requests.post(base + f"/api/sets/{sid}/stop", json={}, timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert sorted(d["stopped"]) == ["stop-a", "stop-b"]
    assert "stop-a" not in srv.engines and "stop-b" not in srv.engines
    assert "stop-outside" in srv.engines        # non-members untouched

    listed = {s["id"]: s for s in
              requests.get(base + "/api/sets", timeout=5).json()["sets"]}
    assert listed[sid]["running"] == 0


def test_set_stop_unknown_404(api):
    _, base = api
    r = requests.post(base + "/api/sets/no-such-set/stop", json={}, timeout=5)
    assert r.status_code == 404 and r.json()["ok"] is False


# ----------------------------------------------------------------------
# 8. Delete set — never deletes projects; the tag comes off
# ----------------------------------------------------------------------

def test_set_delete_409_while_member_runs_then_ok(api, registry):
    srv, base = api
    sid = _create_set(base, "Delete Me")["id"]
    registry.create("del-a", _spec("del-a"))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["del-a"]}, timeout=5)
    _seed_engines(srv, ["del-a"])

    r = requests.delete(base + f"/api/sets/{sid}", timeout=5)
    assert r.status_code == 409
    d = r.json()
    assert d["ok"] is False and "error" in d
    assert d["running"] == ["del-a"]            # the running members are reported

    r = requests.post(base + f"/api/sets/{sid}/stop", json={}, timeout=5)
    assert r.json()["stopped"] == ["del-a"]

    r = requests.delete(base + f"/api/sets/{sid}", timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["stripped"] == ["del-a"]           # tag stripped, project survives
    assert _disk_spec(registry, "del-a")["tags"] == []
    listed = requests.get(base + "/api/sets", timeout=5).json()
    assert all(s["id"] != sid for s in listed["sets"])


# ----------------------------------------------------------------------
# 9. Remove a running member — the engine stops only if it leaves the
#    ACTIVE workspace (multi-set membership keeps it running otherwise)
# ----------------------------------------------------------------------

def test_remove_member_leaving_active_workspace_stops_engine(api, registry):
    srv, base = api
    sa = _create_set(base, "Leave Set A")["id"]
    sb = _create_set(base, "Leave Set B")["id"]
    registry.create("leave-proj", _spec("leave-proj"))
    requests.post(base + f"/api/sets/{sa}/members",
                  json={"project_ids": ["leave-proj"]}, timeout=5)
    requests.post(base + f"/api/sets/{sb}/members",
                  json={"project_ids": ["leave-proj"]}, timeout=5)
    _seed_engines(srv, ["leave-proj"])

    r = requests.post(base + "/api/sets/active", json={"id": sa}, timeout=5)
    assert r.json()["ok"] and r.json()["stopped"] == []  # it's in the workspace

    r = requests.delete(base + f"/api/sets/{sa}/members/leave-proj", timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["removed"] == "leave-proj"
    assert d["tags"] == [sb]                 # still a member of the other set
    assert d["stopped"] == ["leave-proj"]    # but it left the ACTIVE workspace
    assert "leave-proj" not in srv.engines


def test_remove_member_still_in_active_set_keeps_running(api, registry):
    srv, base = api
    sa = _create_set(base, "Keep Set A")["id"]
    sb = _create_set(base, "Keep Set B")["id"]
    registry.create("keep-proj", _spec("keep-proj"))
    requests.post(base + f"/api/sets/{sa}/members",
                  json={"project_ids": ["keep-proj"]}, timeout=5)
    requests.post(base + f"/api/sets/{sb}/members",
                  json={"project_ids": ["keep-proj"]}, timeout=5)
    _seed_engines(srv, ["keep-proj"])
    before = srv.engines["keep-proj"]

    r = requests.post(base + "/api/sets/active", json={"id": sb}, timeout=5)
    assert r.json()["ok"] and r.json()["stopped"] == []  # active set keeps it

    r = requests.delete(base + f"/api/sets/{sa}/members/keep-proj", timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["removed"] == "keep-proj"
    assert d["tags"] == [sb]
    assert d["stopped"] == []                # multi-set membership keeps it in
    assert srv.engines["keep-proj"] is before


# ----------------------------------------------------------------------
# 10. engine/switch — scoped to the active workspace
# ----------------------------------------------------------------------

def test_engine_switch_rejects_project_outside_active_set(api, registry):
    srv, base = api
    sid = _create_set(base, "Switch Set")["id"]
    registry.create("switch-in", _spec("switch-in"))
    registry.create("switch-out", _spec("switch-out"))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["switch-in"]}, timeout=5)
    r = requests.post(base + "/api/sets/active", json={"id": sid}, timeout=5)
    assert r.json()["ok"]

    r = requests.post(base + "/api/engine/switch",
                      json={"project_id": "switch-out"}, timeout=5)
    assert r.status_code == 409
    d = r.json()
    assert d["ok"] is False and "error" in d


def test_engine_switch_member_ok_without_boot(api, registry):
    """The member's engine is ALREADY in the pool (FakeEngine), so switching
    selects it without booting anything — the success path, hermetically."""
    srv, base = api
    sid = _create_set(base, "Switch Set 2")["id"]
    registry.create("switch-member", _spec("switch-member"))
    requests.post(base + f"/api/sets/{sid}/members",
                  json={"project_ids": ["switch-member"]}, timeout=5)
    _seed_engines(srv, ["switch-member"])
    r = requests.post(base + "/api/sets/active", json={"id": sid}, timeout=5)
    assert r.json()["ok"]

    r = requests.post(base + "/api/engine/switch",
                      json={"project_id": "switch-member"}, timeout=5)
    d = r.json()
    assert r.status_code == 200 and d["ok"]
    assert d["active_id"] == "switch-member"
    assert d["started"] is False              # reused the seeded engine, no boot
    assert srv._selected_project_id == "switch-member"


# ----------------------------------------------------------------------
# 11. Engine-pool restore keeps only active-workspace engines
# ----------------------------------------------------------------------

def test_pool_restore_keeps_only_active_workspace_engines(tmp_cfg, registry):
    """engine_pool.json is restored filtered by the ACTIVE workspace: a pool
    spanning two sets comes back with only the set's engines — the other
    stays stopped until you enter its set (same pattern as
    test_engine_pool_persist_and_restore)."""
    registry.create("pool-in", _spec("pool-in"))
    registry.create("pool-out", _spec("pool-out"))
    temp_root = tmp_cfg.path.parent / "temp"

    srv1 = DashboardServer(registry, tmp_cfg, engine=None,
                           host="127.0.0.1", port=8080, temp_root=temp_root,
                           restore_paused=True)
    sid_in = srv1.sets.create("Pool Workspace In", "")["id"]
    sid_out = srv1.sets.create("Pool Workspace Out", "")["id"]
    add_tag(registry.get("pool-in"), sid_in)
    add_tag(registry.get("pool-out"), sid_out)
    srv1.sets.set_active(sid_in)

    _seed_engines(srv1, ["pool-in", "pool-out"])
    srv1._persist_engine_pool()
    assert (tmp_cfg.path.parent / "engine_pool.json").exists()

    srv2 = DashboardServer(registry, tmp_cfg, engine=None,
                           host="127.0.0.1", port=8080, temp_root=temp_root,
                           restore_paused=True)
    assert "pool-in" in srv2.engines          # inside the active workspace
    assert "pool-out" not in srv2.engines     # other set: stays stopped
    assert srv2._selected_project_id == "pool-in"
    srv2.engines["pool-in"].stop()
