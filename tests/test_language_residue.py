"""Regression tests: no C-only residue in non-C projects.

These lock in the fixes for the "everything is C" bug family:
  - ensure_headers must not inject C #include lines into non-C candidates
  - the project-builder prompts must use each language's real toolchain
    and driver file (never a hardcoded driver.c / gcc)
  - the driver flow is gated to COMPILED languages
  - the guardrail launcher allowlist covers non-C toolchains/interpreters
  - KAI GOAL forwards a staged BASELINE <lang> so a python baseline never
    silently becomes a C project
"""
import pytest

from pathlib import Path

from kaisen import promptlib
from kaisen.guardrails import ALLOWED_LAUNCHERS
from kaisen.languages import toolchain_from_lang
from kaisen.skills import ensure_headers
from kaisen.suggest import needs_driver, validate_structure


# ----------------------------------------------------------------------
# ensure_headers — C headers only for C
# ----------------------------------------------------------------------

def test_ensure_headers_injects_nothing_for_non_c():
    py = "def fast(x):\n    return x * 2\n"
    assert ensure_headers(py, "python") == py
    go = "package main\n\nimport \"fmt\"\n\nfunc main() { fmt.Println(1) }\n"
    assert ensure_headers(go, "go") == go
    js = "function fast(x) { return x * 2; }\n"
    assert ensure_headers(js, "javascript") == js
    rs = "pub fn fast(x: i64) -> i64 { x * 2 }\n"
    assert ensure_headers(rs, "rust") == rs


def test_ensure_headers_still_injects_for_c():
    src = "int main(void) { return 0; }\n"
    out = ensure_headers(src, "c")
    assert out.startswith("#include <stdint.h>")
    assert "int main" in out
    # the default (no language) is the C path — engine now always passes
    # the project language explicitly
    assert ensure_headers(src) == out


# ----------------------------------------------------------------------
# project-builder prompts — per-language toolchain + driver file
# ----------------------------------------------------------------------

def test_build_script_uses_language_toolchain():
    p = promptlib.step_build_script("small", "rust", "compiled")
    assert "rustc" in p and "CARGO flag" in p and "nvcc" not in p
    # the old hardcoded C default must be gone
    assert "gcc/g++/nvcc" not in p
    p = promptlib.step_build_script("small", "java", "compiled", with_driver=True)
    assert "javac" in p and "driver.java" in p and "driver.c" not in p
    p = promptlib.step_build_script("small", "python", "interpreted")
    assert "py_compile" in p and "shebang" in p
    p = promptlib.step_build_script("small", "c", "compiled", with_driver=True)
    assert "gcc" in p and "driver.c" in p


@pytest.mark.parametrize("lang,fname", [
    ("c", "driver.c"), ("cpp", "driver.cpp"), ("rust", "driver.rs"),
    ("go", "driver.go"), ("java", "driver.java"), ("python", "driver.py"),
    ("dart", "driver.dart"), ("d", "driver.d"),
])
def test_driver_prompt_is_language_aware(lang, fname):
    p = promptlib.step_driver_program("small", "ANALYSIS JSON", lang)
    assert f"harness/{fname}" in p
    assert "driver.cpp" not in p.replace(f"harness/{fname}", "")
    for c_ism in ("fflush", "fabs(", "printf", "prototypes", "argv[1]"):
        assert c_ism not in p, (lang, c_ism)


def test_toolchain_registry():
    assert toolchain_from_lang("c") == "gcc"
    assert toolchain_from_lang("cpp") == "g++"
    assert toolchain_from_lang("cuda") == "nvcc"
    assert toolchain_from_lang("rust") == "rustc"
    assert toolchain_from_lang("java") == "javac"
    assert toolchain_from_lang("go") == "go build"
    assert toolchain_from_lang("d") == "dmd or ldc2"
    assert toolchain_from_lang("python") == ""      # interpreted
    assert toolchain_from_lang("javascript") == ""  # interpreted


# ----------------------------------------------------------------------
# driver flow gating — compiled languages only
# ----------------------------------------------------------------------

def test_needs_driver_gated_to_compiled():
    assert needs_driver("python", "interpreted", "def f(x): return x") is False
    assert needs_driver("ruby", "interpreted", "def f(x); x; end") is False
    assert needs_driver("c", "compiled", "int f(int x) { return x; }") is True
    assert needs_driver("rust", "compiled", "pub fn f(x: i32) -> i32 { x }") is True
    # a program with its own main never needs a driver
    assert needs_driver("c", "compiled", "int main(void) { return 0; }") is False
    assert needs_driver("rust", "compiled", "fn main() { println!(\"hi\"); }") is False
    assert needs_driver("rust", "compiled", "") is False


# ----------------------------------------------------------------------
# launcher allowlist — non-C toolchains and interpreters
# ----------------------------------------------------------------------

def test_launcher_allowlist_has_non_c_toolchains():
    for name in ("node", "rustc", "javac", "go", "swiftc", "ghc", "dotnet",
                 "ruby", "Rscript", "zig", "dart", "kotlinc", "cargo", "java"):
        assert name in ALLOWED_LAUNCHERS, name


def test_validate_structure_accepts_bare_non_c_launcher():
    """A suggested spec whose build program is a bare `rustc` must NOT be
    told that `rustc` is a missing project file."""
    spec = {
        "name": "rusty",
        "steps": {
            "build": {"program": "rustc", "args": ["{candidate}", "-o", "{artifact}"]},
            "verify": [{"program": "harness/verify.py"}],
            "score": [{"program": "harness/score.py",
                       "parse": [{"type": "regex", "pattern": "ms=(?P<ms>[\\d.]+)"}]}],
        },
        "metrics": {"ms": {"direction": "lower", "weight": 1}},
        "data": {"baseline_source": "original.rs"},
        "files": {
            "original.rs": "fn main() {}",
            "harness/verify.py": "print('OK')",
            "harness/score.py": "print('ms=1.0')",
        },
    }
    errs = validate_structure(spec)
    assert not any("rustc" in e for e in errs), errs


