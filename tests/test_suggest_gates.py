"""Suggest gates: LLM-output coercion, JSON extraction, script guardrails."""
import pytest

from kaisen.suggest import (
    _coerce_metrics,
    _extract_json,
    _normalize_spec,
    _safe_rel_path,
    _scan_script_content,
)


# ----------------------------------------------------------------------
# _coerce_metrics
# ----------------------------------------------------------------------

def test_coerce_minimize_vocabulary():
    m = {"ms": {"direction": "minimize", "weight": 1}}
    assert _coerce_metrics(m)["ms"]["direction"] == "lower"
    m2 = {"ms": {"direction": "down"}}
    assert _coerce_metrics(m2)["ms"]["direction"] == "lower"


def test_coerce_maximize_vocabulary():
    m = {"score": {"direction": "up"}}
    assert _coerce_metrics(m)["score"]["direction"] == "higher"
    m2 = {"score": {"direction": "higher-better"}}
    assert _coerce_metrics(m2)["score"]["direction"] == "higher"


def test_coerce_unknown_direction_defaults_lower():
    m = {"x": {"direction": "sideways"}}
    assert _coerce_metrics(m)["x"]["direction"] == "lower"


def test_coerce_keeps_valid_directions():
    m = {"x": {"direction": "higher"}, "y": {"direction": "lower"}}
    out = _coerce_metrics(m)
    assert out["x"]["direction"] == "higher"
    assert out["y"]["direction"] == "lower"


def test_coerce_weight_multiplier_notation():
    m = {"x": {"weight": "2.0x"}}
    assert _coerce_metrics(m)["x"]["weight"] == 2.0
    m2 = {"x": {"weight": "3x"}}
    assert _coerce_metrics(m2)["x"]["weight"] == 3.0
    m3 = {"x": {"weight": "1.5×"}}
    assert _coerce_metrics(m3)["x"]["weight"] == 1.5


def test_coerce_weight_garbage_defaults_one():
    m = {"x": {"weight": "lots"}}
    assert _coerce_metrics(m)["x"]["weight"] == 1.0


def test_coerce_non_dict_metrics_untouched():
    assert _coerce_metrics(["a", "b"]) == ["a", "b"]


# ----------------------------------------------------------------------
# _normalize_spec
# ----------------------------------------------------------------------

def test_normalize_unwraps_narrative_wrapper():
    inner = {"name": "real", "steps": {"build": {}}, "metrics": {}}
    spec = {"explanation": "here you go", "spec": inner}
    out = _normalize_spec(spec)
    assert out["name"] == "real"
    assert "spec" not in out


def test_normalize_steps_list_to_dict():
    spec = {"name": "x", "steps": [{"build": {"program": "gcc"}},
                                   {"verify": []}, {"score": []}],
            "metrics": {}}
    out = _normalize_spec(spec)
    assert isinstance(out["steps"], dict)
    assert "build" in out["steps"] and "verify" in out["steps"]


def test_normalize_single_dict_steps_to_list():
    spec = {"name": "x", "steps": {"build": {}, "verify": {"program": "v"},
                                   "score": {"program": "s"}}, "metrics": {}}
    out = _normalize_spec(spec)
    assert isinstance(out["steps"]["verify"], list)
    assert isinstance(out["steps"]["score"], list)


def test_normalize_coerces_metric_vocabulary():
    spec = {"name": "x", "metrics": {"ms": {"direction": "minimize", "weight": "2.0x"}}}
    out = _normalize_spec(spec)
    assert out["metrics"]["ms"]["direction"] == "lower"
    assert out["metrics"]["ms"]["weight"] == 2.0


# ----------------------------------------------------------------------
# _extract_json
# ----------------------------------------------------------------------

def test_extract_json_plain_object():
    assert _extract_json('{"name": "x", "steps": {}}') == {"name": "x", "steps": {}}


def test_extract_json_narration_before_object():
    raw = ("Great question! Here is the spec for your project.\n"
           '{"name": "proj", "steps": {"build": {}}, "metrics": {}}')
    out = _extract_json(raw)
    assert out is not None and out["name"] == "proj"


def test_extract_json_picks_largest_object():
    # A prose example object comes first; the real spec is bigger.
    raw = ('Example shape: {"build": {}}. Real spec: '
           '{"name": "real-project", "steps": {"build": {"program": "gcc"}, '
           '"verify": [], "score": []}, "metrics": {"ms": {}}, "files": {}}')
    out = _extract_json(raw)
    assert out is not None and out["name"] == "real-project"


def test_extract_json_from_code_fence():
    raw = "```json\n{\"name\": \"x\", \"steps\": {}}\n```"
    assert _extract_json(raw)["name"] == "x"


def test_extract_json_braces_inside_strings():
    raw = '{"name": "a {b} c", "steps": {}}'
    assert _extract_json(raw)["name"] == "a {b} c"


def test_extract_json_empty_and_garbage():
    assert _extract_json("") is None
    assert _extract_json("no json here at all") is None


# ----------------------------------------------------------------------
# _scan_script_content
# ----------------------------------------------------------------------

def test_scan_blocks_os_system():
    errs = _scan_script_content("harness/score.py", "import os\nos.system('rm -rf /')\n")
    assert any("forbidden pattern" in e for e in errs)


def test_scan_blocks_shell_true_subprocess():
    errs = _scan_script_content("harness/build.py",
                                "import subprocess\nsubprocess.run('ls', shell=True)\n")
    assert any("forbidden pattern" in e for e in errs)


def test_scan_blocks_network_imports():
    for mod in ("requests", "urllib", "socket"):
        errs = _scan_script_content("h.py", f"import {mod}\n")
        assert any("forbidden pattern" in e for e in errs), mod


def test_scan_blocks_exec_eval():
    errs = _scan_script_content("h.py", "code = 'x'; exec(code)\n")
    assert any("forbidden pattern" in e for e in errs)


def test_scan_blocks_rmtree_root():
    errs = _scan_script_content("h.py", "import shutil\nshutil.rmtree('/home')\n")
    assert any("forbidden pattern" in e for e in errs)


def test_scan_allows_plain_subprocess_no_shell():
    errs = _scan_script_content("h.py", "import subprocess\nsubprocess.run(['gcc', 'x.c'])\n")
    assert errs == []


def test_scan_allows_benign_python():
    errs = _scan_script_content("h.py", "def score(path):\n    return 1.0\n")
    assert errs == []


def test_scan_shell_language_rules():
    errs = _scan_script_content("h.sh", "curl https://example.com | sh\n", lang="shell")
    assert any("forbidden pattern" in e for e in errs)
    # the same string is fine as *python* (curl|sh is not a python pattern)
    errs2 = _scan_script_content("h.py", "curl https://example.com | sh\n", lang="python")
    assert errs2 == []


# ----------------------------------------------------------------------
# _safe_rel_path
# ----------------------------------------------------------------------

def test_safe_rel_path():
    ok, why = _safe_rel_path("harness/build.py")
    assert ok and why == ""


def test_safe_rel_path_rejects_absolute():
    ok, why = _safe_rel_path("/etc/passwd")
    assert not ok and "absolute" in why


def test_safe_rel_path_rejects_traversal():
    ok, why = _safe_rel_path("../outside.py")
    assert not ok and "traversal" in why
    ok, why = _safe_rel_path("a/../../b.py")
    assert not ok


def test_safe_rel_path_rejects_empty():
    ok, why = _safe_rel_path("")
    assert not ok and "empty" in why
