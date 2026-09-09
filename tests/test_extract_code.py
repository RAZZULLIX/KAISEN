"""extract_code must return the FINAL answer code block, not a reasoning
snippet.  Original-KAISEN semantics ("largest AND last working codeblock"):
the real program is the LAST ``` block that contains actual code (includes/
entry point); scratch snippets a model emits while thinking usually lack
them and must be skipped, even when they happen to be longer.
extract_code_candidates returns the ordered best-first list (latest block
first) so the engine can fall back to a previous block if the build fails."""
from kaisen.skills import extract_code, extract_code_candidates


def test_picks_last_real_code_block_over_larger_thinking_snippet():
    """Qwen/DeepSeek think aloud with ``` snippets, then emit the program
    LAST.  The larger thinking snippet must NOT win."""
    text = (
        "```python\n"
        "x = [i for i in range(1000000)]  # a long scratch snippet\n"
        "\n"
        "```\n"
        "Let me think about the loop.\n"
        "```python\n"
        "print('hello')\n"
        "```\n"
    )
    out = extract_code(text, "python")
    assert out is not None
    assert "hello" in out
    assert "range(1000000)" not in out


def test_prefers_language_fence_then_last():
    """Multiple ```c blocks: the LAST ```c block is the answer."""
    text = (
        "```c\n#include <stdio.h>\nint x;\n```\n"
        "```c\n#include <stdio.h>\nint main(){return 0;}\n```\n"
    )
    out = extract_code(text, "c")
    assert "int main" in out and "int x;" not in out


def test_thinking_snippet_without_includes_is_skipped():
    """A generic fence that has no include/entry point is not a candidate."""
    text = (
        "```\nint x = 5; // snippet\n```\n"
        "final answer:\n"
        "```c\n#include <stdio.h>\nint main(){return 0;}\n```\n"
    )
    out = extract_code(text, "c")
    assert "int main" in out


def test_trailing_tiny_fence_falls_back_to_largest():
    """The last block is a 3-char fence (abort) -> take the largest."""
    text = (
        "```c\n#include <stdio.h>\nint main(){return 0;}\n```\n"
        "```c\nabc\n```\n"  # trivial trailing fence
    )
    out = extract_code(text, "c")
    assert "int main" in out


def test_marker_channel_last_wins_for_gptoss():
    """gpt-oss final-answer channel: content after the last <|...|> marker's
    starter is the answer (matches original extract_largest_c_code)."""
    text = (
        "<|channel|>analysis<|message|>I will write code.\n"
        "```c\nint x;\n```\n<|end|>\n"
        "assistant\n```c\n#include <stdio.h>\nint main(){return 0;}\n```\n"
    )
    out = extract_code(text, "c")
    assert "int main" in out


def test_no_fence_bare_scan_fallback():
    out = extract_code("#include <stdio.h>\nint main(){return 0;}\n", "c")
    assert out is not None and "int main" in out


def test_working_code_inside_reasoning_is_picked_when_answer_absent():
    """Reasoning is truncated (cut mid-thought) but contains a complete,
    working program — no final answer block exists.  The reasoning block
    must be the result; losing it is losing the generation."""
    text = (
        "I'll use fast doubling:\n"
        "```rust\nfn main() { println!(\"{} \", fib(10)); }\n```\n"
        "(reasoning continues but gets cut...\n"
    )
    out = extract_code(text, "rust")
    assert out is not None and "fn main()" in out


def test_reasoning_program_beats_tiny_final_fragment():
    """A real program sit in the reasoning, then the 'answer' is a 3-char
    fragment (an aborted turn).  The working reasoning block wins."""
    text = (
        "```rust\nfn main() { let s = fib(40); println!(\"{}\", s); }\n```\n"
        "final:\n```rust\nabc\n```\n"
    )
    out = extract_code(text, "rust")
    assert "fib(40)" in out and out.strip() != "abc"


def test_last_working_program_wins_over_reasoning_scratch():
    """When BOTH a reasoning scratch and a final answer are present, the
    final answer (last working block) wins — reasoning scratch is skipped."""
    text = (
        "```rust\nfn helper() {}\n```\n"
        "the real answer:\n"
        "```rust\nfn main() { println!(\"done\"); }\n```\n"
    )
    out = extract_code(text, "rust")
    assert "println!" in out and "helper" not in out


# ---------------------------------------------------------------------- #
# extract_code_candidates — ordered best-first list for candidate fallback
# ---------------------------------------------------------------------- #

def test_candidates_ordered_latest_first():
    """The engine tries the LATEST block first, then earlier ones, up to
    the limit — 'try the latest, if it doesn't build try the previous'."""
    text = (
        "```c\nint a;\n```\n"
        "```c\n#include <stdio.h>\nint main(){ return 1; }\n```\n"
        "```c\n#include <stdio.h>\nint main(){ return 2; }\n```\n"
    )
    cands = extract_code_candidates(text, "c", limit=3)
    assert "return 2;" in cands[0]   # latest first
    assert "return 1;" in cands[1]
    # the leading `int a;` block is a trivial (<8 char) fragment — dropped
    assert len(cands) == 2


def test_candidates_limited_and_dedup():
    text = ("```c\n#include <stdio.h>\nint main(){return 0;}\n```\n"
            "```c\n#include <stdio.h>\nint main(){return 0;}\n```\n"
            "```c\n#include <stdio.h>\nint main(){return 1;}\n```\n")
    cands = extract_code_candidates(text, "c", limit=2)
    assert len(cands) <= 2
    assert len(set(cands)) == len(cands)  # no duplicate candidates


def test_candidates_skips_trivial_fragments():
    """A 3-char trailing fence (aborted turn) is not a candidate."""
    text = ("```c\n#include <stdio.h>\nint main(){return 0;}\n```\n```c\nabc\n```\n")
    cands = extract_code_candidates(text, "c", limit=3)
    assert "int main" in cands[0]
    assert all(len(c.strip()) >= 8 for c in cands)


def test_candidates_falls_back_to_primary_when_no_fences():
    out = extract_code_candidates("#include <stdio.h>\nint main(){return 0;}\n", "c")
    assert out and "int main" in out[0]
