"""One-change diff guard (P1.13): line-level change counting."""
from kaisen.engine import count_changed_lines


def test_single_line_edit_counts_one():
    assert count_changed_lines("int x = 5;\n", "int x = 7;\n") == 1


def test_replaced_line_within_file_counts_one():
    a = "a\nb\nc\nd\n"
    b = "a\nb\nX\nd\n"
    assert count_changed_lines(a, b) == 1


def test_inserted_line_counts_one():
    assert count_changed_lines("a\nb\n", "a\nb\nc\n") == 1


def test_identical_counts_zero():
    assert count_changed_lines("a\nb\n", "a\nb\n") == 0


def test_whole_rewrite_counts_many():
    assert count_changed_lines("a\nb\nc\n", "x\ny\nz\nq\n") >= 3


def test_empty_baseline_all_adds():
    assert count_changed_lines("", "a\nb\n") == 2
