"""KAI protocol: tolerant parsing, aliases, command grammar, scoping.

All commands are exercised against a FakeClient — no network, no engine.
"""
import time

import pytest

from kaisen.kai import (ALIASES, _ALIAS_INDEX, KaiSession, KaiError, _split,
                        run_lines)


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
             "best_metrics": {}, "parallel_gens": 2, "workers": 3}
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
        ("POST", "/api/engine/parallel_gens"): {"parallel_gens": 3},
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


def test_run_budget_and_parallel_gens():
    s = _session(_run_routes("md5-speed"))
    s.project = "md5-speed"
    out = s.cmd_run("RUN FOR 300 WITH 3")
    goal = s._run_goal
    assert goal["ts_deadline"] is not None
    assert "300s" in out and "3 parallel generations" in out
    # the generation count was POSTed to the engine endpoint with the k value
    assert ("POST", "/api/engine/parallel_gens") in [c[:2] for c in s.client.calls]


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


def _parallel_gens_pool_routes():
    """Two-engine pool: md5-speed (2 parallel generations) +
    prime-counter (1)."""
    md5 = {"project_id": "md5-speed", "name": "md5-speed",
           "engine_state": "running", "generation": 4, "paused": False,
           "best_fitness": 1.2, "best_metrics": {}, "parallel_gens": 2, "workers": 3,
           "spec_revision": "abc", "autofix": {"max_tries": 5, "repair_max": 3},
           "valid_rate": {"valid_rate": 0.5, "outcome_counts": {}}, "fuzzy_top_n": 0}
    pc = {"project_id": "prime-counter", "name": "prime-counter",
          "engine_state": "running", "generation": 9, "paused": False,
          "best_fitness": 3.3, "best_metrics": {}, "parallel_gens": 1, "workers": 2,
          "spec_revision": "def", "autofix": {"max_tries": 5, "repair_max": 3},
          "valid_rate": {"valid_rate": 0.8, "outcome_counts": {}}, "fuzzy_top_n": 0}
    routes = {
        ("GET", "/api/projects"): PROJECTS,
        ("POST", "/api/engine/switch"): {"ok": True},
        ("POST", "/api/engine/parallel_gens"): {"parallel_gens": 2},
        ("GET", "/api/active"): {"project_id": "md5-speed", "engine_state": "running",
                                  "state": {"generation": 4, "paused": False, "best": {}},
                                  "engines": [md5, pc]},
        ("GET", "/api/iterations"): [],
        ("POST", "/api/engine/pause"): {"ok": True},
    }
    return routes


def test_run_all_starts_every_pool_member():
    s = _session(_parallel_gens_pool_routes())
    s.project = "md5-speed"
    out = s.cmd_run_all("FOR 300 WITH 2")
    assert len(s._run_goals) == 2
    assert all(g["ts_deadline"] is not None and g["gen_target"] is None
               for g in s._run_goals)
    assert s._run_goal is None
    assert "2 pool projects" in out and "budget 300s" in out
    # every engine got its switch + parallel-gens call
    switches = [c for c in s.client.calls if c[0:2] == ("POST", "/api/engine/switch")]
    assert len(switches) == 2


def test_run_all_without_budget_is_forever():
    s = _session(_parallel_gens_pool_routes())
    s.project = "md5-speed"
    s.cmd_run_all("")
    assert len(s._run_goals) == 2
    assert all(g["ts_deadline"] is None for g in s._run_goals)


def test_run_all_via_run_prefix():
    s = _session(_parallel_gens_pool_routes())
    s.project = "md5-speed"
    s.cmd_run("ALL FOR 300")
    assert len(s._run_goals) == 2


def test_budget_pool_table():
    s = _session(_parallel_gens_pool_routes())
    s._run_goals = [
        {"pid": "md5-speed", "gen_target": None, "ts_deadline": time.time() + 60,
         "start_gen": 0, "start_hist": 0, "start_best": None},
        {"pid": "prime-counter", "gen_target": None, "ts_deadline": time.time() + 120,
         "start_gen": 0, "start_hist": 0, "start_best": None},
    ]
    out = s.cmd_budget("")
    assert "2 runs in flight" in out
    assert "md5-speed: 0 scored" in out
    assert "prime-counter: 0 scored" in out


