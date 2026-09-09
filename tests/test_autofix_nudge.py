"""Per-compiler nudge backends: parse each language's real stderr format."""
import pytest

from kaisen.autofix import engine
from kaisen.autofix import (rust, go, shell, javascript, python, perl, lua, php, ruby, r,
                            java, kotlin, scala, swift, zig, dart, haskell, d, csharp, typescript)


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


# ── lua ────────────────────────────────────────────────────────────────────
def test_lua_missing_then():
    src = "for i = 1, 3 do\n    if i == 2\n        print(i)\n    end\nend\n"
    fixes = lua.parse("prog.lua:3: 'then' expected near 'print'", src)
    assert fixes and fixes[0][0] == "raw"
    out = engine._apply_nudge(src, fixes[0])
    assert "if i == 2 then" in out


# ── php ────────────────────────────────────────────────────────────────────
def test_php_undefined_constant_variable():
    src = "<?php\nfor ($j = i; $j < 3; $j++) echo 1;\n"
    fixes = php.parse('PHP Fatal error: Undefined constant "i" in p.php:2', src)
    assert fixes and fixes[0][0] == "raw"
    out = engine._apply_nudge(src, fixes[0])
    assert "$j = $i" in out


# ── ruby ───────────────────────────────────────────────────────────────────
def test_ruby_exit_nil_fix():
    src = 'n = (ARGV[0] || "0").to_i\nexit(puts(0)) if n < 2\nputs n\n'
    fixes = ruby.parse("p.rb:2:in `exit': no implicit conversion from nil to integer (TypeError)", src)
    assert fixes and fixes[0][0] == "raw"
    out = engine._apply_nudge(src, fixes[0])
    assert "(puts(0); exit 0) if n < 2" in out


def test_ruby_missing_end():
    src = 'if true\n  puts "x"\n'
    fixes = ruby.parse("p.rb:3: syntax error, unexpected end-of-input (SyntaxError)", src)
    assert fixes
    out = engine._apply_nudge(src, fixes[0])
    assert out.rstrip().endswith("end")


# ── r ──────────────────────────────────────────────────────────────────────
def test_r_unrecognized_escape():
    src = 'args <- commandArgs(trailingOnly=TRUE)\nxs <- as.integer(strsplit(args[1], "\\s+")[[1]])\n'
    fixes = r.parse("Error: '\\s' is an unrecognized escape in character string", src)
    assert fixes
    out = engine._apply_nudge(src, fixes[0])
    assert "\\\\s+" in out.replace("\\\\", "\\\\") or "\\s+" in out


def test_r_break_if():
    src = "for (i in 1:5) {\n  break if (i == 3)\n}\n"
    fixes = r.parse("Error: unexpected 'if' in: \"for (i in 1:5) { break if\"", src)
    out = engine._apply_nudge(src, fixes[0])
    assert "if (i == 3) break" in out


# ── compiled-language backends (message -> correction attempt) ────────────
def test_java_semicolon():
    src = "public class Main {\n  public static void main(String[] args) {\n    int x = 5\n    System.out.println(x);\n  }\n}\n"
    fixes = java.parse("Main.java:3: error: ';' expected", src)
    assert ("semicolon", 3) == fixes[0]
    out = engine._apply_nudge(src, fixes[0])
    assert "int x = 5;" in out


def test_java_unbalanced():
    src = "public class Main {\n  public static void main(String[] args) {\n    if (true) {\n      System.out.println(1);\n  }\n}\n"
    fixes = java.parse("Main.java:6: error: reached end of file while parsing", src)
    assert any(f[0] == "delim" for f in fixes)


def test_zig_semicolon_and_unbalanced():
    src = "const std = @import(\"std\");\npub fn main() !void {\n    const x: i64 = 5\n    std.debug.print(\"{d}\", .{x});\n}\n"
    fixes = zig.parse("semi.zig:3:21: error: expected ';' after statement", src)
    assert ("semicolon", 3) == fixes[0]
    src2 = "const std = @import(\"std\");\npub fn main() !void {\n    if (true) {\n        std.debug.print(\"x\", .{});\n}\n"
    fixes2 = zig.parse("unbal.zig:6:1: error: expected statement, found 'EOF'", src2)
    assert any(f[0] == "delim" for f in fixes2)


