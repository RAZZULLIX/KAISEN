"""LLM last-resort repair: gates, guardrails, and the requeue flow."""
import json
from pathlib import Path

import pytest

from kaisen.config import FrameworkConfig
from kaisen.engine import ProjectEngine
from kaisen.projects import ProjectRegistry


class FakeOrch:
    """Orchestrator stand-in: scripted replies, call log."""

    def __init__(self, cfg, reply="```c\nint fixed(void){return 0;}\n```"):
        self.cfg = cfg
        self.reply = reply
        self.calls = []
        self.outcomes = []

    def request(self, prompt, pipeline_id=0, max_retries=None, min_tier="tiny",
                skill="unknown"):
        self.calls.append(("request", prompt, pipeline_id, min_tier, skill))
        return self.reply

    def request_stream(self, prompt, pipeline_id=0, max_retries=None,
                       min_tier="tiny", skill="unknown", **kw):
        self.calls.append(("stream", prompt, pipeline_id, min_tier, skill))
        return self.reply, "fake-server"

    def record_call(self, sid, skill, cost_usd=0.0):
        pass

    def record_outcome(self, sid, skill, kind):
        self.outcomes.append((sid, skill, kind))


class FakePool:
    def __init__(self):
        self.submitted = []
        self.results = {}

    def submit(self, job):
        self.submitted.append(job)

    def set_worker_result(self, wid, result):
        self.results[wid] = result


_REGISTRY = None


@pytest.fixture
def project(tmp_path, tmp_cfg):
    global _REGISTRY
    _REGISTRY = ProjectRegistry(tmp_path / "projects")
    p = _REGISTRY.create("repair-proj", {
        "id": "repair-proj", "name": "Repair",
        "language": "c",
        "steps": {"build": {"program": "gcc", "args": []}, "verify": [], "score": []},
        "metrics": {"ms": {"direction": "lower"}},
        "prompts": {"goal": "make the loop fast"},
    })
    return p


def _engine(project, orch):
    eng = ProjectEngine(project, orchestrator=orch,
                        registry=_REGISTRY or ProjectRegistry(), worker_count=0)
    eng.pool = FakePool()
    return eng


@pytest.fixture
def eng(project, tmp_cfg):
    orch = FakeOrch(tmp_cfg)
    return _engine(project, orch), orch


def _broken_result():
    return {"ok": False, "stage": "build", "outcome": "build_fail",
            "reason": "gcc: error: undeclared foo",
            "stderr_tail": "error: ‘foo’ undeclared (first use in this function)"}


def _job(gen_dir):
    return {"gen_dir": str(gen_dir), "baseline": False, "job_id": f"j-{gen_dir.name}"}


def _write_candidate(project, gen, code):
    d = project.runs_dir / f"gen_{gen:06d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "candidate.c").write_text(code, encoding="utf-8")
    return d


# ----------------------------------------------------------------------
# gates
# ----------------------------------------------------------------------

def test_no_repair_for_baseline(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 1, "int x;")
    eng_obj._maybe_llm_repair(1, _job(d), _broken_result(), baseline=True)
    assert orch.calls == []


def test_no_repair_for_non_build_fail(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 2, "int x;")
    result = dict(_broken_result(), outcome="verify_fail")
    eng_obj._maybe_llm_repair(2, _job(d), result, baseline=False)
    assert orch.calls == []


def test_no_repair_when_deterministic_fixes_were_applied(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 3, "int x;")
    result = dict(_broken_result(), build_fixes=["include <time.h>"])
    eng_obj._maybe_llm_repair(3, _job(d), result, baseline=False)
    assert orch.calls == []


def test_repair_capped_at_default_max(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 6, "int x;")
    # default cap (config llm_repair_max=3): up to 3 dispatches for a
    # generation that keeps failing; the 4th is gated out.
    for _ in range(4):
        eng_obj._maybe_llm_repair(6, _job(d), _broken_result(), baseline=False)
    assert eng_obj._llm_repair_count[6] == 3


def test_repair_cap_args_override(eng, project):
    eng_obj, orch = eng
    eng_obj.set_autofix_settings(repair_max=1)
    d = _write_candidate(project, 6, "int x;")
    eng_obj._maybe_llm_repair(6, _job(d), _broken_result(), baseline=False)
    eng_obj._maybe_llm_repair(6, _job(d), _broken_result(), baseline=False)
    assert eng_obj._llm_repair_count[6] == 1