def test_wait_all_completes_all():
    s = _session(_parallel_gens_pool_routes())
    s._run_goals = [
        {"pid": "md5-speed", "gen_target": None, "ts_deadline": time.time() - 10,
         "start_gen": 0, "start_hist": 0, "start_best": None},
        {"pid": "prime-counter", "gen_target": None, "ts_deadline": time.time() - 10,
         "start_gen": 0, "start_hist": 0, "start_best": None},
    ]
    out = s.cmd_wait("")
    assert "all 2 runs complete" in out
    assert s._run_goals == [] and s._run_goal is None


def test_pool_goals_persist(tmp_path, monkeypatch):
    monkeypatch.setattr("kaisen.kai._RUNS_FILE", tmp_path / "kai_runs.json")
    s = _session(_parallel_gens_pool_routes())
    s._run_goals = [
        {"pid": "a", "gen_target": None, "ts_deadline": None,
         "start_gen": 0, "start_hist": 0, "start_best": None},
        {"pid": "b", "gen_target": None, "ts_deadline": None,
         "start_gen": 0, "start_hist": 0, "start_best": None},
    ]
    s._save_run_state()
    s2 = _session(_parallel_gens_pool_routes())
    assert len(s2._run_goals) == 2 and s2._run_goal is None


def test_status_shows_utilization_line():
    routes = {
        ("GET", "/api/active"): _pool_active("md5-speed"),
        ("GET", "/api/projects"): PROJECTS,
        ("GET", "/api/llm/status"): {"servers": [
            {"id": "s1", "enabled": True, "online": True, "max_concurrent": 4, "inflight": 1},
            {"id": "s2", "enabled": True, "online": True, "max_concurrent": 8, "inflight": 2},
        ]},
    }
    s = _session(routes)
    s.project = "md5-speed"
    out = s.cmd_status("")
    assert "LLM PIPELINES 2/12 (3 in flight)" in out


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
               "best_fitness": 1.2, "best_metrics": {}, "parallel_gens": 2, "workers": 3}
        pc = {"project_id": "prime-counter", "name": "prime-counter",
              "engine_state": "running", "generation": 9, "paused": False,
              "best_fitness": 3.3, "best_metrics": {}, "parallel_gens": 1, "workers": 2}
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


# ----------------------------------------------------------------------
# FACTORY — registration payload shape (files must ride inside the spec)
# ----------------------------------------------------------------------

def test_factory_posts_files_inside_spec(monkeypatch):
    """Regression: the server reads bundled files from spec["files"]; a
    top-level "files" key is silently dropped and ships an empty harness
    (observed live: 40 registered projects, zero files on disk)."""
    import kaisen.factory as FA
    row = {"id": "popcount-c", "ok": True, "error": "",
           "spec": {"id": "popcount-c", "name": "P (C)", "language": "c",
                    "artifact_name": "program"},
           "files": {"harness/build.py": "#!/usr/bin/env python3\nprint('OK')\n"}}
    monkeypatch.setattr(FA, "create_all", lambda **kw: [row])

    posted = {}

    def _post(call):
        posted.update(call[2] or {})
        return {"ok": True, "project": {"id": "popcount-c", "name": "P (C)"}}

    s = _session({("GET", "/api/projects"): {"projects": []},
                  ("POST", "/api/projects"): _post})
    out = s.dispatch("FACTORY")
    assert out.startswith("OK factory: 1 created"), out
    assert "files" not in posted, \
        "top-level files key is silently dropped by the server"
    assert posted["spec"].get("files") == row["files"]


# ----------------------------------------------------------------------
# LOGS — the engine-log tail command
# ----------------------------------------------------------------------

