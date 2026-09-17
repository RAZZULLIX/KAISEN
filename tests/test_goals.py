"""Project goals: a success condition and the actions it fires.

A spec may declare ``goal.when`` (one comparison against the champion's
metrics) and ``goal.then`` (actions — default: stop the project + ping).
When the champion meets it the project STOPS and is not resumed on the next
start; the ping reports it.  Editing the goal re-arms a project instead of
leaving it latched as done forever.
"""
import pytest

from kaisen import goals
from kaisen.engine import STATE_RUNNING, STATE_STOPPED, ProjectEngine
from kaisen.llm import ModelOrchestrator
from kaisen.projects import ProjectRegistry, validate_spec
from kaisen.workers import reset_worker_pool

GOAL = {"when": {"metric": "proved_open", "op": ">=", "value": 2}}


@pytest.fixture(autouse=True)
def _fresh_pool():
    reset_worker_pool()
    yield
    reset_worker_pool()


def _spec(pid, goal=None):
    spec = {
        "id": pid, "name": f"Proj {pid}", "language": "python",
        "steps": {
            "build": {"program": "harness/build.py", "args": ["{candidate}", "{artifact}"]},
            "verify": [], "score": [],
        },
        "metrics": {"proved_open": {"label": "proved", "direction": "higher", "weight": 1.0}},
        "data": {"baseline_source": "baseline.py"},
    }
    if goal is not None:
        spec["goal"] = goal
    return spec


def _mk_project(registry, pid, goal=None):
    p = registry.create(pid, _spec(pid, goal))
    (p.path / "baseline.py").write_text("print(0)\n", encoding="utf-8")
    return p


def _mk_engine(project, registry, cfg):
    eng = ProjectEngine(project, orchestrator=ModelOrchestrator(cfg),
                        registry=registry, worker_count=0)
    eng._set_state(STATE_RUNNING)
    return eng


def _spy_pings(monkeypatch):
    pings = []

    def fake_send(message, max_len=None):
        pings.append(message)
        return {"ok": False}          # no pin attempt, no real channel

    monkeypatch.setattr("kaisen.engine.send_message", fake_send)
    return pings


def _goal_pings(pings):
    return [m for m in pings if "GOAL MET" in m]


# --------------------------------------------------------------------------- #
# condition semantics
# --------------------------------------------------------------------------- #

def test_condition_ops_and_boundaries():
    higher = {"when": {"metric": "proved_open", "op": ">=", "value": 2}}
    assert goals.evaluate(higher, {"proved_open": 2}, None, 5)      # boundary counts
    assert goals.evaluate(higher, {"proved_open": 3}, None, 5)
    assert goals.evaluate(higher, {"proved_open": 1.5}, None, 5) is None

    lower = {"when": {"metric": "ms", "op": "<=", "value": 10}}
    assert goals.evaluate(lower, {"ms": 10}, None, 1)
    assert goals.evaluate(lower, {"ms": 10.5}, None, 1) is None


def test_unmeasured_metric_never_fires():
    """A metric this evaluation did not measure is not met — never an error.
    The first evaluations of a project legitimately lack some metrics."""
    assert goals.evaluate(GOAL, {}, None, 5) is None
    assert goals.evaluate({"when": {"metric": "fitness", "op": "<=", "value": 10}},
                          {}, None, 5) is None


def test_reserved_metrics():
    """fitness and generation are addressable without being declared."""
    assert goals.evaluate({"when": {"metric": "fitness", "op": "<=", "value": 4.5}},
                          {}, 4.0, 7)
    assert goals.evaluate({"when": {"metric": "generation", "op": ">=", "value": 50}},
                          {}, None, 50)
    assert goals.evaluate({"when": {"metric": "generation", "op": ">=", "value": 50}},
                          {}, None, 49) is None


def test_default_actions_are_stop_then_ping():
    """A goal with no `then` stops the project and pings — the default the
    user asked for.  An empty list means the same thing, so a spec that was
    round-tripped through a UI cannot lose its stop action; no goal at all
    means no actions."""
    assert goals.actions_of(GOAL) == ["stop", "ping"]
    assert goals.actions_of({"when": GOAL["when"], "then": []}) == ["stop", "ping"]
    assert goals.actions_of({"when": GOAL["when"], "then": ["ping"]}) == ["ping"]
    assert goals.actions_of({"when": GOAL["when"], "then": "stop"}) == ["stop"]
    assert goals.actions_of({}) == []
    assert goals.actions_of({"when": None, "then": []}) == []


def test_editing_the_goal_produces_a_new_signature():
    """The latch is keyed by signature, so raising the target re-arms the
    project instead of leaving it marked done."""
    stricter = {"when": {"metric": "proved_open", "op": ">=", "value": 3}}
    assert goals.signature(GOAL) != goals.signature(stricter)
    assert goals.signature(GOAL) == goals.signature(dict(GOAL))


# --------------------------------------------------------------------------- #
# spec validation
# --------------------------------------------------------------------------- #

def test_spec_validation_catches_written_wrong_goals():
    assert validate_spec(_spec("ok", GOAL)) == []
    assert validate_spec(_spec("none")) == []

    typo = validate_spec(_spec("typo", {"when": {"metric": "proved", "op": ">=", "value": 1}}))
    assert any("unknown metric 'proved'" in e for e in typo)

    bad_op = validate_spec(_spec("op", {"when": {"metric": "proved_open", "op": "=>", "value": 1}}))
    assert any("goal.when.op" in e for e in bad_op)

    bad_value = validate_spec(_spec("val", {"when": {"metric": "proved_open", "op": ">=", "value": "two"}}))
    assert any("goal.when.value" in e for e in bad_value)

    # A half-written goal must fail loudly rather than silently never fire.
    unknown_action = validate_spec(_spec("act", {"when": GOAL["when"], "then": ["notify"]}))
    assert any("unknown action 'notify'" in e for e in unknown_action)
    no_condition = validate_spec(_spec("cond", {"then": ["stop"]}))
    assert any("needs goal.when" in e for e in no_condition)


