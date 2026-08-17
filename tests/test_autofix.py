"""Autofix: compiler-hint extraction and every deterministic fix rule."""
import pytest

from kaisen.autofix import (
    _AVX_INCLUDE_RE,
    _add_gnu_source,
    _add_include,
    _replace_token,
    apply_fix,
    autofix_build,
    parse_hints,
    resolve_mode,
)


def test_parse_hints_include():
    err = ("candidate.c:3:10: fatal error: immintrin.h: No such file\n"
           "    3 | #include <immintrin.h>\n"
           "      | did you forget to #include <x86intrin.h>?")
    includes, means = parse_hints(err)
    assert includes == ["x86intrin.h"]
    assert means == []


def test_parse_hints_did_you_mean_typographic_quotes():
    err = "candidate.c:7: error: ‘vel_dot’ undeclared; did you mean ‘vec_dot’?"
    includes, means = parse_hints(err)
    assert means == [("vel_dot", "vec_dot")]


def test_parse_hints_nothing():
    includes, means = parse_hints("error: no such hints here")
    assert includes == [] and means == []


def test_add_include_after_existing_include():
    src = "#include <stdio.h>\nint main(void){return 0;}\n"
    out = _add_include(src, "time.h")
    assert out == "#include <stdio.h>\n#include <time.h>\nint main(void){return 0;}\n"


def test_add_include_keeps_define_before_include():
    """#define _GNU_SOURCE must stay above every include (glibc feature tests)."""
    src = "#define _GNU_SOURCE\n#include <stdio.h>\nint main(void){return 0;}\n"
    out = _add_include(src, "time.h")
    assert out == "#define _GNU_SOURCE\n#include <stdio.h>\n#include <time.h>\nint main(void){return 0;}\n"


def test_add_include_first_include_lands_after_defines():
    """No include yet + a leading define: the include goes after the define."""
    src = "#define _GNU_SOURCE\nint main(void){return 0;}\n"
    out = _add_include(src, "time.h")
    assert out == "#define _GNU_SOURCE\n#include <time.h>\nint main(void){return 0;}\n"


def test_add_include_first_include_no_prelude():
    src = "int main(void){return 0;}\n"
    out = _add_include(src, "time.h")
    assert out == "#include <time.h>\nint main(void){return 0;}\n"


def test_has_include_detection():
    from kaisen.autofix import _has_include
    assert _has_include("#include <time.h>\n", "time.h")
    assert not _has_include("#include <stdio.h>\n", "time.h")


def test_replace_token_whole_word_only():
    out = _replace_token("int x = 1;\nint xyz = 2;", "x", "y")
    assert out == "int y = 1;\nint xyz = 2;"


def test_apply_fix_include_and_mean():
    src = "#include <stdio.h>\n"
    assert apply_fix(src, "include", "time.h", "") == "#include <stdio.h>\n#include <time.h>\n"
    out = apply_fix("vec_dot();\n", "mean", "vec_dot", "ggml_vec_dot")
    assert out == "ggml_vec_dot();\n"


def test_add_gnu_source_precedes_includes():
    src = "#include <stdio.h>\n#include <time.h>\nint main(void){return 0;}\n"
    out = _add_gnu_source(src)
    assert "#define _GNU_SOURCE" in out
    assert out.index("#define _GNU_SOURCE") < out.index("#include <stdio.h>")
    # time.h was already there: no duplicate
    assert out.count("#include <time.h>") == 1


def _fake_build_fail_then_ok(err_stderr):
    """run_build that fails once with err_stderr, then succeeds."""
    state = {"n": 0}

    def run_build(cmd):
        state["n"] += 1
        if state["n"] == 1:
            return {"ok": False, "stderr": err_stderr}
        return {"ok": True, "stderr": ""}

    return run_build


def test_autofix_include_loop(tmp_path):
    src = tmp_path / "candidate.c"
    src.write_text("#include <stdio.h>\nint main(void){return 0;}\n")
    ok, applied, _ = autofix_build(
        src, ["gcc", "x"],
        _fake_build_fail_then_ok("fatal error: did you forget to #include <time.h>?"),
    )
    assert ok
    assert applied == ["include <time.h>"]
    assert "#include <time.h>" in src.read_text()


def test_autofix_simd_pragma_avx2(tmp_path):
    src = tmp_path / "candidate.c"
    src.write_text("int main(void){return 0;}\n")
    ok, applied, _ = autofix_build(
        src, ["gcc", "x"],
        _fake_build_fail_then_ok(
            "error: inlining failed in call to ‘always_inline’: target specific option mismatch"),
    )
    assert ok
    assert applied == ["pragma GCC target avx2"]
    assert src.read_text().startswith('#pragma GCC target("avx2,fma")\n')