def test_logs_default_path_and_lines():
    """LOGS with no args hits /api/engine/logs (no query) and returns the
    engine's lines with a count header."""
    s = _session({("GET", "/api/engine/logs"): {"ok": True, "project_id": "md5-speed",
                                                "lines": ["[..] gen 3: ok", "[..] gen 4: valid"]}})
    out = s.cmd_logs("")
    assert out.startswith("OK 2 line(s)"), out
    assert "gen 4" in out


def test_logs_parses_pid_lines_grep():
    """LOGS pid lines N grep STR builds the right query string."""
    s = _session({("GET", "/api/engine/logs"): {"ok": True, "project_id": "fib-mod-rust",
                                                "lines": ["[..] LLM repair applied"]}})
    out = s.cmd_logs("fib-mod-rust lines 300 grep repair")
    # path carries project_id, lines and grep, URL-encoded
    path = s.client.calls[0][1]
    assert "project_id=fib-mod-rust" in path
    assert "lines=300" in path
    assert "grep=repair" in path
    assert "LLM repair applied" in out


def test_logs_bare_word_is_project_id():
    s = _session({("GET", "/api/engine/logs"): {"ok": True, "project_id": "prime-counter",
                                                "lines": ["[..] done"]}})
    s.cmd_logs("prime-counter")
    assert "project_id=prime-counter" in s.client.calls[0][1]


def test_logs_empty_reply():
    s = _session({("GET", "/api/engine/logs"): {"ok": True, "lines": []}})
    out = s.cmd_logs("")
    assert "no log lines" in out


def test_logs_error_propagates():
    s = _session({("GET", "/api/engine/logs"): {"ok": False, "error": "no engine running"}})
    with pytest.raises(KaiError, match="no engine running"):
        s.cmd_logs("")


# ----------------------------------------------------------------------
# MODELCHECK — the streaming-path diagnostic
# ----------------------------------------------------------------------

def test_modelcheck_ok():
    s = _session({("POST", "/api/servers/modelcheck/qwen"): {
        "ok": True, "chars": 5, "first_token_s": 1.2, "ttft_s": 1.1,
        "total_s": 2.0, "prefill_tps": 90.0, "reply": "ok"}})
    out = s.cmd_modelcheck("qwen")
    assert "OK modelcheck qwen" in out
    assert "first_token=1.2s" in out
    assert "OK" in out  # verdict


def test_modelcheck_empty_flags_bug():
    """Endpoint answers but the stream carries no content -> flag it. This
    is the 'generates in the model log but the KAISEN chat stays empty'
    symptom, surfaced directly instead of looking like a hang."""
    s = _session({("POST", "/api/servers/modelcheck/qwen"): {
        "ok": True, "chars": 0, "empty": True, "reply": ""}})
    out = s.cmd_modelcheck("qwen")
    assert "EMPTY" in out


def test_modelcheck_slow_prefill_warns():
    s = _session({("POST", "/api/servers/modelcheck/qwen"): {
        "ok": True, "chars": 10, "first_token_s": 90.0, "reply": "x"}})
    out = s.cmd_modelcheck("qwen")
    assert "SLOW prefill" in out


def test_modelcheck_defaults_to_first_active():
    s = _session({("GET", "/api/config"): {"llm": {"active_ids": ["qwen"]}},
                  ("POST", "/api/servers/modelcheck/qwen"): {"ok": True, "chars": 2}})
    out = s.cmd_modelcheck("")
    assert "modelcheck qwen" in out


def test_modelcheck_no_server_error():
    s = _session({("GET", "/api/config"): {"llm": {"active_ids": []}}})
    with pytest.raises(KaiError, match="no active server"):
        s.cmd_modelcheck("")


# ----------------------------------------------------------------------
# BUDGET SERVER — per-server usage budget
# ----------------------------------------------------------------------

