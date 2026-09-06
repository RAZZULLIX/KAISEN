"""Project factory: registry consistency, generation determinism, and the
end-to-end self-check (build baseline -> pass fuzz gate -> score)."""
import subprocess
import sys

from kaisen import factory as FA


def test_registry_is_complete():
    """Every algorithm has a reference, baselines for every language, and a
    workload for every language — a gap here means a silently skipped
    project in the campaign."""
    assert len(FA.list_algorithms()) >= 10
    for key in FA.list_algorithms():
        algo = FA.ALGORITHMS[key]
        assert algo["ref"].strip(), f"{key}: missing reference"
        for lang in FA.list_languages():
            assert algo["baselines"].get(lang, "").strip(), \
                f"{key}/{lang}: missing baseline"
            assert algo["workload"].get(lang), f"{key}/{lang}: missing workload"


def test_generated_spec_is_valid_and_seeded():
    proj = FA.make_project("popcount", "c", n_cases=100)
    assert proj["id"] == "popcount-c"
    spec = proj["spec"]
    # fuzz gate is the verify step; cases are seeded & recorded in data.fuzz
    assert spec["steps"]["verify"][0]["program"] == "harness/fuzz_verify.py"
    fz = spec["data"]["fuzz"]
    assert fz["family"] == "int" and fz["n"] == 100 and isinstance(fz["seed"], int)
    cases = proj["files"]["fuzz_cases.json"]
    import json
    doc = json.loads(cases)
    assert len(doc["cases"]) == 100
    assert all("expected" in c for c in doc["cases"])


def test_generation_is_deterministic():
    a = FA.make_project("gcd", "python", n_cases=50, seed=99)
    b = FA.make_project("gcd", "python", n_cases=50, seed=99)
    assert a == b


def test_selfcheck_proves_popcount_c_end_to_end():
    """The real guarantee: the C baseline builds, passes its full fuzz gate
    against the Python reference, and scores. If this ever fails, the
    factory shipped a broken project."""
    proj = FA.make_project("popcount", "c", n_cases=60)
    errs = FA.check_project(proj)
    assert errs == [], "; ".join(errs)


def test_selfcheck_proves_reverse_str_python_end_to_end():
    proj = FA.make_project("reverse-str", "python", n_cases=40)
    errs = FA.check_project(proj)
    assert errs == [], "; ".join(errs)


def test_broken_baseline_is_caught_by_selfcheck(tmp_path):
    """A baseline that disagrees with the reference must be rejected, not
    registered — this is what keeps 'fast but wrong' out of the campaign."""
    proj = FA.make_project("popcount", "c", n_cases=20)
    # sabotage: always print 0 (wrong for any input with set bits)
    proj["files"]["original.c"] = (
        "#include <stdio.h>\n"
        "int main(void){ printf(\"0\\n\"); return 0; }\n")
    errs = FA.check_project(proj, workdir=tmp_path / "p", keep=True)
    assert any("fuzz gate failed" in e for e in errs), errs


def test_python_build_adds_shebang(tmp_path):
    """Shebang insurance: an LLM candidate that drops the shebang must still
    produce an executable artifact (build owns executability, model owns
    logic). Live incident: no-shebang candidates exec'd as 'Exec format
    error' and flooded the gate with tracebacks."""
    build = tmp_path / "build.py"
    build.write_text(FA._BUILD_SCRIPTS["python"], encoding="utf-8")
    cand = tmp_path / "candidate.py"
    cand.write_text("import sys\nprint(bin(int(sys.argv[1])).count('1'))\n",
                    encoding="utf-8")                  # no shebang
    art = tmp_path / "program"
    r = subprocess.run([sys.executable, str(build), str(cand), str(art)],
                       capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode()
    first = art.read_bytes().split(b"\n", 1)[0]
    assert first.startswith(b"#!")
    out = subprocess.run([str(art), "5"], capture_output=True, timeout=10)
    assert out.returncode == 0 and out.stdout.strip() == b"2"


def test_contract_text_pins_the_io_protocol(tmp_path):
    """Regression (live incident, 2026-09-06): factory prompts said nothing
    about WHERE the input comes from — candidates read stdin while the fuzz
    gate passed argv, so entire projects failed every generation with empty
    output. The contract must state the I/O protocol and carry a concrete
    example computed from the reference."""
    proj = FA.make_project("gcd", "c", n_cases=8)
    ct = proj["spec"]["data"]["contract_text"]
    assert "I/O PROTOCOL" in ct
    assert "NEVER read from stdin" in ct
    # a concrete, reference-verified example anchors the protocol
    assert "Example: `program" in ct and "must print exactly" in ct
    # the task text rides along so the contract is self-contained
    assert FA.ALGORITHMS["gcd"]["goal"][:40] in ct
