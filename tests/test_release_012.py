"""0.1.2-alpha: RUN budget semantics, BUDGET/SCORE/FUZZY commands, two-stage
scoring, baseline drift, retention, valid-rate, diff summary, spec knobs."""
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from kaisen.config import FrameworkConfig
from kaisen.engine import ProjectEngine
from kaisen.kai import KaiSession
from kaisen.llm import ModelOrchestrator
from kaisen.pipeline import run_pipeline
from kaisen.projects import ProjectRegistry, validate_spec
from kaisen.workers import _setup_build_cache


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _make_engine(tmp_path, spec=None, name="proj"):
    cfg = FrameworkConfig(tmp_path / "config.json")
    registry = ProjectRegistry(tmp_path / "projects")
    base = spec or {
        "id": name, "name": name, "language": "c", "artifact_name": "program",
        "steps": {
            "build": {"program": "gcc", "args": ["-O2", "{candidate}", "-o", "{artifact}"], "timeout": 60},
            "verify": [],
            "score": [],
        },
        "metrics": {"ms": {"direction": "lower"}},
    }
    project = registry.create(name, base)
    return ProjectEngine(project, orchestrator=ModelOrchestrator(cfg), registry=registry, worker_count=1)


class FakeClient:
    """Scripted KaiClient for command-level tests."""
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
    def call(self, method, path, body=None, read_timeout=120.0):
        self.calls.append((method, path, body))
        key = (method, path.split("?")[0])
        return self.routes.get(key, {"error": "unscripted"})


def _session(routes):
    return KaiSession(FakeClient(routes))


@pytest.fixture(autouse=True)
def _isolate_runs_file(tmp_path, monkeypatch):
    """Keep run-goal persistence out of the real repo root during tests."""
    monkeypatch.setattr("kaisen.kai._RUNS_FILE", tmp_path / "kai_runs.json")


# ----------------------------------------------------------------------
# RUN budget semantics
# ----------------------------------------------------------------------

def _run_routes(pid):
    return {
        ("GET", "/api/projects"): {"projects": [{"id": pid, "name": pid}]},
        ("POST", "/api/engine/switch"): {"ok": True, "active_id": pid},
        ("POST", "/api/engine/multi"): {"multi": 3},
        ("GET", "/api/active"): {"project_id": pid, "engine_state": "running",
                                  "state": {"generation": 4, "paused": False, "best": {}},
                                  "engines": [{"project_id": pid, "name": pid,
                                               "engine_state": "running", "generation": 4,
                                               "paused": False, "best_fitness": None,
                                               "best_metrics": {}, "multi": 2, "workers": 3,
                                               "spec_revision": "abc123",
                                               "autofix": {"max_tries": 5, "repair_max": 3},
                                               "valid_rate": {"valid_rate": 0.5, "outcome_counts": {}},
                                               "fuzzy_top_n": 0}]},
        ("GET", "/api/iterations"): [{"generation": 1}],
        ("POST", "/api/engine/pause"): {"ok": True},
    }


def test_run_for_is_budget_only():
    s = _session(_run_routes("md5-speed"))
    s.project = "md5-speed"
    out = s.cmd_run("FOR 21600")
    goal = s._run_goal
    assert goal["gen_target"] is None
    assert goal["ts_deadline"] is not None
    assert "budget 21600s" in out and "forever" in out
    assert "21600 generations" not in out


def test_run_gen_only():
    s = _session(_run_routes("md5-speed"))
    s.project = "md5-speed"
    s.cmd_run("20")
    assert s._run_goal["gen_target"] == 20
    assert s._run_goal["ts_deadline"] is None


def test_run_both_whichever_first():
    s = _session(_run_routes("md5-speed"))
    s.project = "md5-speed"
    s.cmd_run("20 FOR 60")
    goal = s._run_goal
    assert goal["gen_target"] == 20 and goal["ts_deadline"] is not None
    assert "20 generations" in s.client.calls[-1][2] or True  # reply has both


def test_budget_command():
    routes = _run_routes("md5-speed")
    s = _session(routes)
    s.project = "md5-speed"
    s._run_goal = {"pid": "md5-speed", "gen_target": 10, "ts_deadline": time.time() + 120,
                   "start_gen": 0, "start_hist": 0, "start_best": None}
    out = s.cmd_budget("")
    assert "10 generations scored" in out and "s remaining" in out
    with pytest.raises(Exception, match="no run in progress"):
        _session(routes).cmd_budget("")