def test_budget_server_status_single():
    s = _session({("GET", "/api/servers/budget/qwen"): {
        "ok": True, "budget": {"configured": True, "exhausted": False,
                               "tokens_used": 250000, "max_tokens": 1000000,
                               "generations_used": 12, "max_generations": 50,
                               "window_reset_in_s": 3600.0}}})
    out = s.cmd_budget("SERVER qwen")
    assert "OK qwen budget" in out
    assert "OK" in out
    assert "250,000/1,000,000" in out


def test_budget_server_unconfigured_message():
    s = _session({("GET", "/api/servers/budget/qwen"): {
        "ok": True, "budget": {"configured": False}}})
    out = s.cmd_budget("SERVER qwen")
    assert "not configured" in out


def test_budget_server_set_posts_patch():
    s = _session({("POST", "/api/servers/budget/qwen"): {
        "ok": True, "budget": {"configured": True, "exhausted": False,
                               "tokens_used": 0, "max_tokens": 1000000,
                               "max_generations": None, "window_reset_in_s": 10800.0}}})
    s.cmd_budget("SERVER qwen SET max_tokens 1M reset 3h")
    # the patch body carries the parsed-friendly human values, sent raw
    patch = s.client.calls[0][2]
    assert patch["max_tokens"] == "1M"
    assert patch["reset"] == "3h"


def test_budget_server_list_all():
    cfg = {"llm": {"servers": [
        {"id": "qwen", "budget": {"configured": True, "tokens_used": 5,
                                  "max_tokens": 1000, "max_generations": None,
                                  "window_reset_in_s": 10.0}},
        {"id": "gpt", "budget": {}},
    ]}}
    s = _session({("GET", "/api/config"): cfg})
    out = s.cmd_budget("SERVER")
    assert "2 server(s) budget" in out
    assert "qwen" in out and "gpt" in out


def test_budget_server_set_requires_sid_and_limit():
    s = _session({})
    with pytest.raises(KaiError, match="SET <sid>.*max_tokens"):
        s.cmd_budget("SERVER SET max_tokens 1M")


# ----------------------------------------------------------------------
# GEN — the full per-generation log (raw LLM + extracted program)
# ----------------------------------------------------------------------

def test_gen_returns_all_fields():
    s = _session({("GET", "/api/projects/fib-mod-rust/gen/246"): {
        "ok": True, "project_id": "fib-mod-rust", "generation": 246,
        "language": "rust", "outcome": "valid", "fitness": 4.2,
        "prompt": "Compute fib...", "llm_raw": "```rust\nfn main(){}\n```",
        "candidate": "fn main(){}", "diff": "{...}"}})
    s.project = "fib-mod-rust"
    out = s.cmd_gen("246")
    assert "OK fib-mod-rust gen 246" in out
    assert "RAW LLM REPLY" in out and "EXTRACTED PROGRAM" in out and "DIFF" in out
    assert "fn main(){}" in out


def test_gen_field_selector():
    s = _session({("GET", "/api/projects/fib-mod-rust/gen/246"): {
        "ok": True, "project_id": "fib-mod-rust", "generation": 246,
        "candidate": "fn main(){}", "llm_raw": "thinking...", "prompt": "P", "diff": "D"}})
    s.project = "fib-mod-rust"
    out = s.cmd_gen("246 CODE")
    assert "EXTRACTED PROGRAM" in out and "RAW LLM REPLY" not in out


def test_gen_requires_number():
    s = _session({})
    with pytest.raises(KaiError, match="GEN <n>"):
        s.cmd_gen("")


def test_gen_on_pid_overrides_session():
    s = _session({("GET", "/api/projects/prime-counter/gen/42"): {
        "ok": True, "project_id": "prime-counter", "generation": 42,
        "candidate": "x"}})
    s.project = "md5-speed"
    out = s.cmd_gen("42 ON prime-counter")
    assert "prime-counter" in out
    assert s.client.calls[0][1].endswith("/prime-counter/gen/42")


# ----------------------------------------------------------------------
# TELEGRAM — channel setup without putting the token in the transcript
# ----------------------------------------------------------------------

