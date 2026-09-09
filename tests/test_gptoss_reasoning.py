"""gpt-oss hybrid-reasoning handling on raw /completion servers:

- raw prompts are opened in the model's NATIVE chat format (a bare prompt
  degrades gpt-oss into erratic continuations — verified against a live
  llama.cpp b10402 box);
- the reasoning channel is stripped from captured content so thinking
  never pollutes the answer or counts against it;
- non-gpt-oss servers and explicit chat_template=none keep the historical
  raw-prompt behavior;
- request_chat paths are not double-wrapped.

No network: requests.post / _post_stream are faked per-test via monkeypatch.
"""
import json

from kaisen import llm as L

MARKER = L.GPTOSS_FINAL_MARKER


def _gptoss(tmp_cfg, sid="t-gptoss"):
    return L.Server({"id": sid, "type": "llama", "model": "gpt-oss-20b",
                     "url": f"http://127.0.0.1:1/{sid}/completion"}, tmp_cfg), sid


def _other(tmp_cfg, model="qwen3-8b", sid="t-other"):
    return L.Server({"id": sid, "type": "llama", "model": model,
                     "url": f"http://127.0.0.1:1/{sid}/completion"}, tmp_cfg), sid


class FakeResponse:
    def __init__(self, body):
        self._body = body
        self.encoding = None

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _fake_post(monkeypatch, body):
    """Capture the outgoing payload; return a canned JSON response."""
    captured = {}

    def post(url, json=None, headers=None, timeout=None, stream=False):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(body)
    monkeypatch.setattr(L.requests, "post", post)
    return captured


# --------------------------------------------------------------------------- #
# strip_reasoning
# --------------------------------------------------------------------------- #

def test_strip_removes_analysis_prefix():
    text = f"<|channel|>analysis<|message|>thinking hard{MARKER}the answer"
    assert L.strip_reasoning(text) == "the answer"


def test_strip_drops_trailing_end_token():
    """gpt-oss closes its turn with <|end|>; marker-scanning consumers
    (DeepworkAgent's CoT cut) must never see a stray <token> in the
    captured reply or they truncate it to nothing."""
    text = f"<|channel|>analysis<|message|>thinking{MARKER}int main(){{return 0;}}\n<|end|>"
    out = L.strip_reasoning(text)
    assert out == "int main(){return 0;}"
    assert "<|" not in out


def test_strip_keeps_last_marker_when_repeated():
    text = f"junk{MARKER}first{MARKER}second"
    assert L.strip_reasoning(text) == "second"


def test_strip_noop_without_marker():
    plain = "```c\nint main(void){return 0;}\n```"
    assert L.strip_reasoning(plain) == plain
    assert L.strip_reasoning("") == ""
    assert L.strip_reasoning(None) is None


def test_strip_qwen_think_block():
    """Qwen3/DeepSeek-R1 reply: `` before the answer. Thinking must
    be dropped so extract_code sees only the code."""
    text = ("<think>\nThe user wants a Rust program considering fast "
            "doubling.\n</think>\n\n```rust\nfn main(){}\n```")
    out = L.strip_reasoning(text)
    assert out == "```rust\nfn main(){}\n```"


def test_strip_qwen_unclosed_think_gives_empty():
    """n_predict exhausted mid-thought: no closing tag -> nothing usable."""
    assert L.strip_reasoning("<think>\nI will design a fast algorithm...") == ""


def test_strip_qwen_think_with_answer_tag():
    text = "<think>analyzing</think>\n<answer>\n```rust\nfn main(){}\n```\n</answer>"
    out = L.strip_reasoning(text)
    assert "fn main(){}" in out and "<think>" not in out and "</think>" not in out


# --------------------------------------------------------------------------- #
# prompt wrapping (request, non-stream)
# --------------------------------------------------------------------------- #

def test_gptoss_raw_prompt_wrapped_and_reasoning_stripped(tmp_cfg, monkeypatch):
    s, _ = _gptoss(tmp_cfg)
    reply = f"<|channel|>analysis<|message|>let me think{MARKER}int main(void){{return 0;}}"
    cap = _fake_post(monkeypatch, {"content": reply, "tokens_predicted": 12})
    out = s.request("Write a C program that returns zero.")
    sent = cap["json"]["prompt"]
    assert sent.startswith("<|start|>system<|message|>")
    assert sent.endswith("<|start|>assistant")
    assert "Write a C program that returns zero." in sent
    assert out == "int main(void){return 0;}"


def test_non_gptoss_native_template_wrapped(tmp_cfg, monkeypatch):
    """Every instruct model on a raw /completion server gets its native
    framing — not just gpt-oss. qwen template: ChatML-style im_start."""
    s, _ = _other(tmp_cfg)
    cap = _fake_post(monkeypatch, {"content": "hello", "tokens_predicted": 1})
    out = s.request("Say hello.")
    sent = cap["json"]["prompt"]
    assert sent.startswith("<|im_start|>system")
    assert sent.rstrip().endswith("<|im_start|>assistant")
    assert "Say hello." in sent
    assert out == "hello"


def test_templated_true_skips_wrap(tmp_cfg, monkeypatch):
    """Multi-turn roll-your-own loops (deepwork/agent) pass templated=True:
    the server must NOT re-frame each turn in the chat template."""
    s, _ = _other(tmp_cfg)
    cap = _fake_post(monkeypatch, {"content": "ok", "tokens_predicted": 1})
    s.request("Assistant: prior turn", templated=True)
    assert cap["json"]["prompt"] == "Assistant: prior turn"


def test_chat_template_none_disables_wrap(tmp_cfg, monkeypatch):
    s, _ = _gptoss(tmp_cfg)
    s.chat_template = "none"
    cap = _fake_post(monkeypatch, {"content": "ok", "tokens_predicted": 1})
    s.request("Hi.")
    assert cap["json"]["prompt"] == "Hi."


