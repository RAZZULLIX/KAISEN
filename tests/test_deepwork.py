"""Deepwork agent loop + tooling: command parsing (the bug that made the
loop phantom-read every turn and never produce a memo), small-model
tolerance, min-reads enforcement, and the guardrailed pandas tool."""
import pytest

from kaisen.skills import (
    DeepworkAgent,
    ResultsStore,
    find_candidate_source,
    format_top_rows,
)


# ----------------------------------------------------------------------
# agent loop: parsing + memo contract
# ----------------------------------------------------------------------

def _tools(read=None):
    calls = {"list": [], "read": [], "diff": [], "pandas": [], "lesson": [], "memo": []}
    def _read(args):
        calls["read"].append(args)
        if read:
            return read(args)
        return "READ-OK"
    return {
        "LIST": lambda a: calls["list"].append(a) or "LIST-OK",
        "READ": _read,
        "DIFF": lambda a: calls["diff"].append(a) or "DIFF-OK",
        "PANDAS": lambda a: calls["pandas"].append(a) or "PANDAS-OK",
        "LESSON": lambda a: calls["lesson"].append(a) or "LESSON-OK",
        "MEMO": lambda a: calls["memo"].append(a) or "MEMO-OK",
    }, calls


def _agent(replies, tools, min_reads=2, max_turns=10):
    it = iter(replies)
    return DeepworkAgent("PROMPT", tools, lambda _: next(it),
                         max_turns=max_turns, min_reads=min_reads)


def test_commands_execute_and_memo_returns():
    tools, calls = _tools()
    a = _agent([
        "LIST 5\nDIFF 42",
        "READ 42\nREAD 43",
        "<DEEPWORK_MEMO>\nStudy the winners",
    ], tools)
    assert a.run() == "Study the winners"
    assert calls["list"] == ["5"]
    assert calls["diff"] == ["42"]
    assert calls["read"] == ["42", "43"]


def test_prose_never_parsed_as_commands():
    """Regression: 'Rationale: list the winners' must NOT become a LIST/
    READ call, and '{YELOOK} 10' must not match either (the old pattern
    matched 'y' inside YELOOK and 'r' inside Rationale and ate the line)."""
    tools, calls = _tools()
    a = _agent([
        "Rationale: list the winners\nLIST 3",
        "Rationale: read the best one\nREAD 12\nREAD 13",
        "<DEEPWORK_MEMO>\nDone",
    ], tools)
    assert a.run() == "Done"
    assert calls["list"] == ["3"]          # the real commands, and ONLY them
    assert calls["read"] == ["12", "13"]
    assert len(calls["read"]) == 2         # no phantom reads from Rationale


def test_small_model_tolerance():
    """Lowercase, colon, hyphen, trailing prose, gen prefix — the PARSER
    accepts all of these; the engine's _gen_token then extracts the number."""
    tools, calls = _tools()
    a = _agent([
        "list: 10 top\nread-42\nREAD gen 43\nDIFF 44 please\nLESSON",
        "READ 45.\nREAD 46\nREAD 47",
        "<DEEPWORK_MEMO>\nMemo",
    ], tools)
    assert a.run() == "Memo"
    assert calls["list"] == ["10 top"]
    assert calls["read"] == ["42", "gen 43", "45.", "46", "47"]
    assert calls["diff"] == ["44 please"]
    assert calls["lesson"] == [""]


def test_gen_token_extracts_numbers_tolerantly():
    """Engine-side arg parsing: '42', 'gen 43', 'gen_000044', 'the winner
    45.', trailing prose — all resolve; paths and junk never pass."""
    from kaisen.engine import ProjectEngine
    tok = ProjectEngine._gen_token
    assert tok("42") == "42"
    assert tok("gen 43") == "43"
    assert tok("gen_000044") == "gen_000044"
    assert tok("the winner 45.") == "45"
    assert tok("READ 46, please") == "46"
    assert tok("../config.json") == ""     # paths are NEVER a generation
    assert tok("../../etc/passwd") == ""
    assert tok("") == ""