def test_telegram_status_reports_source_and_readiness():
    s = _session({("GET", "/api/config"): {"telegram": {
        "token_set": True, "token_source": "secrets.json", "chat_id": "42"}}})
    out = s.cmd_telegram("")
    assert "token set (from secrets.json)" in out
    assert "chat 42" in out and "ready to send" in out


def test_telegram_status_says_incomplete_without_chat():
    s = _session({("GET", "/api/config"): {"telegram": {
        "token_set": True, "token_source": "env", "chat_id": ""}}})
    assert "incomplete" in s.cmd_telegram("STATUS")


def test_telegram_load_imports_the_env_token():
    s = _session({
        ("POST", "/api/telegram/load_env"): {"ok": True},
        ("GET", "/api/config"): {"telegram": {"token_set": True, "token_source": "env",
                                              "chat_id": "-100"}},
    })
    out = s.cmd_telegram("LOAD")
    assert "loaded from the environment" in out and "from env" in out
    assert "chat -100" in out, "LOAD imports the chat id too"
    assert ("POST", "/api/telegram/load_env", {}) in s.client.calls


def test_telegram_load_without_env_reports_the_error():
    s = _session({("POST", "/api/telegram/load_env"):
                  {"ok": False, "error": "KAISEN_TG_TOKEN is not set"}})
    out = s.dispatch("TELEGRAM LOAD")
    assert out.startswith("ERR") and "KAISEN_TG_TOKEN is not set" in out


def test_telegram_check_reports_the_bot_or_the_rejection():
    s = _session({("POST", "/api/telegram/check"): {"ok": True, "username": "kaisen_bot"}})
    assert s.cmd_telegram("CHECK") == "OK telegram token works — @kaisen_bot"
    bad = _session({("POST", "/api/telegram/check"): {"ok": False, "error": "Unauthorized"}})
    assert bad.dispatch("TELEGRAM CHECK") == "ERR telegram token rejected: Unauthorized"


def test_telegram_refuses_the_chat_id_too():
    """The chat id is stored as a secret like the token — typing it into a
    session would put it in the transcript, so the command refuses and names
    the env path instead."""
    s = _session({})
    err = s.dispatch("TELEGRAM CHAT -100123")
    assert err.startswith("ERR")
    assert "KAISEN_TG_CHAT_ID" in err and "TELEGRAM LOAD" in err
    assert not s.client.calls


def test_telegram_refuses_a_token_typed_through_kai():
    """A token typed into a KAI session lands in the transcript: the command
    must refuse it, name the right path, and write nothing."""
    s = _session({})
    err = s.dispatch("TELEGRAM TOKEN 123456:ABC")
    assert err.startswith("ERR")
    assert "KAISEN_TG_TOKEN" in err and "TELEGRAM LOAD" in err
    assert not any(c[0] == "PUT" for c in s.client.calls)


# ----------------------------------------------------------------------
# SUCCESS — the goal: criterion, actions, custom message, attachments
# ----------------------------------------------------------------------

