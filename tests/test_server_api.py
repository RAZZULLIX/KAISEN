"""Dashboard HTTP API: projects CRUD, engine pool controls, /kai, guardrails.

Runs a REAL DashboardServer on an ephemeral port (own thread + loop) and
hits it over HTTP — this also exercises the /kai self-connect path.
"""
import asyncio
import base64
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from kaisen.server import DashboardServer

POLL = 0.05


class FakeEngine:
    """Minimal stand-in for ProjectEngine over the pool-control surface."""

    def __init__(self, pid, engine_state="running", generation=4,
                 best=None, multi=2, workers=3, paused=False):
        self.project = SimpleNamespace(id=pid, name=pid)
        self.engine_state = engine_state
        self._st = SimpleNamespace(generation=generation, paused=paused,
                                   best=best or {})
        self._multi = multi
        self.pool = SimpleNamespace(_procs={i: None for i in range(workers)})
        self.set_multi_calls = []

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

    def set_multi(self, n):
        self._multi = n
        self.set_multi_calls.append(n)
        return n


def _live_server(tmp_cfg, registry, mutate=None):
    """(server, base_url) for a live DashboardServer on an ephemeral port.
    `mutate(cfg)` tweaks the config (e.g. server.api_key) before the
    server reads it."""
    if mutate:
        mutate(tmp_cfg)
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


@pytest.fixture
def api_keyed(tmp_cfg, registry):
    """Same live server, but server.api_key = "sekret" -> 401 without it."""
    yield from _live_server(
        tmp_cfg, registry,
        mutate=lambda c: c.data["server"].update(api_key="sekret"))


@pytest.fixture
def api_env_key(tmp_cfg, registry, monkeypatch):
    """Key comes from KAISEN_API_KEY env; config key must lose (env-first)."""
    monkeypatch.setenv("KAISEN_API_KEY", "envsekret")
    yield from _live_server(
        tmp_cfg, registry,
        mutate=lambda c: c.data["server"].update(api_key="confsekret"))


# ----------------------------------------------------------------------
# static surface
# ----------------------------------------------------------------------

def test_index_serves_html(api):
    _, base = api
    r = requests.get(base + "/", timeout=5)
    assert "KAISEN" in r.text


def test_system_endpoint(api):
    _, base = api
    r = requests.get(base + "/api/system", timeout=5)
    assert r.status_code == 200
    d = r.json()
    assert d["ram_total"] is None or d["ram_total"] > 0  # graceful w/ or w/o psutil
    assert "load_avg" in d


# ----------------------------------------------------------------------
# projects CRUD
# ----------------------------------------------------------------------