def test_memo_requires_min_successful_reads():
    tools, calls = _tools()
    a = _agent([
        "<DEEPWORK_MEMO>\nToo soon",
        "READ 1\n<DEEPWORK_MEMO>\nStill only one",
        "READ 2\n<DEEPWORK_MEMO>\nTwo reads now",
    ], tools)
    assert a.run() == "Two reads now"
    assert calls["read"] == ["1", "2"]


def test_failed_reads_do_not_count():
    tools, calls = _tools(read=lambda a: "READ-OK" if a.strip() != "99" else "ERROR: no such gen")
    a = _agent([
        "READ 99",
        "READ 99\n<DEEPWORK_MEMO>\nOne successful read only",
        "READ 1\n<DEEPWORK_MEMO>\nOne successful read still",
        "READ 5\n<DEEPWORK_MEMO>\nTwo successful reads",
    ], tools)
    assert a.run() == "Two successful reads"
    # second "READ 99" came from the cache (tool not re-invoked), and
    # neither of its errors counted toward min_reads
    assert calls["read"] == ["99", "1", "5"]


def test_cached_commands_run_once():
    tools, calls = _tools()
    a = _agent([
        "LIST 5\nLIST 5",
        "READ 1\n<DEEPWORK_MEMO>\nOne read is not enough",
        "READ 2\n<DEEPWORK_MEMO>\nMemo",
    ], tools)
    assert a.run() == "Memo"
    assert calls["list"] == ["5"]  # second LIST served from cache


def test_timeout_without_memo():
    tools, calls = _tools()
    a = _agent(["no command here", "still nothing", "silence"], tools, max_turns=3)
    assert a.run() == "DEEPWORK TIMEOUT -- no memo produced."


# ----------------------------------------------------------------------
# listing: generic columns from the actual data
# ----------------------------------------------------------------------

def test_format_top_rows_generic_columns_and_sort():
    rows = [
        {"generation": "1", "outcome": "NEW_BEST", "fitness": "3.5", "ms": "1.2", "err": "0.01"},
        {"generation": "2", "outcome": "valid", "fitness": "2.0", "ms": "1.4", "err": "0.02"},
        {"generation": "3", "outcome": "build_fail"},
        {"generation": "4", "outcome": "valid", "fitness": "9.9", "ms": "0.8", "err": "0.005", "mems": "44"},
    ]
    out = format_top_rows(rows, 10)
    lines = out.split("\n")
    assert lines[0] == "3 scored / 1 not scored generations"
    # columns come from the data, not assumptions
    for col in ("generation", "outcome", "ms", "err", "mems", "fitness"):
        assert col in lines[1]
    # sorted by fitness: gen 4 (9.9) before gen 1 (3.5) before gen 2 (2.0)
    data_lines = [l for l in lines[2:] if l.startswith(("4 ", "1 ", "2 "))]
    assert data_lines[0].startswith("4 ")
    assert data_lines[1].startswith("1 ")
    assert data_lines[2].startswith("2 ")
    # failure tail present
    assert "recent failures:" in out
    assert "gen 3 build_fail" in out


def test_format_top_rows_no_failures_no_tail():
    rows = [{"generation": "1", "outcome": "NEW_BEST", "fitness": "1.0", "ms": "5"}]
    out = format_top_rows(rows, 10)
    assert "recent failures:" not in out
    assert "1 scored / 0 not scored" in out


def test_format_top_rows_respects_direction():
    """lower-is-better projects (time_ms, err) must list the SMALLEST
    fitness first — the old hardcoded listing showed the worst as best."""
    rows = [
        {"generation": "1", "outcome": "valid", "fitness": "3.5", "ms": "3.5"},
        {"generation": "2", "outcome": "valid", "fitness": "9.9", "ms": "9.9"},
    ]

    import re as _re

    def first_data(out):
        return [l for l in out.split("\n") if _re.match(r"^\d+ valid", l)][0]

    assert first_data(format_top_rows(rows, 10, higher_is_better=True)).startswith("2 ")
    assert first_data(format_top_rows(rows, 10, higher_is_better=False)).startswith("1 ")