def _goal_session(goal=None, put_ok=True, active=None):
    """A session whose project 'demo' carries `goal`, with PUT scripted."""
    spec = {"id": "demo", "name": "Demo",
            "steps": {"build": {"program": "gcc", "args": []}, "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}
    if goal is not None:
        spec["goal"] = goal

    def put(call):
        if put_ok:
            return {"ok": True, "spec": call[2]["spec"]}
        return {"ok": False,
                "error": "invalid project spec: goal.message: unknown variable {nope}"}

    s = _session({("GET", "/api/projects/demo/spec"): {"spec": spec},
                  ("PUT", "/api/projects/demo/spec"): put,
                  ("GET", "/api/active"): active if active is not None else _pool_active("demo")})
    s.project = "demo"
    return s


def _last_spec(s):
    return [c for c in s.client.calls if c[0] == "PUT"][-1][2]["spec"]


def test_success_without_a_goal_says_how_to_set_one():
    out = _goal_session().cmd_success("")
    assert out.startswith("OK") and "has no goal" in out
    assert "SUCCESS <metric> <op> <value>" in out


def test_success_sets_criterion_and_actions():
    s = _goal_session()
    out = s.cmd_success("ms <= 10 THEN telegram,stop")
    assert out == "OK demo: goal ms <= 10 THEN telegram, stop (message 0 line(s))" or \
        out.startswith("OK demo: goal ms <= 10 THEN telegram, stop")
    assert _last_spec(s)["goal"] == {"when": {"metric": "ms", "op": "<=", "value": 10},
                                     "then": ["telegram", "stop"]}


def test_success_changes_only_the_actions_with_then():
    s = _goal_session({"when": {"metric": "ms", "op": "<=", "value": 10}, "then": ["stop"]})
    s.cmd_success("THEN stop,ping,telegram")
    assert _last_spec(s)["goal"] == {"when": {"metric": "ms", "op": "<=", "value": 10},
                                     "then": ["stop", "ping", "telegram"]}


def test_success_message_one_line():
    s = _goal_session({"when": {"metric": "ms", "op": "<=", "value": 10}, "then": ["telegram"]})
    s.cmd_success("MESSAGE done at gen {generation}")
    assert _last_spec(s)["goal"]["message"] == "done at gen {generation}"


def test_success_message_block_and_the_next_command_still_runs():
    """`SUCCESS MESSAGE` + lines + END sets a multi-line message — and must
    NOT swallow the commands that follow it."""
    s = _goal_session({"when": {"metric": "ms", "op": "<=", "value": 10}, "then": ["telegram"]})
    out = run_lines(s, ["SUCCESS MESSAGE", "hello {project}", "second line", "END", "STATUS"])
    assert _last_spec(s)["goal"]["message"] == "hello {project}\nsecond line"
    assert any(c[0] == "GET" and c[1] == "/api/active" for c in s.client.calls), \
        "STATUS after the block must have run"
    assert "OK" in out


def test_success_attach_clear_and_off():
    s = _goal_session({"when": {"metric": "ms", "op": "<=", "value": 10},
                       "then": ["telegram"], "attach": ["champion"]})
    s.cmd_success("ATTACH champion,llm_output,prompt")
    assert _last_spec(s)["goal"]["attach"] == ["champion", "llm_output", "prompt"]
    s.cmd_success("ATTACH none")
    assert "attach" not in _last_spec(s)["goal"]
    s.cmd_success("CLEAR")
    assert "attach" not in _last_spec(s)["goal"] and "message" not in _last_spec(s)["goal"]
    assert "goal removed" in s.dispatch("SUCCESS OFF")
    assert "goal" not in _last_spec(s)


def test_success_shows_met_generation_and_date():
    met = _pool_active("demo", goal={"met": True, "met_generation": 7,
                                     "met_at": 1789660000, "detail": "ms <= 10 (seen 9)"})
    s = _goal_session({"when": {"metric": "ms", "op": "<=", "value": 10}, "then": ["stop"]},
                      active=met)
    out = s.cmd_success("")
    assert "MET gen 7 on 2026-09-17" in out


def test_success_surfaces_a_validation_error():
    """A bad variable ends as ERR from the server — never a message with a
    blank spot."""
    s = _goal_session({"when": {"metric": "ms", "op": "<=", "value": 10}, "then": ["telegram"]},
                      put_ok=False)
    err = s.dispatch("SUCCESS MESSAGE hello {nope}")
    assert err.startswith("ERR") and "unknown variable {nope}" in err


def test_success_message_requires_the_telegram_action():
    s = _goal_session({"when": {"metric": "ms", "op": "<=", "value": 10}, "then": ["stop"]})
    err = s.dispatch("SUCCESS MESSAGE hi")
    assert err.startswith("ERR") and "telegram" in err
    assert not [c for c in s.client.calls if c[0] == "PUT"], "nothing may be written"


def test_success_rejects_a_bad_value_and_unknown_verb():
    s = _goal_session()
    assert s.dispatch("SUCCESS ms <= ten").startswith("ERR")
    assert s.dispatch("SUCCESS wobble").startswith("ERR")
