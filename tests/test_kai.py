"""KAI protocol: tolerant parsing, aliases, command grammar, scoping.

All commands are exercised against a FakeClient — no network, no engine.
"""
import pytest

from kaisen.kai import ALIASES, _ALIAS_INDEX, KaiSession, KaiError, _split


# ----------------------------------------------------------------------
# _split — the tolerant line parser (LLMs decorate everything)
# ----------------------------------------------------------------------

def test_split_plain():
    assert _split("STATUS") == ("status", "")
    assert _split("RUN 20") == ("run", "20")
    assert _split("project md5-speed") == ("project", "md5-speed")


def test_split_ok_prefixes():
    assert _split("OK STATUS") == ("status", "")
    assert _split("OK? RUN") == ("run", "")
    assert _split("ok, run 5") == ("run", "5")
    assert _split("OK: PROJECT x") == ("project", "x")


def test_split_err_prefix():
    assert _split("ERR STATUS") == ("status", "")
    assert _split("ERROR: STATUS") == ("status", "")


def test_split_command_labels_and_quotes():
    assert _split("CMD: STATUS") == ("status", "")
    assert _split("command=STATUS") == ("status", "")
    assert _split('"STATUS"') == ("status", "")
    assert _split("`STATUS`") == ("status", "")
    assert _split("*STATUS*") == ("status", "")


def test_split_trailing_punctuation_on_word():
    assert _split("STATUS:") == ("status", "")
    assert _split("RUN:") == ("run", "")


def test_split_case_insensitive():
    assert _split("status") == ("status", "")
    assert _split("Status") == ("status", "")


# ----------------------------------------------------------------------
# ALIASES — every alias must resolve to a real command
# ----------------------------------------------------------------------




def test_canonical_words_indexed():
    for cmd in ALIASES:
        assert cmd.lower() in _ALIAS_INDEX

def test_every_alias_resolves_to_a_command():
    dispatcher_cmds = {"HELP", "QUIT"}
    for word, cmd in _ALIAS_INDEX.items():
        assert cmd in dispatcher_cmds or hasattr(KaiSession, f"cmd_{cmd.lower()}"), \
            f"{word} -> {cmd}"


# ----------------------------------------------------------------------
# Fake client + session
# ----------------------------------------------------------------------