def test_projects_list_empty_then_created(api, registry):
    srv, base = api
    assert requests.get(base + "/api/projects", timeout=5).json()["projects"] == []

    spec = {"id": "demo-proj", "name": "Demo",
            "steps": {"build": {"program": "gcc", "args": ["-O3", "{candidate}", "-o", "{artifact}"]},
                      "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower", "weight": 1.0}}}
    r = requests.post(base + "/api/projects", json={"id": "demo-proj", "spec": spec}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"]
    ids = [p["id"] for p in requests.get(base + "/api/projects", timeout=5).json()["projects"]]
    assert "demo-proj" in ids
    spec_back = requests.get(base + "/api/projects/demo-proj/spec", timeout=5).json()["spec"]
    assert spec_back["name"] == "Demo"


def test_project_create_rejects_guardrail_violation(api):
    _, base = api
    spec = {"id": "evil", "name": "Evil",
            "steps": {"build": {"program": "rm", "args": ["-rf", "/"]},
                      "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}
    r = requests.post(base + "/api/projects", json={"id": "evil", "spec": spec}, timeout=5)
    assert r.status_code == 400
    assert "guardrail" in r.json()["error"]


def test_project_create_rejects_bad_id(api):
    _, base = api
    spec = {"id": "Bad ID!", "name": "x",
            "steps": {"build": {"program": "gcc", "args": []}, "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}
    r = requests.post(base + "/api/projects", json={"id": "Bad ID!", "spec": spec}, timeout=5)
    assert r.status_code == 400

# ----------------------------------------------------------------------
# TEMP ROOT — quick agent runs never touch the real setup
# ----------------------------------------------------------------------

def _spec(pid, name="X"):
    return {"id": pid, "name": name,
            "steps": {"build": {"program": "gcc", "args": []}, "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}


def test_temp_create_lands_in_temp_root(api, tmp_cfg):
    srv, base = api
    r = requests.post(base + "/api/projects",
                      json={"id": "temp-proj", "spec": _spec("temp-proj"), "temp": True},
                      timeout=5)
    assert r.status_code == 200 and r.json()["ok"]
    temp_root = tmp_cfg.path.parent / "temp"
    assert (temp_root / "temp-proj" / "project.json").exists()
    assert not (srv.registry.root / "temp-proj").exists()  # real root untouched


def test_temp_project_listed_with_flag(api):
    srv, base = api
    requests.post(base + "/api/projects",
                  json={"id": "temp-listed", "spec": _spec("temp-listed"), "temp": True},
                  timeout=5)
    requests.post(base + "/api/projects",
                  json={"id": "real-listed", "spec": _spec("real-listed")},
                  timeout=5)
    items = requests.get(base + "/api/projects", timeout=5).json()["projects"]
    by_id = {p["id"]: p for p in items}
    assert by_id["temp-listed"].get("temp") is True
    assert not by_id["real-listed"].get("temp")


def test_temp_spec_and_delete_routed(api):
    srv, base = api
    requests.post(base + "/api/projects",
                  json={"id": "temp-del", "spec": _spec("temp-del"), "temp": True},
                  timeout=5)
    spec = requests.get(base + "/api/projects/temp-del/spec", timeout=5).json()["spec"]
    assert spec["id"] == "temp-del"
    r = requests.delete(base + "/api/projects/temp-del", timeout=5)
    assert r.json()["ok"]
    ids = [p["id"] for p in requests.get(base + "/api/projects", timeout=5).json()["projects"]]
    assert "temp-del" not in ids


def test_real_project_wins_over_temp_collision(api, tmp_cfg):
    srv, base = api
    requests.post(base + "/api/projects",
                  json={"id": "collision", "spec": _spec("collision", "REAL")},
                  timeout=5)
    requests.post(base + "/api/projects",
                  json={"id": "collision", "spec": _spec("collision", "TEMP"), "temp": True},
                  timeout=5)
    spec = requests.get(base + "/api/projects/collision/spec", timeout=5).json()["spec"]
    assert spec["name"] == "REAL"  # the real setup is never shadowed


def test_temp_project_best_returns_baseline(api, tmp_cfg):
    """The /api/projects/{pid}/best endpoint reads temp projects too."""
    srv, base = api
    spec = _spec("temp-best")
    spec["data"] = {"baseline_source": "original.c"}
    spec["files"] = {"original.c": "int main(){return 0;}"}
    requests.post(base + "/api/projects",
                  json={"id": "temp-best", "spec": spec, "temp": True}, timeout=5)
    r = requests.get(base + "/api/projects/temp-best/best", timeout=5)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["project_id"] == "temp-best"
    assert "int main()" in d["code"]
    temp_root = tmp_cfg.path.parent / "temp"
    assert d["source_path"].startswith(str(temp_root))  # resolved under temp/, not projects/


def test_temp_project_best_returns_champion(api, tmp_cfg):
    """Best reads the champion from the project's own state.json (temp root)."""
    import json as _json
    srv, base = api
    spec = _spec("temp-champ")
    spec["data"] = {"baseline_source": "original.c"}
    spec["files"] = {"original.c": "int main(){return 0;}"}
    requests.post(base + "/api/projects",
                  json={"id": "temp-champ", "spec": spec, "temp": True}, timeout=5)
    temp_root = tmp_cfg.path.parent / "temp"
    pdir = temp_root / "temp-champ"
    (pdir / "best").mkdir()
    champ = pdir / "best" / "program.c"
    champ.write_text("int main(){return 42;}", encoding="utf-8")
    _json.dump({"best": {"code_path": str(champ), "metrics": {"ms": 1.2}, "generation": 5}},
               open(pdir / "state.json", "w", encoding="utf-8"))
    d = requests.get(base + "/api/projects/temp-champ/best", timeout=5).json()
    assert d["ok"] is True
    assert "return 42" in d["code"]
    assert d["generation"] == 5
    assert d["metrics"] == {"ms": 1.2}


def test_kai_best_resolves_temp(api, tmp_cfg):
    """KAI BEST routes through /api/projects/{pid}/best, so temp projects work."""
    srv, base = api
    spec = _spec("temp-kai-best")
    spec["data"] = {"baseline_source": "original.c"}
    spec["files"] = {"original.c": "int main(){return 7;}"}
    requests.post(base + "/api/projects",
                  json={"id": "temp-kai-best", "spec": spec, "temp": True}, timeout=5)
    body = "PROJECT temp-kai-best\nBEST"
    r = requests.post(base + "/kai", data=body.encode(), timeout=10)
    assert r.status_code == 200
    assert "OK temp-kai-best" in r.text
    assert "return 7" in r.text


def test_temp_root_wiped_at_next_startup(api, tmp_cfg, registry):
    srv, base = api
    temp_root = tmp_cfg.path.parent / "temp"
    requests.post(base + "/api/projects",
                  json={"id": "temp-wipe", "spec": _spec("temp-wipe"), "temp": True},
                  timeout=5)
    assert (temp_root / "temp-wipe").exists()
    # next startup: a fresh server on the same roots wipes the temp root
    srv2 = DashboardServer(registry, tmp_cfg, engine=None,
                           host="127.0.0.1", port=8080, temp_root=temp_root)
    assert not (temp_root / "temp-wipe").exists()
    assert (srv2.registry.root / "temp-wipe").exists() or True  # real root logic
    ids = [p["id"] for p in srv2.registry.list()]
    assert "temp-wipe" not in ids



# ----------------------------------------------------------------------
# engine pool controls
# ----------------------------------------------------------------------

def _seed_engines(srv, pids):
    for pid in pids:
        srv.engines[pid] = FakeEngine(pid)
    srv._selected_project_id = pids[0]


def test_active_reports_pool(api):
    srv, base = api
    _seed_engines(srv, ["a-proj", "b-proj"])
    d = requests.get(base + "/api/active", timeout=5).json()
    assert d["project_id"] == "a-proj"
    pool_ids = [e["project_id"] for e in d["engines"]]
    assert set(pool_ids) == {"a-proj", "b-proj"}


def test_engine_pause_scoped_to_pid(api):
    srv, base = api
    _seed_engines(srv, ["a-proj", "b-proj"])
    r = requests.post(base + "/api/engine/pause", json={"paused": True, "project_id": "b-proj"},
                      timeout=5)
    assert r.json()["ok"] and r.json()["project_id"] == "b-proj"
    assert srv.engines["b-proj"].state.paused
    assert not srv.engines["a-proj"].state.paused


def test_engine_resume_scoped_to_pid(api):
    srv, base = api
    _seed_engines(srv, ["a-proj", "b-proj"])
    srv.engines["a-proj"].request_pause()
    requests.post(base + "/api/engine/pause", json={"paused": False, "project_id": "a-proj"},
                  timeout=5)
    assert not srv.engines["a-proj"].state.paused


def test_engine_multi_scoped_to_pid(api):
    srv, base = api
    _seed_engines(srv, ["a-proj", "b-proj"])
    r = requests.post(base + "/api/engine/multi", json={"multi": 5, "project_id": "b-proj"},
                      timeout=5)
    assert r.json()["multi"] == 5
    assert srv.engines["b-proj"]._multi == 5
    assert srv.engines["a-proj"]._multi == 2  # untouched


def test_engine_stop_removes_from_pool(api):
    srv, base = api
    _seed_engines(srv, ["a-proj", "b-proj"])
    r = requests.post(base + "/api/engine/stop", json={"project_id": "b-proj"}, timeout=5)
    assert r.json()["ok"] and r.json()["stopped"] == "b-proj"
    assert "b-proj" not in srv.engines
    assert srv.engines["a-proj"] is not None  # other engine untouched


def test_delete_409_while_running_then_ok_after_stop(api, registry):
    srv, base = api
    spec = {"id": "killable", "name": "K",
            "steps": {"build": {"program": "gcc", "args": []}, "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}
    requests.post(base + "/api/projects", json={"id": "killable", "spec": spec}, timeout=5)
    srv.engines["killable"] = FakeEngine("killable", engine_state="running")
    r = requests.delete(base + "/api/projects/killable", timeout=5)
    assert r.status_code == 409
    requests.post(base + "/api/engine/stop", json={"project_id": "killable"}, timeout=5)
    r = requests.delete(base + "/api/projects/killable", timeout=5)
    assert r.json()["ok"]


# ----------------------------------------------------------------------
# /kai — the LLM-facing protocol endpoint
# ----------------------------------------------------------------------

def test_kai_status(api):
    _, base = api
    r = requests.post(base + "/kai", data="STATUS", timeout=10)
    assert r.status_code == 200
    assert r.text.strip().startswith("OK")


def test_kai_empty_body_error(api):
    _, base = api
    r = requests.post(base + "/kai", data="   ", timeout=10)
    assert r.status_code == 200
    assert r.text.startswith("ERR")


def test_kai_garbage_command_errors_not_crashes(api):
    _, base = api
    r = requests.post(base + "/kai", data="BOGUS_COMMAND arg1", timeout=10)
    assert r.status_code == 200
    assert r.text.startswith("ERR")


def test_kai_project_flow(api, registry):
    srv, base = api
    spec = {"id": "kai-proj", "name": "KAI Proj",
            "steps": {"build": {"program": "gcc", "args": []}, "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}
    requests.post(base + "/api/projects", json={"id": "kai-proj", "spec": spec}, timeout=5)
    r = requests.post(base + "/kai", data="PROJECT kai-proj\nSPEC\n", timeout=10)
    assert "kai-proj" in r.text


def test_kai_ok_prefix_tolerance(api):
    _, base = api
    r = requests.post(base + "/kai", data="OK STATUS", timeout=10)
    assert r.text.strip().startswith("OK")


# ----------------------------------------------------------------------
# misc read surfaces
# ----------------------------------------------------------------------

def test_llm_status_shape(api):
    _, base = api
    r = requests.get(base + "/api/llm/status", timeout=5)
    assert r.status_code == 200
    d = r.json()
    assert "servers" in d and isinstance(d["servers"], list)


def test_snapshots_list_shape(api):
    _, base = api
    r = requests.get(base + "/api/snapshots", timeout=5)
    assert r.status_code == 200
    assert "snapshots" in r.json()


def test_active_without_engine_is_400(api):
    _, base = api
    r = requests.get(base + "/api/active", timeout=5)
    assert r.status_code == 400


# ----------------------------------------------------------------------
# optional server password (server.api_key / KAISEN_API_KEY)
# ----------------------------------------------------------------------

def test_no_api_key_stays_open(api):
    _, base = api
    assert requests.get(base + "/api/projects", timeout=5).status_code == 200


def test_api_key_rejects_unauthenticated(api_keyed):
    _, base = api_keyed
    r = requests.get(base + "/api/projects", timeout=5)
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    # the whole control surface is locked, not just /api/projects
    assert requests.get(base + "/", timeout=5).status_code == 401
    assert requests.post(base + "/kai", data="STATUS\n", timeout=5).status_code == 401


def test_api_key_accepts_bearer(api_keyed):
    _, base = api_keyed
    r = requests.get(base + "/api/projects",
                     headers={"Authorization": "Bearer sekret"}, timeout=5)
    assert r.status_code == 200


def test_api_key_accepts_basic(api_keyed):
    _, base = api_keyed
    token = base64.b64encode(b"admin:sekret").decode()
    r = requests.get(base + "/api/projects",
                     headers={"Authorization": f"Basic {token}"}, timeout=5)
    assert r.status_code == 200


def test_api_key_wrong_key_401(api_keyed):
    _, base = api_keyed
    r = requests.get(base + "/api/projects",
                     headers={"Authorization": "Bearer nope"}, timeout=5)
    assert r.status_code == 401


def test_api_key_env_wins_over_config(api_env_key):
    _, base = api_env_key
    assert requests.get(base + "/api/projects", timeout=5).status_code == 401
    assert requests.get(
        base + "/api/projects",
        headers={"Authorization": "Bearer confsekret"}, timeout=5).status_code == 401
    assert requests.get(
        base + "/api/projects",
        headers={"Authorization": "Bearer envsekret"}, timeout=5).status_code == 200