def test_autofix_simd_pragma_avx512(tmp_path):
    src = tmp_path / "candidate.c"
    src.write_text("int main(void){return 0;}\n")
    ok, applied, _ = autofix_build(
        src, ["gcc", "x"],
        _fake_build_fail_then_ok(
            "error: AVX512 vector return without AVX512 enabled (__m512)"),
    )
    assert ok
    assert applied == ["pragma GCC target avx512"]


def test_autofix_vec_const_drop(tmp_path):
    src = tmp_path / "candidate.c"
    src.write_text("static const __m256 k = _mm256_set1_ps(1.0f);\nint main(void){return 0;}\n")
    ok, applied, _ = autofix_build(
        src, ["gcc", "x"],
        _fake_build_fail_then_ok("error: initializer element is not constant"),
    )
    assert ok
    assert applied == ["drop const on static SIMD vector"]
    assert "static __m256 k" in src.read_text()


def test_autofix_literal_newline_unescape(tmp_path):
    src = tmp_path / "candidate.c"
    src.write_text("int main(void){\\n  return 0;\\n}\\n")
    ok, applied, _ = autofix_build(
        src, ["gcc", "x"],
        _fake_build_fail_then_ok("error: stray ‘\\’ in program"),
    )
    assert ok
    assert applied == [r"unescape literal \n"]
    assert "\n" in src.read_text() and "\\n" not in src.read_text()


def test_autofix_avxintrin_include_rewrite(tmp_path):
    src = tmp_path / "candidate.c"
    src.write_text("#include <avxintrin.h>\nint main(void){return 0;}\n")
    ok, applied, _ = autofix_build(
        src, ["gcc", "x"],
        _fake_build_fail_then_ok("fatal error: avxintrin.h: No such file or directory"),
    )
    assert ok
    assert applied == ["avxintrin.h -> immintrin.h"]
    assert "#include <immintrin.h>" in src.read_text()


def test_autofix_no_hints_gives_up(tmp_path):
    src = tmp_path / "candidate.c"
    src.write_text("int main(void){return 0;}\n")
    tries = 0

    def run_build(cmd):
        nonlocal tries
        tries += 1
        return {"ok": False, "stderr": "error: something we cannot auto-fix"}

    ok, applied, _ = autofix_build(src, ["gcc", "x"], run_build)
    assert not ok and applied == []
    assert tries == 1  # no fixes applicable -> no rebuild churn


def test_autofix_reverts_breaking_warning_fix(tmp_path):
    """apply_on_success: a warning-hint fix that breaks the build is reverted."""
    src = tmp_path / "candidate.c"
    src.write_text("#include <stdio.h>\nint main(void){return 0;}\n")
    n = 0

    def run_build(cmd):
        nonlocal n
        n += 1
        if n == 1:
            return {"ok": True, "stderr": "warning: did you forget to #include <time.h>?"}
        return {"ok": False, "stderr": "error: after include, real failure"}

    ok, applied, _ = autofix_build(src, ["gcc", "x"], run_build, apply_on_success=True)
    assert ok and applied == []
    assert "#include <time.h>" not in src.read_text()


def test_autofix_gnu_then_include_ordering(tmp_path):
    """POSIX clock error + a missing include in the same build: the define
    must still precede the added include in the final file."""
    src = tmp_path / "candidate.c"
    src.write_text("int main(void){return 0;}\n")
    stderr = ("error: ‘CLOCK_MONOTONIC’ undeclared (first use in this function)\n"
              "fatal error: did you forget to #include <string.h>?")
    state = {"n": 0}

    def run_build(cmd):
        state["n"] += 1
        if state["n"] <= 3:
            return {"ok": False, "stderr": stderr}
        return {"ok": True, "stderr": ""}

    ok, applied, _ = autofix_build(src, ["gcc", "x"], run_build)
    assert ok
    assert any("_GNU_SOURCE" in f for f in applied)
    text = src.read_text()
    assert text.index("#define _GNU_SOURCE") < text.index("#include <time.h>")
    assert text.index("#define _GNU_SOURCE") < text.index("#include <string.h>")


def test_resolve_mode_matrix():
    assert resolve_mode({"skills": {"autofix_build": False}}) == "off"
    assert resolve_mode({"skills": {"autofix_build": True}}) == "default"
    assert resolve_mode({"skills": {}}) == "default"
    assert resolve_mode({"language": "c", "skills": {}}) == "default"
    assert resolve_mode({"language": "c++", "skills": {}}) == "default"
    assert resolve_mode({"language": "python", "skills": {}}) == "python"
    assert resolve_mode({"language": "rust", "skills": {}}) == "off"
    assert resolve_mode({"skills": {"autofix_build": "harness/fixer.py"}}) == \
        ("custom", "harness/fixer.py")


def test_avx_include_regex():
    assert _AVX_INCLUDE_RE.search("#include <avxintrin.h>")
    assert _AVX_INCLUDE_RE.search("#include <fmaintrin.h>")
    assert not _AVX_INCLUDE_RE.search("#include <immintrin.h>")