def test_project_without_goal_has_no_goal_snapshot(tmp_path, tmp_cfg):
    registry = ProjectRegistry(tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    p = _mk_project(registry, "plain")
    eng = _mk_engine(p, registry, tmp_cfg)
    assert eng.state.goal_snapshot() == {}
    assert eng.state.goal_done() is False


# --------------------------------------------------------------------------- #
# firing
# --------------------------------------------------------------------------- #

def test_goal_met_crown_the_champion_and_fires_once(tmp_path, tmp_cfg, monkeypatch):
    pings = _spy_pings(monkeypatch)
    registry = ProjectRegistry(tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    project = _mk_project(registry, "goal-a", GOAL)
    eng = _mk_engine(project, registry, tmp_cfg)

    eng.state.set_best({"fitness": 3.0, "metrics": {"proved_open": 3}, "generation": 4})
    eng._check_goal(4)

    assert eng.engine_state == STATE_STOPPED, "a met goal stops the project"
    assert eng.state.goal_done() is True
    history = [h for h in eng.state.history if h["outcome"] == "goal_met"]
    assert len(history) == 1 and "proved_open >= 2" in history[0]["detail"]
    assert len(_goal_pings(pings)) == 1

    # The goal keeps holding on every later generation: no second ping, no
    # second history row (the latch is the signature).
    eng._check_goal(5)
    eng._check_goal(6)
    assert len(_goal_pings(pings)) == 1
    assert len([h for h in eng.state.history if h["outcome"] == "goal_met"]) == 1


def test_goal_not_met_leaves_the_project_running(tmp_path, tmp_cfg, monkeypatch):
    pings = _spy_pings(monkeypatch)
    registry = ProjectRegistry(tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    project = _mk_project(registry, "goal-b", GOAL)
    eng = _mk_engine(project, registry, tmp_cfg)

    eng.state.set_best({"fitness": 1.0, "metrics": {"proved_open": 1}, "generation": 2})
    eng._check_goal(2)

    assert eng.engine_state == STATE_RUNNING
    assert eng.state.goal_done() is False
    assert _goal_pings(pings) == []
    assert [h for h in eng.state.history if h["outcome"] == "goal_met"] == []


def test_goal_fires_from_the_applied_evaluation(tmp_path, tmp_cfg, monkeypatch):
    """The check hangs off the evaluation path, not off a manual call: an
    applied evaluation whose champion meets the goal stops the project."""
    pings = _spy_pings(monkeypatch)
    registry = ProjectRegistry(tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    project = _mk_project(registry, "goal-c", GOAL)
    eng = _mk_engine(project, registry, tmp_cfg)

    gen_dir = eng._make_gen_dir(1)
    eng._apply_result(1, {"gen_dir": str(gen_dir), "baseline": False},
                      {"ok": True, "metrics": {"proved_open": 3}, "outcome": "valid"})

    assert eng.engine_state == STATE_STOPPED
    assert eng.state.goal_done() is True
    assert len(_goal_pings(pings)) == 1


def test_ping_only_action_does_not_stop(tmp_path, tmp_cfg, monkeypatch):
    """`then` is the extension point: a goal can report without stopping."""
    pings = _spy_pings(monkeypatch)
    registry = ProjectRegistry(tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    project = _mk_project(registry, "goal-d", {"when": GOAL["when"], "then": ["ping"]})
    eng = _mk_engine(project, registry, tmp_cfg)

    eng.state.set_best({"fitness": 3.0, "metrics": {"proved_open": 3}, "generation": 4})
    eng._check_goal(4)

    assert eng.engine_state == STATE_RUNNING, "no stop action — the project keeps working"
    assert len(_goal_pings(pings)) == 1
    assert eng.state.goal_done() is True


def test_unmet_goal_is_checked_against_the_champion_not_a_lucky_candidate(tmp_path, tmp_cfg, monkeypatch):
    """The project HAS the goal when its champion does; a candidate that
    happens to satisfy the goal metric but LOSES on fitness does not end
    the run."""
    pings = _spy_pings(monkeypatch)
    registry = ProjectRegistry(tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    spec = {
        "id": "goal-e", "name": "Proj goal-e", "language": "python",
        "steps": {"build": {"program": "harness/build.py", "args": ["{candidate}", "{artifact}"]},
                  "verify": [], "score": []},
        "metrics": {"proved_open": {"direction": "higher", "weight": 1.0},
                    "proof_lines": {"direction": "lower", "weight": 0.0}},
        # The goal watches proof_lines; fitness is driven by proved_open.
        "goal": {"when": {"metric": "proof_lines", "op": "<=", "value": 500}},
        "data": {"baseline_source": "baseline.py"},
    }
    project = registry.create("goal-e", spec)
    (project.path / "baseline.py").write_text("print(0)\n", encoding="utf-8")
    eng = _mk_engine(project, registry, tmp_cfg)

    eng.state.set_best({"fitness": 10.0, "metrics": {"proved_open": 10, "proof_lines": 900},
                        "generation": 1})
    gen_dir = eng._make_gen_dir(2)
    eng._apply_result(2, {"gen_dir": str(gen_dir), "baseline": False},
                      {"ok": True, "metrics": {"proved_open": 1, "proof_lines": 400},
                       "outcome": "valid"})

    assert eng.engine_state == STATE_RUNNING
    assert eng.state.goal_done() is False
    assert _goal_pings(pings) == []