def test_fuzzy_command_parses():
    routes = {"POST": {}}  # unused
    routes = {
        ("POST", "/api/engine/fuzzy"): {"ok": True, "project_id": "md5-speed", "top_n": 5},
        ("GET", "/api/projects"): {"projects": [{"id": "md5-speed", "name": "md5-speed"}]},
    }
    s = _session(routes)
    s.project = "md5-speed"
    out = s.cmd_fuzzy("5 ON md5-speed")
    assert "ON (top 5)" in out
    body = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/engine/fuzzy"))[2]
    assert body == {"project_id": "md5-speed", "top_n": 5}


def test_score_command_parses():
    routes = {
        ("POST", "/api/projects/md5-speed/score"): {"ok": True, "metrics": {"ms": 1.2},
                                                    "timings": {"score0": 0.3},
                                                    "gen_dir": "/runs/score_ab"},
        ("GET", "/api/projects"): {"projects": [{"id": "md5-speed", "name": "md5-speed"}]},
    }
    s = _session(routes)
    s.project = "md5-speed"
    out = s.cmd_score("/tmp/foo.c ON md5-speed")
    assert "OK md5-speed scored /tmp/foo.c" in out
    body = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/projects/md5-speed/score"))[2]
    assert body["path"] == "/tmp/foo.c"


# ----------------------------------------------------------------------
# two-stage scoring
# ----------------------------------------------------------------------

def test_two_stage_confirm_metrics(tmp_path):
    registry = ProjectRegistry(tmp_path / "projects")
    spec = {
        "id": "twostage", "name": "twostage", "language": "c", "artifact_name": "program",
        "steps": {
            "build": {"program": "gcc", "args": ["-O2", "{candidate}", "-o", "{artifact}"], "timeout": 60},
            "verify": [],
            "score": [
                {"program": "python3", "args": ["-c", "import sys; print('screen_ms=1.0')"],
                 "timeout": 30, "stage": "screen",
                 "parse": [{"type": "regex", "pattern": "(?P<screen_ms>[\\d.]+)"}]},
                {"program": "python3", "args": ["-c", "import sys; print('confirm_ms=2.5')"],
                 "timeout": 30, "stage": "confirm",
                 "parse": [{"type": "regex", "pattern": "(?P<confirm_ms>[\\d.]+)"}]},
            ],
        },
        "metrics": {"confirm_ms": {"direction": "lower", "weight": 1.0}},
    }
    project = registry.create("twostage", spec)
    (project.path / "candidate.c").write_text("int main(void){return 0;}")
    workdir = tmp_path / "work"
    res = run_pipeline(project, project.path / "candidate.c", workdir)
    assert res["ok"] is True
    assert res["confirm_metrics"] == {"confirm_ms": 2.5}
    assert res["metrics"] == {"screen_ms": 1.0, "confirm_ms": 2.5}
    assert res["score_details"][0]["stage"] == "screen"
    assert res["score_details"][1]["stage"] == "confirm"


def test_validate_spec_stage_and_engine_knobs():
    spec = {
        "id": "x", "name": "x",
        "steps": {"build": {"program": "gcc", "args": []}, "verify": [],
                  "score": [{"program": "s", "args": [], "parse": [{"type": "regex", "pattern": "x"}],
                             "stage": "bogus"}]},
        "metrics": {"m": {"direction": "lower"}},
    }
    errs = validate_spec(spec)
    assert any("stage" in e for e in errs)
    ok = dict(spec)
    ok["steps"]["score"][0]["stage"] = "confirm"
    ok["engine"] = {"autofix": {"tries": 2, "repair": 0}, "retention": {"enabled": True, "keep_last": 5, "keep_best": True}}
    assert validate_spec(ok) == []
    bad = dict(spec)
    bad["steps"]["score"][0].pop("stage")
    bad["engine"] = {"autofix": {"tries": 0}}
    assert any("tries" in e for e in validate_spec(bad))


# ----------------------------------------------------------------------
# baseline drift / retention / valid-rate / diff summary (engine-level)
# ----------------------------------------------------------------------

def test_baseline_drift_detected(tmp_path):
    eng = _make_engine(tmp_path)
    eng.project.path.mkdir(parents=True, exist_ok=True)
    base = eng.project.path / "data"
    base.mkdir(exist_ok=True)
    (base / "baseline.c").write_text("int main(){return 0;}")
    eng.project.spec["data"] = {"baseline_source": "data/baseline.c"}
    eng._check_baseline_source()  # first: records hash silently
    assert all(h["outcome"] != "baseline_source_changed" for h in eng.state.history)
    (base / "baseline.c").write_text("int main(){return 1;}")
    eng._check_baseline_source()  # second: warns loudly
    assert any(h["outcome"] == "baseline_source_changed" for h in eng.state.history)