class FakeClient:
    """Scripted KaiClient: routes (method, path, body) -> canned replies."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def call(self, method, path, body=None, read_timeout=120.0):
        self.calls.append((method, path, body))
        key = (method, path.split("?")[0])
        if key in self.routes:
            reply = self.routes[key]
            return reply(self.calls[-1]) if callable(reply) else reply
        return {"error": f"unscripted: {method} {path}"}


def _session(routes):
    return KaiSession(FakeClient(routes))


def _pool_active(pid, **kw):
    entry = {"project_id": pid, "name": pid, "engine_state": "running",
             "generation": 4, "paused": False, "best_fitness": 1.2,
             "best_metrics": {}, "multi": 2, "workers": 3}
    entry.update(kw)
    return {"project_id": pid, "engine_state": "running",
            "state": {"generation": 4, "paused": False,
                      "best": {"fitness": 1.2, "metrics": {"ms": 0.2}}},
            "engines": [entry]}


PROJECTS = {"projects": [{"id": "md5-speed", "name": "MD5 Speed"},
                         {"id": "prime-counter", "name": "Prime Counter"}]}


def test_project_sets_and_validates():
    s = _session({("GET", "/api/projects"): PROJECTS})
    assert "OK project=md5-speed" in s.cmd_project("MD5-SPEED")
    assert s.project == "md5-speed"
    with pytest.raises(KaiError):
        s.cmd_project("does-not-exist")
    with pytest.raises(KaiError):
        s.cmd_project("")


def test_need_project_error():
    s = _session({})
    with pytest.raises(KaiError, match="no project set"):
        s.cmd_spec("")


def test_status_shows_engine_and_pool():
    s = _session({("GET", "/api/active"): _pool_active("md5-speed"),
                  ("GET", "/api/projects"): PROJECTS})
    s.project = "md5-speed"
    spec_routes = {("GET", "/api/projects/md5-speed/spec"): {"spec": {}}}
    s.client.routes.update(spec_routes)
    out = s.cmd_status("")
    assert "OK" in out
    assert "ENGINE running gen=4" in out
    assert "ACTIVE PROJECTS" in out
    assert "*md5-speed" in out  # selected engine marked


# ----------------------------------------------------------------------
# RUN parsing (n, FOR secs, WITH k, ON pid)
# ----------------------------------------------------------------------

def _run_routes(pid):
    return {
        ("GET", "/api/projects"): PROJECTS,
        ("POST", "/api/engine/switch"): {"ok": True, "active_id": pid},
        ("POST", "/api/engine/multi"): {"multi": 3},
        ("GET", "/api/active"): _pool_active(pid),
        ("GET", "/api/iterations"): [{"generation": 1}],
        ("POST", "/api/engine/pause"): {"ok": True},
    }


def test_run_forever_default():
    s = _session(_run_routes("md5-speed"))
    s.project = "md5-speed"
    out = s.cmd_run("")
    assert "forever" in out
    goal = s._run_goal
    assert goal["pid"] == "md5-speed"
    assert goal["gen_target"] is None and goal["ts_deadline"] is None


def test_run_generation_target():
    s = _session(_run_routes("md5-speed"))
    s.project = "md5-speed"
    out = s.cmd_run("20")
    assert "20 generations" in out
    assert s._run_goal["gen_target"] == 20


def test_run_budget_and_multi():
    s = _session(_run_routes("md5-speed"))
    s.project = "md5-speed"
    out = s.cmd_run("RUN FOR 300 WITH 3")
    goal = s._run_goal
    assert goal["ts_deadline"] is not None
    assert "300s" in out and "3 LLMs" in out
    # multi was POSTed to the engine endpoint with the k value
    assert ("POST", "/api/engine/multi") in [c[:2] for c in s.client.calls]


def test_run_on_pid_overrides_session():
    s = _session(_run_routes("prime-counter"))
    # session project is md5-speed; ON switches the goal to prime-counter
    s.project = "md5-speed"
    s.cmd_run("5 ON prime-counter")
    assert s._run_goal["pid"] == "prime-counter"
    switch = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/engine/switch"))
    assert switch[2] == {"project_id": "prime-counter"}


def test_run_requires_project():
    s = _session(_run_routes("md5-speed"))
    with pytest.raises(KaiError, match="no project set"):
        s.cmd_run("5")


# ----------------------------------------------------------------------
# FORGE parsing (n, ON pid, TIER, GOAL words)
# ----------------------------------------------------------------------

def _forge_routes(pid):
    return {
        ("GET", "/api/projects"): PROJECTS,
        ("POST", "/api/swarm/start"): {"job_id": "j1"},
        ("GET", "/api/swarm/j1"): {"job": {"state": "done",
                                           "results": [{"rank": 1, "ok": True,
                                                        "metrics": {"ms": 0.1}}]}},
    }


def test_forge_defaults():
    s = _session(_forge_routes("md5-speed"))
    s.project = "md5-speed"
    out = s.cmd_forge("GOAL make the MD5 loop faster")
    assert "1 draft(s)" in out
    start = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/swarm/start"))
    body = start[2]
    assert body["kind"] == "code_forge"
    assert body["n"] == 3 and body["min_tier"] == "tiny"
    assert body["project_id"] == "md5-speed"
    assert "make the MD5 loop faster" in body["request"]


def test_forge_n_tier_on():
    s = _session(_forge_routes("prime-counter"))
    s.project = "md5-speed"
    s.cmd_forge("7 TIER small ON prime-counter GOAL do better")
    start = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/swarm/start"))
    body = start[2]
    assert body["n"] == 7 and body["min_tier"] == "small"
    assert body["project_id"] == "prime-counter"
    assert body["request"] == "do better"


def test_forge_caps_n_at_12():
    s = _session(_forge_routes("md5-speed"))
    s.project = "md5-speed"
    s.cmd_forge("99 GOAL x")
    start = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/swarm/start"))
    assert start[2]["n"] == 12


# ----------------------------------------------------------------------
# block commands (CANDIDATE / BASELINE)
# ----------------------------------------------------------------------

def test_candidate_requires_code():
    s = _session({})
    s.project = "md5-speed"
    with pytest.raises(KaiError):
        s.cmd_candidate("", [""])

# ----------------------------------------------------------------------
# TEMP flag — quick runs never touch the real setup
# ----------------------------------------------------------------------

def test_goal_temp_flag_parsed():
    routes = {
        ("POST", "/api/projects/suggest"): {"ok": True,
                                            "suggested_spec": {"name": "x"},
                                            "validation": {"rounds": 1}},
    }
    s = _session(routes)
    out = s.cmd_goal("make it fast TEMP")
    assert s._last_temp is True
    assert "TEMP" in out
    sent = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/projects/suggest"))
    assert "make it fast" in sent[2]["goal"]
    assert "TEMP" not in sent[2]["goal"]


def test_goal_without_temp_flag():
    routes = {("POST", "/api/projects/suggest"): {"ok": True,
                                                  "suggested_spec": {"name": "x"},
                                                  "validation": {"rounds": 1}}}
    s = _session(routes)
    s.cmd_goal("make it fast")
    assert s._last_temp is False


def test_accept_carries_temp():
    routes = {
        ("GET", "/api/projects"): PROJECTS,
        ("POST", "/api/projects/suggest"): {"ok": True,
                                            "suggested_spec": {"name": "x"},
                                            "validation": {"rounds": 1}},
        ("POST", "/api/projects"): {"ok": True, "project": {"id": "tp", "name": "x"}},
    }
    s = _session(routes)
    s.cmd_goal("make it fast TEMP")
    out = s.cmd_accept("tp")
    assert "TEMP" in out
    sent = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/projects"))
    assert sent[2].get("temp") is True


def test_create_temp_flag():
    spec_json = '{"name": "x", "steps": {}, "metrics": {}}'
    routes = {("POST", "/api/projects"): {"ok": True, "project": {"id": "tp", "name": "x"}}}
    s = _session(routes)
    out = s.cmd_create(f"tp TEMP {spec_json}")
    assert "TEMP" in out
    sent = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/projects"))
    assert sent[2].get("temp") is True
    assert sent[2]["spec"]["name"] == "x"


def test_create_without_temp_flag():
    spec_json = '{"name": "x", "steps": {}, "metrics": {}}'
    routes = {("POST", "/api/projects"): {"ok": True, "project": {"id": "tp", "name": "x"}}}
    s = _session(routes)
    s.cmd_create(f"tp {spec_json}")
    sent = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/projects"))


def test_autofix_command_parses_and_posts():
    routes = {
        ("GET", "/api/projects"): PROJECTS,
        ("POST", "/api/engine/autofix"): {"ok": True, "project_id": "md5-speed",
                                          "settings": {"max_tries": 10, "repair_max": 5},
                                          "effective": {"max_tries": 10, "repair_max": 5}},
    }
    s = _session(routes)
    s.project = "md5-speed"
    out = s.cmd_autofix("tries 10 repair 5")
    assert "autofix turns 10" in out and "5 LLM repair" in out
    sent = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/engine/autofix"))
    assert sent[2] == {"project_id": "md5-speed", "tries": 10, "repair": 5}


def test_autofix_repair_off_parses():
    routes = {
        ("GET", "/api/projects"): PROJECTS,
        ("POST", "/api/engine/autofix"): {"ok": True, "project_id": "md5-speed",
                                          "settings": {"max_tries": None, "repair_max": 0},
                                          "effective": {"max_tries": 5, "repair_max": 0}},
    }
    s = _session(routes)
    s.project = "md5-speed"
    out = s.cmd_autofix("repair off")
    assert "deterministic only" in out
    sent = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/engine/autofix"))
    assert sent[2] == {"project_id": "md5-speed", "repair": 0}


def test_candidate_accepts_code_block():
    s = _session({("GET", "/api/projects"): PROJECTS,
                  ("GET", "/api/active"): _pool_active("md5-speed"),
                  ("POST", "/api/queue/custom_code"): {"ok": True, "generation": 7}})
    s.project = "md5-speed"
    out = s.cmd_candidate("c", ["int main(void){return 0;}"])
    assert "OK" in out
    posted = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/queue/custom_code"))
    assert "int main" in posted[2].get("code", "")
    assert posted[2]["project_id"] == "md5-speed"


def test_baseline_stages_code_for_goal():
    s = _session({})
    s.project = "md5-speed"
    out = s.cmd_baseline("c", ["int x;", "int main(void){return 0;}"])
    assert "OK" in out
    assert s._baseline_code and "int x;" in s._baseline_code


def test_baseline_empty_block_rejected():
    s = _session({})
    with pytest.raises(KaiError):
        s.cmd_baseline("", [])


# ----------------------------------------------------------------------
# _run_summary scoping: per-project history, not the selected engine
# ----------------------------------------------------------------------

def test_run_summary_uses_goal_pid():
    def iterations(c):
        if "prime-counter" in c[1]:
            return [{"generation": 1}]           # goal project: 1 scored
        return [{"generation": 1}, {"generation": 2}]  # selected: 2 scored

    def active(c):
        # both engines in the pool; the SELECTED one is md5-speed
        md5 = {"project_id": "md5-speed", "name": "md5-speed",
               "engine_state": "running", "generation": 4, "paused": False,
               "best_fitness": 1.2, "best_metrics": {}, "multi": 2, "workers": 3}
        pc = {"project_id": "prime-counter", "name": "prime-counter",
              "engine_state": "running", "generation": 9, "paused": False,
              "best_fitness": 3.3, "best_metrics": {}, "multi": 1, "workers": 2}
        return {"project_id": "md5-speed", "engine_state": "running",
                "state": {"generation": 4, "paused": False, "best": {}},
                "engines": [md5, pc]}

    s = _session({("GET", "/api/iterations"): iterations,
                  ("GET", "/api/active"): active})
    goal = {"pid": "prime-counter", "gen_target": 3, "ts_deadline": None,
            "start_gen": 0, "start_hist": 0, "start_best": None}
    out = s._run_summary(goal, done=True)
    # the summary reported the GOAL project's engine numbers (3.3, 1 scored),
    # not the selected engine's (1.2, 2 scored)
    assert "3.3" in out and "1.2" not in out
    assert "1 generation(s) scored" in out