def test_request_chat_not_double_wrapped(tmp_cfg, monkeypatch):
    s, _ = _gptoss(tmp_cfg)
    cap = _fake_post(monkeypatch, {"content": "ok", "tokens_predicted": 1})
    s.request_chat([{"role": "user", "content": "Hi."}])
    sent = cap["json"]["prompt"]
    assert sent.count("<|start|>system") == 1
    assert sent.endswith("<|start|>assistant")


def test_explicit_auto_config_resolves_to_native(tmp_cfg, monkeypatch):
    """Field config shape: chat_template is the literal "auto" string — it
    must resolve to the model's native template at init, not stay 'auto'
    (which would silently fall back to ChatML and skip gpt-oss framing)."""
    s = L.Server({"id": "t-auto", "type": "llama", "model": "gpt-oss-20b",
                  "chat_template": "auto",
                  "url": "http://127.0.0.1:1/t-auto/completion"}, tmp_cfg)
    cap = _fake_post(monkeypatch, {"content": "ok", "tokens_predicted": 1})
    s.request("Hi.")
    assert cap["json"]["prompt"].endswith("<|start|>assistant")


# --------------------------------------------------------------------------- #
# streaming path

class ReasoningStream:
    """SSE stream whose tokens carry the analysis channel then the final
    answer — exactly what a reasoning_format=none llama.cpp box delivers."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        for tok in self.tokens:
            yield f'data: {json.dumps({"content": tok, "stop": False})}\n'.encode()
        yield b'data: {"content": "", "stop": true, "tokens_predicted": 5}\n'

    def close(self):
        self.closed = True


def test_stream_returns_clean_answer_but_streams_thinking(tmp_cfg, monkeypatch):
    s, _ = _gptoss(tmp_cfg)
    seen = []
    stream = ReasoningStream([
        "<|channel|>analysis<|message|>thinking", " about it", MARKER, "final ", "answer",
    ])
    captured = {}

    def fake_post_stream(self, target, headers, payload):
        captured["json"] = payload
        return stream
    monkeypatch.setattr(L.Server, "_post_stream", fake_post_stream)

    out = s.request_stream("Do the thing.", on_token=lambda t, n: seen.append(t))
    # returned content: final channel only
    assert out == "final answer"
    # live tokens still include the thinking (visible reasoning in the GUI)
    assert "".join(seen).startswith("<|channel|>analysis")
    # and the outgoing prompt was templated
    assert captured["json"]["prompt"].endswith("<|start|>assistant")


def test_stream_no_marker_untouched(tmp_cfg, monkeypatch):
    s, _ = _other(tmp_cfg)
    stream = ReasoningStream(["hel", "lo"])
    monkeypatch.setattr(L.Server, "_post_stream",
                        lambda self, target, headers, payload: stream)
    out = s.request_stream("Say hello.")
    assert out == "hello"


def test_stream_qwen_think_events_return_clean_code(tmp_cfg, monkeypatch):
    """Qwen3 streaming: reasoning tokens then a closing tag, then the code.
    The engine's streaming path (request_stream) must return ONLY the code,
    not the <think> block — the regression that made the live view/logs
    disagree (model log shows generation, KAISEN gets empty/thinking)."""
    s, _ = _other(tmp_cfg, model="Qwen3.8-27B")
    stream = ReasoningStream([
        "<think>", " considering fast doubling ", "</think>",
        "\n```rust\nfn main(){}\n```",
    ])
    monkeypatch.setattr(L.Server, "_post_stream",
                        lambda self, target, headers, payload: stream)
    out = s.request_stream("Write a Rust program.")
    assert "<think>" not in out and "</think>" not in out
    assert "fn main(){}" in out


def test_model_check_reports_streaming_path(tmp_cfg, monkeypatch):
    """" MODELCHECK must exercise request_stream (what a generation uses),
    not just request — it catches 'endpoint answers but delivers no stream'."""
    s, _ = _other(tmp_cfg)
    stream = ReasoningStream(["ok"])
    monkeypatch.setattr(L.Server, "_post_stream",
                        lambda self, target, headers, payload: stream)
    res = s.model_check(max_tokens=8)
    assert res.get("streaming_path") is True
    assert res.get("ok") is True
    assert res.get("empty") is False
    assert "ok" in res.get("reply", "")


def test_concurrency_capped_at_detected_slots(tmp_cfg):
    """Single-slot server configured with max_concurrent>1: concurrency is
    capped at the real slot count so generations stop queuing invisibly
    behind one slot (the 'chat looks dead during a long queue' field bug)."""
    s, _ = _gptoss(tmp_cfg)
    s.max_concurrent = 8                     # config over-subscribes
    s._detected_slots = 1                    # server has ONE slot
    assert s._capacity == 1
    assert s.acquire() is True
    assert s.acquire() is False              # second slot is a lie
    # capacity is a min, never below 1, never above configured
    s._detected_slots = None
    assert s._capacity == 8
    s._detected_slots = 3
    assert s._capacity == 3


def test_slots_snapshot_learns_capacity(tmp_cfg, monkeypatch):
    s, _ = _gptoss(tmp_cfg)
    calls = {}

    def fake_get(url, headers=None, timeout=5.0, **kw):
        calls["url"] = url
        return FakeSlots([{"is_processing": True, "n_prompt_tokens_processed": 100,
                           "n_tokens_predicted": 20}])
    monkeypatch.setattr(L.requests, "get", fake_get)
    working, total = s._slots_snapshot()
    assert working is True and total == 120
    assert s._detected_slots == 1


class FakeSlots:
    def __init__(self, slots):
        self._slots = slots
        self.status_code = 200

    def json(self):
        return self._slots