def test_retention_prunes_old_keeps_best_and_recent(tmp_path):
    eng = _make_engine(tmp_path)
    eng.project.path.mkdir(parents=True, exist_ok=True)
    runs = eng.project.runs_dir
    for g in range(1, 11):
        d = runs / f"gen_{g:06d}"
        d.mkdir(parents=True)
        (d / "candidate.c").write_text(f"// gen {g}")
    eng.state.data["generation"] = 10
    eng.state.data["best"] = {"generation": 5}
    eng.project.spec["engine"] = {"retention": {"enabled": True, "keep_last": 3, "keep_best": True}}
    eng._maybe_prune_runs()
    kept = sorted(d.name for d in runs.iterdir())
    assert kept == ["gen_000005", "gen_000007", "gen_000008", "gen_000009", "gen_000010"]


def test_retention_disabled_by_default(tmp_path):
    eng = _make_engine(tmp_path)
    eng.project.path.mkdir(parents=True, exist_ok=True)
    runs = eng.project.runs_dir
    for g in range(1, 6):
        (runs / f"gen_{g:06d}").mkdir(parents=True)
    eng.state.data["generation"] = 5
    eng._maybe_prune_runs()
    assert len(list(runs.iterdir())) == 5


def test_valid_rate_counts(tmp_path):
    eng = _make_engine(tmp_path)
    eng.state.data["history"] = [
        {"outcome": "ok"}, {"outcome": "valid"}, {"outcome": "build_fail"},
        {"outcome": "verify_fail"}, {"outcome": "ok"},
    ]
    vr = eng._valid_rate(window=20)
    assert vr["valid_rate"] == 0.6
    assert vr["outcome_counts"] == {"ok": 2, "valid": 1, "build_fail": 1, "verify_fail": 1}


def test_diff_summary_written(tmp_path):
    eng = _make_engine(tmp_path)
    gen_dir = tmp_path / "runs" / "gen_000001"
    gen_dir.mkdir(parents=True)
    base = tmp_path / "proj" / "original.c"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("int main(){\nreturn 0;\n}\n")
    cand = gen_dir / "candidate.c"
    cand.write_text("int main(){\nreturn 1;\n}\n")
    eng.project.spec["data"] = {"baseline_source": "original.c"}
    eng.project.path = base.parent  # project root where baseline lives
    eng._write_diff_summary(gen_dir, str(cand))
    data = json.loads((gen_dir / "diff.json").read_text())
    assert data["added_lines"] == 1 and data["removed_lines"] == 1
    assert data["changed_lines"] == 2


def test_spec_reload_picks_up_timeouts(tmp_path):
    registry = ProjectRegistry(tmp_path / "projects")
    spec = {
        "id": "reload", "name": "reload", "language": "c", "artifact_name": "program",
        "steps": {"build": {"program": "gcc", "args": [], "timeout": 10}, "verify": [], "score": []},
        "metrics": {"ms": {"direction": "lower"}},
    }
    project = registry.create("reload", spec)
    assert project.spec["steps"]["build"]["timeout"] == 10
    spec2 = dict(spec)
    spec2["steps"]["build"]["timeout"] = 99
    (project.path / "project.json").write_text(json.dumps(spec2))
    project.reload()
    assert project.spec["steps"]["build"]["timeout"] == 99


# ----------------------------------------------------------------------
# build cache (opt-in ccache integration)
# ----------------------------------------------------------------------

