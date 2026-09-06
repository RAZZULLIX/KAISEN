"""Fuzz infrastructure: deterministic case generation, output comparison,
and the fuzz_verify gate end-to-end (correct artifact passes, wrong/crashing
artifacts fail with machine-readable diagnostics)."""
import sys
import json
import stat
import subprocess
from pathlib import Path

import pytest

from kaisen import fuzzlib as F

REPO = Path(__file__).resolve().parent.parent
FUZZ_VERIFY = REPO / "kaisen" / "templates" / "_shared" / "fuzz_verify.py"


# --------------------------------------------------------------------------- #
# determinism + shape
# --------------------------------------------------------------------------- #

def test_same_seed_same_cases():
    a = F.gen_cases("int", seed=42, n=60)
    b = F.gen_cases("int", seed=42, n=60)
    assert a == b


def test_different_seed_different_cases():
    a = [c["argv"] for c in F.gen_cases("int", seed=1, n=80)]
    b = [c["argv"] for c in F.gen_cases("int", seed=2, n=80)]
    assert a != b


def test_int_case_shape_and_domain():
    cases = F.gen_cases("int", seed=7, n=50, lo=1, hi=999)
    assert len(cases) == 50
    for c in cases:
        assert len(c["argv"]) == 1 and c["tag"]
        v = int(c["argv"][0])
        assert 1 <= v <= 999


def test_int_edges_cover_the_danger_zones():
    cases = F.gen_cases("int", seed=3, n=200, lo=0, hi=10 ** 6)
    vals = {int(c["argv"][0]) for c in cases}
    for v in (0, 1, 2, 10 ** 6, 10 ** 6 - 1, 2 ** 19, 2 ** 19 - 1, 2 ** 19 + 1):
        assert v in vals, f"missing edge value {v}"


def test_str_edges_cover_empty_and_unicode():
    cases = F.gen_cases("str", seed=5, n=40)
    strs = [c["argv"][0] for c in cases]
    assert "" in strs
    assert any(ord(ch) > 127 for s in strs for ch in s)


def test_pair_int_crosses_corners():
    cases = F.gen_cases("pair_int", seed=9, n=40, alo=1, ahi=50, blo=1, bhi=50)
    assert all(len(c["argv"]) == 2 for c in cases)
    argvs = {tuple(c["argv"]) for c in cases}
    # (edgeA x first edgeB) and (first edgeA x edgeB) both present
    assert ("1", "1") in argvs
    assert any(a != "1" and b == "1" for a, b in argvs)
    assert any(a == "1" and b != "1" for a, b in argvs)


def test_unknown_family_rejected():
    with pytest.raises(ValueError):
        F.gen_cases("nope", seed=1, n=5)


# --------------------------------------------------------------------------- #
# comparison modes
# --------------------------------------------------------------------------- #

def test_compare_exact_ignores_surrounding_whitespace():
    assert F.compare_outputs("  42\n", "42")
    assert not F.compare_outputs("42", "43")


def test_compare_sorted_lines_is_order_free():
    assert F.compare_outputs("b\na\nc\n", "a\nb\nc", mode="sorted_lines")
    assert not F.compare_outputs("a\na\nb\n", "a\nb\nb", mode="sorted_lines")  # multiset, not set


def test_compare_float_last_tolerance():
    assert F.compare_outputs("score=1.0000004", "score=1.0", mode="float_last")
    assert not F.compare_outputs("score=1.5", "score=1.0", mode="float_last")
    assert F.compare_outputs("x 0.0000001", "x 0", mode="float_last")


def test_compare_unknown_mode_rejected():
    with pytest.raises(ValueError):
        F.compare_outputs("a", "a", mode="vibes")
# end-to-end: the gate itself
# --------------------------------------------------------------------------- #

POPREF = "#!/usr/bin/env python3\nimport sys\nprint(bin(int(sys.argv[1])).count('1'))\n"
POPBAD = ("#!/usr/bin/env python3\nimport sys\n"
          "print(bin(int(sys.argv[1]) // 2).count('1'))\n")   # wrong for odds
POPCRAZY = "#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n"


def _write_exec(path: Path, code: str) -> None:
    path.write_text(code, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fuzz_project(tmp_path):
    """A tiny project dir: cases computed from the CORRECT reference."""
    ref = tmp_path / "ref"
    _write_exec(ref, POPREF)
    cases = F.gen_cases("int", seed=11, n=40, lo=0, hi=5000)
    cases = F.compute_expected(ref, cases, timeout=10.0)
    F.save_cases(tmp_path / "fuzz_cases.json", "int", 11, 40, "exact", cases,
                 lo=0, hi=5000)
    return tmp_path


def _run_gate(project: Path, artifact: Path):
    return subprocess.run(
        [sys.executable, str(FUZZ_VERIFY), str(artifact)],
        capture_output=True, cwd=project, timeout=120)


def test_correct_artifact_passes(fuzz_project):
    good = fuzz_project / "good"
    _write_exec(good, POPREF)
    r = _run_gate(fuzz_project, good)
    assert r.returncode == 0, r.stderr.decode()
    assert "OK 40 cases" in r.stdout.decode()


def test_wrong_artifact_fails_with_mismatch(fuzz_project):
    bad = fuzz_project / "bad"
    _write_exec(bad, POPBAD)
    r = _run_gate(fuzz_project, bad)
    assert r.returncode == 1
    err = r.stderr.decode()
    assert "FUZZ MISMATCH" in err
    assert "expected=" in err and "got=" in err          # reproducible diagnostic


def test_crashing_artifact_fails_with_crash(fuzz_project):
    crazy = fuzz_project / "crazy"
    _write_exec(crazy, POPCRAZY)
    r = _run_gate(fuzz_project, crazy)
    assert r.returncode == 1
    assert "FUZZ CRASH" in r.stderr.decode()


def test_missing_cases_file_is_setup_error(fuzz_project):
    good = fuzz_project / "good"
    _write_exec(good, POPREF)
    r = subprocess.run([sys.executable, str(FUZZ_VERIFY), str(good),
                        "nope.json"], capture_output=True, cwd=fuzz_project)
    assert r.returncode == 1
    assert "FUZZ SETUP" in r.stderr.decode()


def test_reference_failure_never_ships(tmp_path):
    """compute_expected must raise when the reference itself fails — a fuzz
    set with missing expectations is worse than none."""
    broken = tmp_path / "broken_ref"
    _write_exec(broken, POPCRAZY)
    cases = F.gen_cases("int", seed=1, n=5, lo=0, hi=10)
    with pytest.raises(RuntimeError):
        F.compute_expected(broken, cases, timeout=5.0)


def test_nonexecutable_artifact_fails_with_exec_error(fuzz_project):
    """An artifact without the exec bit must produce an actionable FUZZ
    EXEC ERROR diagnostic, not a raw harness traceback (observed live:
    9 straight verify_failures on collatz-steps-python, all tracebacks)."""
    good = fuzz_project / "noexec"
    good.write_text(POPREF, encoding="utf-8")          # NO chmod +x
    r = _run_gate(fuzz_project, good)
    assert r.returncode == 1
    err = r.stderr.decode()
    assert "FUZZ EXEC ERROR" in err
    assert "Traceback" not in err                      # no raw crash