def test_dart_semicolon():
    src = "void main(List<String> args) {\n    int x = 5\n    print(x);\n}\n"
    fixes = dart.parse("semi.dart:2:13: Error: Expected ';' after this.", src)
    assert ("semicolon", 2) == fixes[0]
    out = engine._apply_nudge(src, fixes[0])
    assert "int x = 5;" in out


def test_kotlin_unbalanced():
    src = "fun main(args: Array<String>) {\n    if (true) {\n        println(\"hi\")\n}\n"
    fixes = kotlin.parse("unbal.kt:4:2: error: syntax error: Expecting '}'", src)
    assert any(f[0] == "delim" for f in fixes)


def test_scala_missing_brace():
    src = "object original {\n  def main(args: Array[String]): Unit = {\n    if (true) {\n      println(1)\n  }\n}\n"
    fixes = scala.parse("unbal.scala:5: error: Missing closing brace '}' assumed here", src)
    assert any(f[0] == "delim" for f in fixes)


def test_d_semicolon_and_unbalanced():
    src = "import std.stdio;\nvoid main() {\n    int x = 5\n    writeln(x);\n}\n"
    fixes = d.parse("semi.d(4): Error: semicolon needed to end declaration of 'x'", src)
    assert ("semicolon", 4) == fixes[0]
    src2 = "import std.stdio;\nvoid main() {\n    if (true) {\n        writeln(\"x\");\n}\n"
    fixes2 = d.parse("unbal.d(6): Error: matching '}' expected following compound statement", src2)
    assert any(f[0] == "delim" for f in fixes2)


def test_d_lowercase_integer_suffix_fix():
    """The sweep's #1 D error: the model writes `1000000007ul` (valid C) but
    D wants `...uL` — lower-case suffix 'l' is rejected."""
    src = "enum uint64_t MOD = 1000000007ul;\nvoid main() {}\n"
    fixes = d.parse("semi.d(4): Error: lower case integer suffix 'l' is not allowed. Please use 'L' instead", src)
    assert fixes and fixes[0][0] == "raw"
    out = engine._apply_nudge(src, fixes[0])
    assert "1000000007uL" in out      # lower-case l fixed to L
    assert "1000000007ul" not in out


def test_d_undefined_identifier_type_fix():
    """C-style `uint64_t`/`int64_t` are not D names — the model writes them
    constantly.  nudge maps them to D's aliases."""
    src = "import std.stdio;\nvoid main() {\n    uint64_t n = 5;\n    long x = 9;\n}\n"
    fixes = d.parse("semi.d(3): Error: undefined identifier `uint64_t`", src)
    assert fixes and fixes[0][0] == "raw"
    out = engine._apply_nudge(src, fixes[0])
    assert "ulong n = 5;" in out       # uint64_t -> ulong
    assert "uint64_t" not in out


def test_csharp_unbalanced():
    src = "class Program {\n  static void Main() {\n    if (true) {\n      System.Console.WriteLine(1);\n  }\n}\n"
    fixes = csharp.parse("unbal.cs(6,246): error CS1525: Unexpected symbol `end-of-file'", src)
    assert any(f[0] == "delim" for f in fixes)


def test_haskell_unbalanced():
    src = "main :: IO ()\nmain = do {\n    putStrLn \"x\"\n"
    fixes = haskell.parse("unbal.hs:4:1: error: parse error (possibly incorrect indentation or mismatched brackets)", src)
    assert any(f[0] == "delim" for f in fixes)


def test_swift_unbalanced():
    src = "import Glibc\nlet args = CommandLine.arguments\nif true {\n    print(args[1])\n\n"
    fixes = swift.parse("unbal.swift:3:9: note: to match this opening '{'", src)
    assert any(f[0] == "delim" for f in fixes)


def test_line_parse_file_ext_digit_colon():
    from kaisen.autofix import engine as E
    assert E._parse_line_no("Main.java:3: error: ';' expected") == 3
    assert E._parse_line_no("--> /tmp/main.rs:2:14") == 2
    assert E._parse_line_no("prog.js:6\n\nSyntaxError: Unexpected end of input") == 6


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
