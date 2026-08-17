"""Skill scoreboard: per-(model, skill) stats, allowlists, adaptive routing,
and hard metric constraints."""
import pytest

from kaisen.llm import ModelOrchestrator, Server
from kaisen.scores import check_constraints, validate_metric_schema


def _server(orch, sid, tier="small", priority=1, cost_out=0.0, cost_in=0.0):
    return Server({
        "id": sid, "type": "llama", "url": f"http://127.0.0.1:1/{sid}",
        "tier": tier, "priority": priority,
        "cost_in": cost_in, "cost_out": cost_out,
    }, orch.cfg)


@pytest.fixture
def orch(tmp_cfg):
    return ModelOrchestrator(tmp_cfg)


def _register(orch, *servers):
    orch._servers = {s.id: s for s in servers}
    orch._active_ids = [s.id for s in servers]
    orch._rr = 0


# ----------------------------------------------------------------------
# scoreboard mechanics
# ----------------------------------------------------------------------

def test_record_call_and_outcomes(orch):
    orch.record_call("m", "generation", 0.001)
    orch.record_call("m", "generation", 0.002)
    orch.record_outcome("m", "generation", "oneshot")
    orch.record_outcome("m", "generation", "win")
    rows = orch.model_stats()
    assert len(rows) == 1
    r = rows[0]
    assert r["server_id"] == "m" and r["skill"] == "generation"
    assert r["attempts"] == 2 and r["oneshots"] == 1 and r["wins"] == 1
    assert r["cost_usd"] == 0.003
    assert r["oneshot_rate"] == 0.5 and r["win_rate"] == 0.5


def test_record_outcome_rejects_bad_kinds(orch):
    orch.record_outcome("m", "generation", "banana")
    assert orch.model_stats() == []


def test_stats_persist_across_instances(tmp_cfg):
    o1 = ModelOrchestrator(tmp_cfg)
    o1.record_call("m", "generation", 0.5)
    o1.record_outcome("m", "generation", "win")
    o2 = ModelOrchestrator(tmp_cfg)
    rows = o2.model_stats()
    assert len(rows) == 1 and rows[0]["wins"] == 1 and rows[0]["attempts"] == 1


def test_skill_quality_smoothing(orch):
    orch.record_call("m", "x")
    # 0 attempts: smoothed baseline, not division by zero
    assert orch._skill_quality("n", "x") == 0.0
    orch.record_outcome("m", "x", "win")  # 1 attempt, 1 win
    assert 0 < orch._skill_quality("m", "x") <= 1.0


# ----------------------------------------------------------------------
# allowlists
# ----------------------------------------------------------------------

def test_allowlist_by_server_id(orch):
    a = _server(orch, "a")
    b = _server(orch, "b")
    _register(orch, a, b)
    orch.cfg.llm["allowlists"] = {"suggest": ["b"]}
    assert not orch._allowed("a", "suggest")
    assert orch._allowed("b", "suggest")
    assert orch._allowed("a", "generation")  # no rule for this skill
    assert orch._allowed("a", None)


def test_allowlist_by_tier(orch):
    tiny = _server(orch, "t", tier="tiny")
    large = _server(orch, "l", tier="large")
    _register(orch, tiny, large)
    orch.cfg.llm["allowlists"] = {"llm_repair": ["tier:tiny", "tier:small"]}
    assert orch._allowed("t", "llm_repair")
    assert not orch._allowed("l", "llm_repair")


def test_allowlist_hard_filters_pick(orch):
    a = _server(orch, "a")
    b = _server(orch, "b")
    _register(orch, a, b)
    orch.cfg.llm["allowlists"] = {"generation": ["b"]}
    assert orch._pick_server("tiny", skill="generation") == "b"
    orch.release("b")


def test_allowlist_excludes_all_returns_none(orch):
    a = _server(orch, "a")
    _register(orch, a)
    orch.cfg.llm["allowlists"] = {"generation": ["nonexistent"]}
    assert orch._pick_server("tiny", skill="generation") is None


# ----------------------------------------------------------------------
# adaptive routing
# ----------------------------------------------------------------------

def test_adaptive_prefers_better_score_per_dollar(orch):
    cheap = _server(orch, "cheap", tier="small", cost_out=0.0, cost_in=0.0)
    pricey = _server(orch, "pricey", tier="small", cost_out=10.0, cost_in=0.0)
    _register(orch, cheap, pricey)
    # pricey has perfect stats; cheap has none -> quality/cost picks pricey
    # despite its cost (10 $/Mtok out) because 0.0 quality loses everywhere.
    orch.record_call("pricey", "generation", 0.1)
    orch.record_outcome("pricey", "generation", "win")
    orch.cfg.llm["routing"] = "adaptive"
    assert orch._pick_server("tiny", skill="generation") == "pricey"
    orch.release("pricey")


def test_adaptive_min_tier_still_applies(orch):
    tiny = _server(orch, "tiny", tier="tiny")
    large = _server(orch, "large", tier="large")
    _register(orch, tiny, large)
    orch.record_call("large", "generation", 0.1)
    orch.record_outcome("large", "generation", "win")
    orch.cfg.llm["routing"] = "adaptive"
    # min_tier=large -> tiny cannot serve it, regardless of stats
    assert orch._pick_server("large", skill="generation") == "large"
    orch.release("large")


def test_cost_routing_remains_default(orch):
    tiny = _server(orch, "tiny", tier="tiny")
    large = _server(orch, "large", tier="large")
    _register(orch, tiny, large)
    orch.record_call("large", "generation", 0.1)
    orch.record_outcome("large", "generation", "win")
    assert orch._pick_server("tiny", skill="generation") == "tiny"  # cost-first
    orch.release("tiny")


# ----------------------------------------------------------------------
# hard metric constraints
# ----------------------------------------------------------------------

def test_constraint_validation():
    errs = validate_metric_schema({
        "err": {"direction": "lower", "constraint": 0.01},
        "ms": {"direction": "lower"},
    })
    assert errs == []
    errs = validate_metric_schema({"err": {"direction": "lower", "constraint": "lots"}})
    assert any("constraint" in e for e in errs)


def test_check_constraints_lower():
    schema = {"err": {"direction": "lower", "constraint": 0.01}}
    assert check_constraints({"err": 0.005}, schema) == []
    v = check_constraints({"err": 0.05}, schema)
    assert len(v) == 1 and "0.05" in v[0]


def test_check_constraints_higher():
    schema = {"speed": {"direction": "higher", "constraint": 100.0}}
    assert check_constraints({"speed": 150.0}, schema) == []
    v = check_constraints({"speed": 90.0}, schema)
    assert len(v) == 1 and "90.0" in v[0]


def test_check_constraints_missing_metric_not_violation():
    schema = {"err": {"direction": "lower", "constraint": 0.01}}
    assert check_constraints({}, schema) == []
    assert check_constraints({"err": None}, schema) == []