def test_format_top_rows_clamps_and_empty():
    assert format_top_rows([], 10) is not None
    rows = [{"generation": str(i), "outcome": "valid", "fitness": str(i)} for i in range(60)]
    out = format_top_rows(rows, 50)
    import re as _re
    data = [l for l in out.split("\n") if _re.match(r"^\d+ valid", l)]
    assert len(data) == 50


# ----------------------------------------------------------------------
# candidate source finding: works for any project language
# ----------------------------------------------------------------------

def test_find_candidate_preferred(tmp_path):
    (tmp_path / "candidate.py").write_text("x=1")
    (tmp_path / "program.py").write_text("y=2")
    p = find_candidate_source(tmp_path, ".py")
    assert p is not None and p.name == "candidate.py"


def test_find_program_fallback(tmp_path):
    (tmp_path / "program.c").write_text("int main(){}")
    p = find_candidate_source(tmp_path, ".c")
    assert p is not None and p.name == "program.c"


def test_find_any_source_with_project_ext(tmp_path):
    (tmp_path / "random.rs").write_text("fn main(){}")
    p = find_candidate_source(tmp_path, ".rs")
    assert p is not None and p.name == "random.rs"


def test_find_none_when_no_source(tmp_path):
    (tmp_path / "prompt.txt").write_text("hi")
    assert find_candidate_source(tmp_path, ".c") is None


# ----------------------------------------------------------------------
# guardrailed pandas: data analysis only, nothing else
# ----------------------------------------------------------------------

def _store(tmp_path, rows):
    s = ResultsStore(tmp_path)
    for r in rows:
        s.append(r)
    return s


def test_pandas_queries_work(tmp_path):
    s = _store(tmp_path, [
        {"generation": "1", "outcome": "NEW_BEST", "fitness": "3.5", "ms": "1.2"},
        {"generation": "2", "outcome": "valid", "fitness": "2.0", "ms": "1.4"},
    ])
    out = s.query("df[df.fitness > 3]['generation'].tolist()")
    assert "1" in out and "ERROR" not in out
    # small-model tolerance: result = prefix and backticks
    out = s.query("`result = df.fitness.max()`")
    assert "3.5" in out and "ERROR" not in out


def test_pandas_no_modules_no_os(tmp_path):
    """The old namespace exposed pd AND os — an agent could os.system() or
    pd.read_csv() any path. Profound guardrail: none of that exists."""
    s = _store(tmp_path, [{"generation": "1", "outcome": "valid", "fitness": "1.0"}])
    assert "ERROR" in s.query("pd.read_csv('/etc/passwd')")
    assert "ERROR" in s.query("__import__('os').system('id')")
    assert "ERROR" in s.query("os.system('id')")
    assert "ERROR" in s.query("open('/etc/passwd')")


def test_pandas_blocks_io_methods(tmp_path):
    """df.to_csv / df.to_pickle could write files; read_* could read paths;
    df.query()/df.eval() resolve names in the caller frame (where pd lives)."""
    s = _store(tmp_path, [{"generation": "1", "outcome": "valid", "fitness": "1.0"}])
    for evil in ("df.to_csv('/tmp/pwn')", "df.to_pickle('/tmp/pwn')",
                 "df['fitness'].plot()", "getattr(df, 'to_csv')('/tmp/pwn')",
                 "df.__class__", "df.query('fitness > 0')",
                 "df.eval('fitness')", "df.to_json('/tmp/pwn')",
                 "df['fitness'].to_csv('/tmp/pwn')"):
        assert "ERROR" in s.query(evil), evil


def test_pandas_rejects_statements(tmp_path):
    """Expression-only: assignments/statements/imports are not code."""
    s = _store(tmp_path, [{"generation": "1", "outcome": "valid", "fitness": "1.0"}])
    assert "ERROR" in s.query("import os")
    assert "ERROR" in s.query("df.to_csv('/tmp/x') if True else None")  # still expression, but IO blocked
    assert "not a valid expression" in s.query("x = 1")