# ----------------------------------------------------------------------
# KAI — GOAL forwards the staged BASELINE language
# ----------------------------------------------------------------------

class FakeClient:
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


def _kai_session(routes):
    from kaisen.kai import KaiSession
    return KaiSession(FakeClient(routes))


def test_goal_forwards_staged_baseline_language():
    routes = {("POST", "/api/projects/suggest"): {
        "ok": True, "suggested_spec": {"name": "x"}, "validation": {"rounds": 1}}}
    s = _kai_session(routes)
    s.cmd_baseline("python", ["def f(x): return x", "print(f(1))"])
    assert s._baseline_lang == "python"
    s.cmd_goal("make it faster")
    sent = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/projects/suggest"))
    assert sent[2]["language"] == "python"
    assert "def f(x)" in sent[2]["code"]


def test_goal_without_staged_language_sends_none():
    routes = {("POST", "/api/projects/suggest"): {
        "ok": True, "suggested_spec": {"name": "x"}, "validation": {"rounds": 1}}}
    s = _kai_session(routes)
    s.cmd_goal("make it faster")
    sent = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/projects/suggest"))
    assert "language" not in sent[2]


# ----------------------------------------------------------------------
# health-probe gate — the probe must mark the SAME orchestrator the
# status handler reads, or `online` never latches and every dashboard
# poll re-probes forever (the "0 token generation" flood).
# ----------------------------------------------------------------------

def test_probe_server_uses_passed_orchestrator():
    import asyncio
    from kaisen.server import DashboardServer

    class FakeOrch:
        def __init__(self):
            self.probed = []

        def check_health(self, sid):
            self.probed.append(sid)
            return {"ok": True}

    srv = object.__new__(DashboardServer)
    srv._probe_inflight = {"s1"}
    orch = FakeOrch()
    asyncio.run(srv._probe_server("s1", orch))
    assert orch.probed == ["s1"]
    assert "s1" not in srv._probe_inflight  # gate can fire again — but only after latch


def test_health_latches_when_probing_the_read_orchestrator(monkeypatch):
    """After one successful probe through the SAME orchestrator whose
    snapshot the status handler reads, the server's online becomes True —
    so the 'online is None' gate closes and polling cannot re-probe."""
    from kaisen.llm import ModelOrchestrator
    from kaisen.config import FrameworkConfig

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": "ok", "tokens_predicted": 2}

    monkeypatch.setattr("kaisen.llm.requests.post", lambda *a, **k: _FakeResp())

    cfg = FrameworkConfig()
    cfg.data["llm"] = {
        "servers": [{"id": "fake1", "type": "llama", "url": "http://127.0.0.1:1/completion",
                     "model": "x", "params": {}, "timeout": 5, "connect_timeout": 1}],
        "active_ids": ["fake1"],
        "max_retries": 1, "retry_backoff": 0.1,
    }
    orch = ModelOrchestrator(cfg)
    orch._active_ids = ["fake1"]
    # gate: online is None -> would probe
    assert orch.status()["servers"][0]["online"] is None
    # one probe on THIS orchestrator latches it
    orch.check_health("fake1")
    assert orch.status()["servers"][0]["online"] is True


def test_no_hardcoded_bearer_dummy():
    """The configured API key must reach llama/remote endpoints — the old
    hardcoded 'Bearer dummy' ignored env/secrets entirely."""
    src = open(Path(__file__).resolve().parent.parent / "kaisen" / "llm.py",
               encoding="utf-8").read()
    assert "Bearer dummy" not in src


# ----------------------------------------------------------------------
# synthesized parse rules must be anchored on "key=" — a bare
# (?P<key>\d+) matches ANY number and parse_metrics takes the LAST match,
# so every metric would silently bind the same number (the binary-size bug
# seen in the Rust suggest smoke: time_ms == size_bytes).
# ----------------------------------------------------------------------

def test_synthesized_parse_rules_are_anchored():
    from kaisen.scores import parse_metrics
    output = "KAISEN_PROGRESS time_ms=12.345 size_bytes=4382768\ntime_ms=12.345\n"
    # the OLD bare fallback pattern: every metric binds the SAME (last)
    # number — size_bytes wrongly gets the time value, cross-wired
    bare = [{"type": "regex", "pattern": r"(?P<time_ms>[\d.]+)"},
            {"type": "regex", "pattern": r"(?P<size_bytes>[\d.]+)"}]
    old = parse_metrics(output, bare)
    assert old.get("time_ms") == old.get("size_bytes") == 12.345
    assert old.get("size_bytes") != 4382768.0  # cross-wired to the time line
    # the anchored pattern: each metric binds its OWN key= line
    anchored = [{"type": "regex", "pattern": r"time_ms=(?P<time_ms>[\d.]+)"},
                {"type": "regex", "pattern": r"size_bytes=(?P<size_bytes>[\d.]+)"}]
    new = parse_metrics(output, anchored)
    assert new.get("time_ms") == 12.345
    assert new.get("size_bytes") == 4382768.0


def test_suggest_fallback_rule_string_is_anchored():
    """The fallback synthesis in suggest.parse_score must emit
    'key=(?P<key>...)' — verify by reproducing the exact f-string."""
    import re as _re
    from kaisen import suggest as _suggest
    src = open(Path(_suggest.__file__).resolve(), encoding="utf-8").read()
    assert 're.escape(key)}=(?P<{key}>' in src
    # and the bare pattern must be gone
    assert 'f"(?P<{key}>[\\\\\\\\d.]+)"' not in src
