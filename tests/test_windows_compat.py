"""Regression tests: Windows compatibility + the user-reported crashers.

W-1  — run_subprocess must drain >64KiB of output without deadlocking on
       ANY platform (select() on pipes is POSIX-only; on Windows the old
       drain loop never saw readiness and hung as soon as a chatty child
       filled the pipe buffer).
W-2  — timeout/abort kills the WHOLE process tree (harness + candidate),
       not just the direct child.
W-3  — guardrails must recognize Windows drive paths ("C:\\…") as absolute
       on every OS, so a spec scans identically cross-platform: in-project
       is trusted, out-of-project is denied — never falling through to the
       bare-launcher allowlist.
W-4  — relative programs with separators resolve against the PROJECT dir at
       scan time (matching execution-time resolution), not the process CWD.
E-1  — suggest repair loop: a GATE failure skips the smoke run entirely;
       stage/notes/reason must still exist (old code raised
       UnboundLocalError: 'notes' — the "Suggest failed" crash users hit
       with gpt-oss template-garbage replies).
E-2  — _lint_scripts under a non-UTF-8 locale: model output containing
       U+202F (narrow no-break space) must not crash the temp-file write
       ('charmap' codec can't encode character '\\u202f').
"""
import os
import subprocess
import sys
import time

import pytest

from kaisen.guardrails import check_command, _resolve_executable
from kaisen.util import kill_pid_tree, run_subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------
# W-1 / W-2 — cross-platform process supervision
# ----------------------------------------------------------------------

def test_run_subprocess_captures_large_output_without_deadlock():
    """Child writes 500KiB to EACH pipe and exits.  A parent that only
    drains between poll cycles (the old select-based loop, which on Windows
    never reported readiness) deadlocks here; concurrent readers must not."""
    res = run_subprocess(
        [sys.executable, "-c",
         "import sys\n"
         "sys.stdout.write('A' * 500000)\n"
         "sys.stderr.write('B' * 500000)\n"],
        timeout=60, poll_interval=0.05,
    )
    assert res["ok"] is True
    assert len(res["stdout"]) == 500000
    assert len(res["stderr"]) == 500000


def test_run_subprocess_progress_token_live_fields():
    seen = []

    def prog(d):
        seen.append(dict(d))

    res = run_subprocess(
        [sys.executable, "-c",
         "import sys\n"
         "for i in range(3):\n"
         "    print('KAISEN_PROGRESS gen=%d tps=9.5' % i)\n"],
        timeout=60, poll_interval=0.02, progress=prog,
        progress_token="KAISEN_PROGRESS",
    )
    assert res["ok"] is True
    lives = [d["live"] for d in seen if d.get("live")]
    assert any(l.get("gen") == "2" and l.get("tps") == "9.5" for l in lives)


def test_run_subprocess_timeout_kills_whole_tree(tmp_path):
    """A harness that spawned a long-lived child must be killed TOGETHER
    with that child (POSIX: process group; Windows: taskkill /T)."""
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('CHILD=%d' % p.pid)\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    res = run_subprocess([sys.executable, str(script)],
                         timeout=1.5, poll_interval=0.1)
    assert res["timed_out"] is True
    assert res["ok"] is False
    child_pid = int(res["stdout"].split("CHILD=")[1].strip())
    time.sleep(0.3)  # let the kill land
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    else:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}"],
                           capture_output=True, timeout=10)
        assert str(child_pid) not in r.stdout.decode("utf-8", "replace")


def test_kill_pid_tree_kills_children(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(p.pid)\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, str(script)],
                            stdout=subprocess.PIPE, **kwargs)
    try:
        child_pid = int(proc.stdout.readline().decode().strip())
        assert kill_pid_tree(proc.pid) is True
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    time.sleep(0.3)
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


# ----------------------------------------------------------------------
# W-3 / W-4 — guardrail launcher policy, cross-platform path recognition
# ----------------------------------------------------------------------

def test_guardrails_posix_absolute_in_project_trusted(tmp_path):
    ok, why = _resolve_executable([str(tmp_path / "harness" / "build.py")], tmp_path)
    assert ok, why