def test_repair_off_means_deterministic_only(eng, project):
    eng_obj, orch = eng
    eng_obj.set_autofix_settings(repair_max=0)
    d = _write_candidate(project, 6, "int x;")
    eng_obj._maybe_llm_repair(6, _job(d), _broken_result(), baseline=False)
    assert 6 not in eng_obj._llm_repair_count
    # and the settings setter reports the effective caps
    assert eng_obj._autofix_effective()["repair_max"] == 0


def test_no_repair_when_global_switch_off(eng, project):
    eng_obj, orch = eng
    orch.cfg.data.setdefault("autofix", {})["llm_repair"] = False
    d = _write_candidate(project, 5, "int x;")
    eng_obj._maybe_llm_repair(5, _job(d), _broken_result(), baseline=False)
    assert orch.calls == []

def _ok_result(metrics=None, build_fixes=None):
    return {"ok": True, "stage": "done", "outcome": "ok",
            "metrics": metrics or {}, "build_fixes": build_fixes or []}


def test_constraint_violation_blocks_champion(eng, project):
    eng_obj, orch = eng
    project.spec["metrics"]["ms"]["constraint"] = 1.0
    d = _write_candidate(project, 20, "int x;")
    job = _job(d)
    eng_obj._gen_server[20] = "fake-server"
    eng_obj._apply_result(20, job, _ok_result({"ms": 5.0}))
    hist = [h for h in eng_obj.state.history if h.get("generation") == 20]
    assert hist and hist[-1]["outcome"] == "constraint_violated"
    assert not eng_obj.state.best.get("fitness")
    assert orch.outcomes == []  # a rejected candidate earns no scoreboard


def test_generation_oneshot_and_win_recorded(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 21, "int x;")
    job = _job(d)
    eng_obj._gen_server[21] = "fake-server"
    eng_obj._apply_result(21, job, _ok_result({"ms": 0.5}))
    assert ("fake-server", "generation", "oneshot") in orch.outcomes
    assert ("fake-server", "generation", "win") in orch.outcomes


def test_autofixed_generation_not_oneshot(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 22, "int x;")
    job = _job(d)
    eng_obj._gen_server[22] = "fake-server"
    eng_obj._apply_result(22, job, _ok_result({"ms": 0.5}, build_fixes=["include <x>"]))
    assert ("fake-server", "generation", "oneshot") not in orch.outcomes
    assert ("fake-server", "generation", "win") in orch.outcomes


 # ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# the repair run itself
# ----------------------------------------------------------------------

def test_repair_rewrites_and_requeues(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 7, "int main(void){ foo(); }\n")
    eng_obj._llm_repair_run(7, _job(d), _broken_result())
    assert len(orch.calls) == 1
    kind, prompt, pipeline_id, min_tier, skill = orch.calls[0]
    assert "foo" in prompt and "undeclared" in prompt
    assert pipeline_id == 7 and min_tier == "small" and skill == "llm_repair"
    # candidate rewritten with the fixed code, repair log written
    assert "int fixed(void)" in (d / "candidate.c").read_text()
    assert (d / "repair.txt").exists()
    # the FULL pipeline was re-queued for the same generation
    assert len(eng_obj.pool.submitted) == 1
    job = eng_obj.pool.submitted[0]
    assert job["generation"] == 7 and job["candidate"] == str(d / "candidate.c")


def test_repair_no_change_is_dropped(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 8, "int main(void){ foo(); }\n")
    orch.reply = "```c\nint main(void){ foo(); }\n```"  # identical
    eng_obj._llm_repair_run(8, _job(d), _broken_result())
    assert eng_obj.pool.submitted == []
    assert not (d / "repair.txt").exists()


def test_repair_dangerous_reply_rejected(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 9, "int main(void){ foo(); }\n")
    orch.reply = "```c\n#include <stdlib.h>\nint main(void){ system(\"rm -rf /\"); }\n```"
    eng_obj._llm_repair_run(9, _job(d), _broken_result())
    assert eng_obj.pool.submitted == []
    assert "foo();" in (d / "candidate.c").read_text()  # untouched


def test_repair_oversized_reply_rejected(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 10, "int main(void){ foo(); }\n")
    orch.reply = "```c\nint x;\n" + ("/* pad */\n" * 4000) + "```"
    eng_obj._llm_repair_run(10, _job(d), _broken_result())
    assert eng_obj.pool.submitted == []


def test_repair_llm_error_is_contained(eng, project):
    eng_obj, orch = eng
    d = _write_candidate(project, 11, "int main(void){ foo(); }\n")

    def boom(prompt, **kw):
        raise RuntimeError("server down")

    orch.request_stream = boom
    eng_obj._llm_repair_run(11, _job(d), _broken_result())  # must not raise
    assert eng_obj.pool.submitted == []
    assert any("repair failed" in str(l).lower() for l in eng_obj._last_log)
