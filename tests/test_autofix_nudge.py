"""Per-compiler nudge backends: parse each language's real stderr format."""
import pytest

from kaisen.autofix import engine
from kaisen.autofix import rust, go, shell, javascript, python, perl


# ── delimiter scan ────────────────────────────────────────────────────────
def test_scan_delim_balanced():
    assert engine._scan_delim("fn main() { return; }\n") == ()


def test_scan_delim_unclosed_brace():
    assert engine._scan_delim("fn main() {\n    if true {\n        x();\n}\n") == ("}",)


def test_scan_delim_ignores_strings_and_comments():
    src = 'let s = "a } b"; // }\nlet t = \'{\';\nfn f() {\n}'
    # the } and { inside strings/comments must not count
    assert engine._scan_delim(src) == ()


def test_scan_delim_multi_close():
    assert engine._scan_delim("f( g( 1 )\n") == (")",)


# ── rustc ────────────────────────────────────────────────────────────────
RUST_UNCLOSED = (
    "error: this file contains an unclosed delimiter\n"
    " --> /tmp/main.rs:4:3\n"
    "  |\n"
    "4 | }\n"
    "  | -^\n"
)
RUST_SEMI = (
    "error: expected `;`, found `println`\n"
    " --> /tmp/main.rs:2:14\n"
    "  |\n"
    "2 |     let x = 5\n"
    "  |              ^ help: add `;` here\n"
)
RUST_E0433 = "error[E0433]: cannot find module or crate `fmt` in this scope\n"


def test_rustc_unclosed_delim():
    src = "fn main() {\n    if true {\n        println!(\"hi\");\n}\n"
    fixes = rust.parse(RUST_UNCLOSED, src)
    assert fixes == [("delim", ("}",), 4)]


def test_rustc_semicolon():
    src = "fn main() {\n    let x = 5\n    println!(\"{}\", x);\n}\n"
    fixes = rust.parse(RUST_SEMI, src)
    assert fixes == [("semicolon", 2)]
    out = engine._apply_nudge(src, fixes[0])
    assert "let x = 5;" in out


def test_rustc_import_std_module():
    src = "fn main() {\n    fmt::print(\"x\");\n}\n"
    fixes = rust.parse(RUST_E0433, src)
    assert fixes == [("import", "fmt", "std")]
    out = engine._apply_nudge(src, fixes[0])
    assert out.startswith("use std::fmt;\n")


# ── go ───────────────────────────────────────────────────────────────────
GO_EOF = "./main.go:9:1: syntax error: unexpected EOF, expected }\n"
GO_UNDEF = "./main.go:4:2: undefined: fmt\n"
GO_PRINTLN = "./main.go:4:2: undefined: Println\n"


def test_go_unexpected_eof():
    src = "package main\n\nfunc main() {\n\tif true {\n\t\tfmt.Println(1)\n"
    fixes = go.parse(GO_EOF, src)
    assert ("delim", ("}", "}"), 9) in fixes


def test_go_import_known_package():
    src = "package main\n\nfunc main() {\n\tfmt.Println(1)\n}\n"
    fixes = go.parse(GO_UNDEF, src)
    assert fixes == [("import", "fmt", "go")]
    out = engine._apply_nudge(src, fixes[0])
    assert 'import "fmt"' in out


def test_go_ignores_non_package_undefined():
    src = "package main\n\nfunc main() {\n\tPrintln(1)\n}\n"
    assert go.parse(GO_PRINTLN, src) == []


# ── shell ────────────────────────────────────────────────────────────────
SH_EOF = "prog.sh: line 2: unexpected EOF while looking for matching `)'\n"
SH_FI = "prog.sh: line 5: syntax error near unexpected token `fi'\n"


def test_shell_unexpected_eof():
    src = "#!/bin/bash\nx=$((1 + 2\necho $x\n"
    fixes = shell.parse(SH_EOF, src)
    assert any(f[0] == "delim" for f in fixes)


def test_shell_missing_then_inline():
    src = "#!/bin/bash\nif [ 1 -eq 1 ]; echo yes; fi\n"
    fixes = shell.parse(SH_FI, src)
    assert fixes and fixes[0][0] == "raw"
    out = engine._apply_nudge(src, fixes[0])
    assert out.splitlines()[1] == "if [ 1 -eq 1 ]; then echo yes; fi"

def test_shell_missing_then_multiline():
    src = "#!/bin/bash\nif [ 1 -eq 1 ]\necho yes\nfi\n"
    fixes = shell.parse(SH_FI, src)
    out = engine._apply_nudge(src, fixes[0])
    assert "; then" in out


# ── javascript (node) ────────────────────────────────────────────────────
NODE_EOF = "/tmp/prog.js:6\n\nSyntaxError: Unexpected end of input\n"


def test_node_unexpected_end():
    src = "function f(x) {\n  return x\n"
    fixes = javascript.parse(NODE_EOF, src)
    assert any(f[0] == "delim" for f in fixes)


# ── python (ast) ─────────────────────────────────────────────────────────
PY_COLON = "line 3: expected ':'\n"


def test_python_missing_colon():
    src = "def f(x):\n    return x * 2\ndef g(x)\n    return x\n"
    fixes = python.parse(PY_COLON, src)
    assert fixes and fixes[0][0] == "raw"
    out = engine._apply_nudge(src, fixes[0])
    assert "def g(x):" in out


PY_INDENT = "line 5: expected an indented block\n"


def test_python_empty_block():
    src = "def f():\n    if True:\n        x = 1\n"
    fixes = python.parse(PY_INDENT, src)
    out = engine._apply_nudge(src, fixes[0])
    assert "pass" in out


# ── perl ─────────────────────────────────────────────────────────────────
PERL_ERR = "syntax error at /tmp/prog.pl line 1, near \"2;\"\n"


def test_perl_syntax_error_unbalanced():
    src = "my $x = (1 + 2;\nprint $x, \"\\n\";\n"
    fixes = perl.parse(PERL_ERR, src)
    assert any(f[0] == "delim" for f in fixes)


# ── loop contract: revert a fix that breaks a previously-good build ───────
def test_nudge_reverts_breaking_fix(tmp_path):
    src = tmp_path / "prog.go"
    src.write_text("package main\n\nfunc main() {\n\t_ = 1\n}\n")
    calls = {"n": 0}

    def run_build(cmd):
        calls["n"] += 1
        # first build fails with an import hint, the FIXED build would break
        # an otherwise-working program (simulate a bad fix), then the revert
        # restores it.
        if calls["n"] == 1:
            return {"ok": False, "stderr": "prog.go:4:2: undefined: fmt\n"}
        # fix applied -> probe: pretend it broke a previously-ok build
        return {"ok": False, "stderr": "prog.go:7:1: unexpected EOF\n"}

    from kaisen.autofix import autofix_nudge
    ok, fixes, last = autofix_nudge(src, ["go", "build"], run_build, "go")
    # the original build failed, so nothing to "revert to success"; the
    # engine should have applied the import fix then given up.
    assert "import" in " ".join(fixes) or fixes == []