def test_guardrails_posix_absolute_outside_denied(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    ok, why = _resolve_executable([str(other / "evil.py")], proj)
    assert not ok
    assert "outside project directory" in why


def test_guardrails_windows_drive_path_uses_absolute_branch():
    """A Windows drive path must be judged by its in-project location on
    EVERY host (PureWindowsPath recognition), never fall through to the
    bare-launcher allowlist — that was the rejection users hit on Windows."""
    tmp = os.path.dirname(os.path.abspath(__file__))  # any real dir
    ok, why = _resolve_executable(["C:\\outside\\the\\project\\build.py"], tmp)
    assert not ok
    assert "outside project directory" in why  # absolute branch, NOT allowlist


def test_guardrails_relative_with_separator_resolves_against_project(tmp_path):
    """'harness/build.py' (no ./ prefix) resolves against the PROJECT dir —
    matching Project.resolve_program at execution time."""
    ok, why = _resolve_executable(["harness/build.py"], tmp_path)
    assert ok, why


def test_guardrails_relative_escape_denied(tmp_path):
    ok, why = _resolve_executable(["../../etc/passwd"], tmp_path)
    assert not ok
    assert "outside project directory" in why


def test_guardrails_bare_launcher_policy():
    proj = os.path.dirname(os.path.abspath(__file__))
    ok, _ = _resolve_executable(["python"], proj)
    assert ok
    ok, why = _resolve_executable(["evilbin"], proj)
    assert not ok
    assert "not allowed" in why


def test_guardrails_framework_level_windows_absolute_allowed():
    """No project context: absolute paths (POSIX or Windows) are trusted."""
    ok, _ = check_command(["C:\\framework\\tool.exe", "--x"])
    assert ok
    ok, why = check_command(["evilbin"])
    assert not ok


# ----------------------------------------------------------------------
# E-1 — suggest repair loop: gate failure must not raise UnboundLocalError
# ----------------------------------------------------------------------

def _fake_llm(build_script: str):
    """Stateless fake LLM keyed on the prompt's step marker.  Returns a
    reply for each suggest_project step; the build script is injectable so
    a test can force a gate failure (forbidden pattern)."""
    analysis = ('{"summary": "Test program", '
                '"entry_contract": "int main(void)", '
                '"metrics": [{"key": "ms", "direction": "lower"}]}')
    verify = "```python\nimport subprocess, sys\n" \
             "r = subprocess.run([sys.argv[1]], capture_output=True, timeout=30)\n" \
             "print('OK' if r.returncode == 0 else 'FAIL')\n" \
             "sys.exit(0 if r.returncode == 0 else 1)\n```"
    score = ("```python\nimport sys\n"
             "print('ms=1.5')\n```\n"
             "METRICS JSON: {\"ms\": {\"label\": \"Time\", \"unit\": \"ms\", "
             "\"direction\": \"lower\", \"weight\": 1}}\n"
             "ms = (?P<ms>[\\d.]+)")

    def request(conv):
        last_user = ""
        for m in reversed(conv):
            if m["role"] == "user":
                last_user = m["content"]
                break
        if "You are the ANALYSIS step" in last_user:
            return analysis
        if "You are the BUILD step" in last_user:
            return f"```python\n{build_script}\n```"
        if "You are the VERIFY step" in last_user:
            return verify
        if "You are the SCORE step" in last_user:
            return score
        # REPAIR (or anything else): no JSON — the repair round fails and
        # suggest_project must return a clean failure result.
        return "I cannot fix this."

    return request


def test_suggest_gate_failure_returns_result_not_unboundlocal(tmp_path, monkeypatch):
    """Regression: when a validation gate fails BEFORE the smoke run, the
    repair section used to read `notes` (assigned only inside the smoke
    branch) -> UnboundLocalError.  Must now return ok=False with the gate
    errors as feedback."""
    from kaisen import suggest

    # os.system in the build script trips _scan_script_content -> gate fails,
    # smoke run is skipped entirely.
    bad_build = ("import os, sys\n"
                 "os.system('gcc -O2 ' + sys.argv[1] + ' -o ' + sys.argv[2])\n")
    result = suggest.suggest_project(
        _fake_llm(bad_build),
        goal="speed up my prime checker",
        code="int main(void){return 0;}\n",
        max_rounds=1,
    )
    assert result["ok"] is False
    assert "forbidden pattern" in result["error"]
    # The repair round ran (last resort) and reported cleanly.
    assert any(s.get("id") == "repair" for s in result["steps"])


# ----------------------------------------------------------------------
# E-2 — lint scripts under a non-UTF-8 locale (the \u202f charmap crash)
# ----------------------------------------------------------------------

def test_lint_scripts_survives_narrow_nbsp_under_ascii_locale():
    """Regression: model output containing U+202F crashed _lint_scripts
    under a non-UTF-8 locale ('charmap' codec can't encode character
    '\\u202f') — the temp-file write must be explicit UTF-8."""
    snippet = (
        "import sys\n"
        "label = '1\u202f234 ms'\n"  # narrow no-break space
        "print(label)\n"
    )
    code = (
        "import sys\n"
        f"sys.path.insert(0, {REPO!r})\n"
        "from kaisen.suggest import _lint_scripts\n"
        f"files = {{'harness/build.py': {snippet!r}}}\n"
        "errs = _lint_scripts(files)\n"
        "print('ERRS:' + repr(errs))\n"
    )
    env = dict(os.environ, LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, env=env, timeout=120)
    assert r.returncode == 0, f"crashed under ASCII locale: {r.stderr.decode('utf-8', 'replace')[-800:]}"
    assert b"ERRS:[]" in r.stdout
