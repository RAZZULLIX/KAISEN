"""extract_code must return the FINAL answer code block, not a reasoning
snippet.  Original-KAISEN semantics ("largest AND last working codeblock"):
the real program is the LAST ``` block that contains actual code (includes/
entry point); scratch snippets a model emits while thinking usually lack
them and must be skipped, even when they happen to be longer."""
from kaisen.skills import extract_code


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