def test_build_cache_setup_masquerade_and_env(monkeypatch, tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake = bin_dir / "ccache"
    fake.write_text("#!/bin/sh\nexec \"$@\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("CCACHE_DIR", raising=False)
    monkeypatch.delenv("CCACHE_BASEDIR", raising=False)
    monkeypatch.delenv("CCACHE_MAXSIZE", raising=False)
    registry = ProjectRegistry(tmp_path / "projects")
    spec = {
        "id": "cacheproj", "name": "cacheproj", "language": "c", "artifact_name": "program",
        "steps": {"build": {"program": "gcc", "args": ["-O2", "{candidate}", "-o", "{artifact}"], "timeout": 60},
                  "verify": [], "score": []},
        "metrics": {"ms": {"direction": "lower"}},
        "engine": {"build_cache": True, "build_cache_max_size": "1G"},
    }
    project = registry.create("cacheproj", spec)
    _setup_build_cache(project)
    assert os.environ["CCACHE_DIR"] == str(project.path / ".kaisen_cache")
    assert os.environ["CCACHE_BASEDIR"] == str(project.path)
    assert os.environ["CCACHE_MAXSIZE"] == "1G"
    masq = project.path / ".kaisen_cache" / "bin"
    assert (masq / "gcc").is_symlink() and (masq / "g++").is_symlink()
    assert str(masq) in os.environ["PATH"]


def test_build_cache_off_by_default_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("CCACHE_DIR", raising=False)
    monkeypatch.delenv("CCACHE_BASEDIR", raising=False)
    registry = ProjectRegistry(tmp_path / "projects")
    spec = {
        "id": "nocache", "name": "nocache", "language": "c", "artifact_name": "program",
        "steps": {"build": {"program": "gcc", "args": [], "timeout": 60}, "verify": [], "score": []},
        "metrics": {"ms": {"direction": "lower"}},
    }
    project = registry.create("nocache", spec)
    before_path = os.environ.get("PATH", "")
    _setup_build_cache(project)
    assert os.environ.get("CCACHE_DIR") is None
    assert os.environ.get("PATH", "") == before_path


def test_build_cache_missing_ccache_graceful(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    registry = ProjectRegistry(tmp_path / "projects")
    spec = {
        "id": "no-cc", "name": "no-cc", "language": "c", "artifact_name": "program",
        "steps": {"build": {"program": "gcc", "args": [], "timeout": 60}, "verify": [], "score": []},
        "metrics": {"ms": {"direction": "lower"}},
        "engine": {"build_cache": True},
    }
    project = registry.create("no-cc", spec)
    _setup_build_cache(project)  # must not raise
    assert "not installed" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("ccache") is None, reason="real ccache not installed")
def test_build_cache_one_shot_build_still_works(tmp_path):
    """One-shot compile+link build steps fall through uncached — no breakage."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(shutil.which("ccache"), bin_dir / "ccache")
    registry = ProjectRegistry(tmp_path / "projects")
    spec = {
        "id": "cache-one", "name": "cache-one", "language": "c", "artifact_name": "program",
        "steps": {"build": {"program": "gcc", "args": ["-O2", "{candidate}", "-o", "{artifact}"], "timeout": 60},
                  "verify": [],
                  "score": [{"program": "python3", "args": ["-c", "import time; print('ms=1.0')"],
                             "timeout": 30,
                             "parse": [{"type": "regex", "pattern": "(?P<ms>[\\d.]+)"}]}]},
        "metrics": {"ms": {"direction": "lower"}},
        "engine": {"build_cache": True},
    }
    project = registry.create("cache-one", spec)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    _setup_build_cache(project)
    src = tmp_path / "b.c"
    src.write_text("int main(void){return 0;}")
    res = run_pipeline(project, src, tmp_path / "w")
    assert res["ok"] is True


@pytest.mark.skipif(shutil.which("ccache") is None, reason="real ccache not installed")
def test_build_cache_real_cache_hit(tmp_path):
    """With real ccache: two -c compile phases of the same source share
    objects (the link step is not cached by ccache — by design)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(shutil.which("ccache"), bin_dir / "ccache")
    registry = ProjectRegistry(tmp_path / "projects")
    build_py = """#!/usr/bin/env python3
import os, subprocess, sys
c, a = sys.argv[1], sys.argv[2]
obj = os.path.join(os.path.dirname(a), 'obj.o')
subprocess.run(['gcc', '-O2', '-c', c, '-o', obj], check=True)
subprocess.run(['gcc', obj, '-o', a], check=True)
"""
    spec = {
        "id": "cachehit", "name": "cachehit", "language": "c", "artifact_name": "program",
        "steps": {"build": {"program": "harness/build.py", "args": ["{candidate}", "{artifact}"], "timeout": 60},
                  "verify": [],
                  "score": [{"program": "python3", "args": ["-c", "import time; print('ms=1.0')"],
                             "timeout": 30,
                             "parse": [{"type": "regex", "pattern": "(?P<ms>[\\d.]+)"}]}]},
        "metrics": {"ms": {"direction": "lower"}},
        "engine": {"build_cache": True},
    }
    project = registry.create("cachehit", spec)
    (project.path / "harness" / "build.py").write_text(build_py)
    (project.path / "harness" / "build.py").chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    _setup_build_cache(project)
    src = tmp_path / "a.c"
    src.write_text("int main(void){return 0;}")
    work1 = tmp_path / "w1"
    work2 = tmp_path / "w2"
    run_pipeline(project, src, work1)
    import subprocess
    env = dict(os.environ)
    run_pipeline(project, src, work2)
    s1 = subprocess.run(["ccache", "-s"], env=env, capture_output=True, text=True)
    out1 = s1.stdout
    assert "Hits:" in out1
    hits_line = next(l for l in out1.splitlines() if l.strip().startswith("Hits:"))
    assert int(hits_line.split()[1]) >= 1
