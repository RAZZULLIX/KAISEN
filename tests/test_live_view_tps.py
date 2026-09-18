"""The live stream view and the pill's tps — the two dashboard surfaces that
made a working pool look dead:

  - the live modal read ONLY the selected engine, so with another project
    streaming it showed an empty box (now: pool-wide, like the pill);
  - tps was `tokens / max(1.0, elapsed)`, which ramps up from ~1 and is
    simply wrong for a short generation (40 tokens in 0.5 s read as 40 tok/s
    instead of 80) — now a windowed rate that reports 0.0 until it is real;
  - a single stream that died mid-response marked the endpoint OFFLINE and
    left it out of routing until the 30 s reprobe cycle, during which every
    pipeline waited (empty live view, 0 tps) — now it takes two consecutive
    stream failures, and a stalled pool re-probes on demand.
"""
import time

import pytest

from kaisen.llm import Server, ModelOrchestrator
from kaisen.engine import ProjectEngine, Session


def _server(orch, sid="one", **over):
    spec = {"id": sid, "type": "llama",
            "url": f"http://127.0.0.1:1/{sid}", "tier": "small",
            "priority": 1, "max_concurrent": 1, "enabled": True}
    spec.update(over)
    return Server(spec, orch.cfg)


# ── tps: windowed, no ramp ────────────────────────────────────────────

def _session(orch=None):
    from kaisen.engine import Session
    return Session(0, "code", 1, "prompt")


def test_tps_measures_the_recent_window_not_the_whole_generation(monkeypatch):
    """A generation that STREAMS 40 tokens in 0.5 s is 80 tok/s — the old
    wall-clock average reported 40 (the 1 s floor), and ramped 1→6→13→20 on
    the way to a steady rate."""
    sess = _session()
    t = [1000.0]
    monkeypatch.setattr("kaisen.engine.time.time", lambda: t[0])

    for i in range(40):                    # 40 tokens, one per 12.5 ms
        t[0] += 0.0125
        sess.push("tok ")
    rate = sess.tps
    assert 60.0 <= rate <= 100.0, f"expected ~80 tok/s, got {rate}"


def test_tps_is_zero_until_it_has_a_real_window(monkeypatch):
    """No invented number: before a 0.2 s / 2-sample window the rate is 0.0,
    so the pill cannot show a made-up speed (it used to read 1.0, 6.0, ...)."""
    sess = _session()
    t = [500.0]
    monkeypatch.setattr("kaisen.engine.time.time", lambda: t[0])
    sess.push("a")
    assert sess.tps == 0.0
    t[0] += 0.05
    sess.push("b")
    assert sess.tps == 0.0                 # window still too short
    t[0] += 0.3
    sess.push("c")
    assert sess.tps > 0.0                  # real rate from here on


def test_tps_reflects_a_speed_change(monkeypatch):
    """The window tracks what is happening NOW: after a stall, the rate must
    drop instead of hiding behind the generation's average."""
    sess = _session()
    t = [0.0]
    monkeypatch.setattr("kaisen.engine.time.time", lambda: t[0])
    for _ in range(30):                    # fast: ~100 tok/s
        t[0] += 0.01
        sess.push("x")
    fast = sess.tps
    for _ in range(5):                     # then a slow patch: ~10 tok/s
        t[0] += 0.1
        sess.push("x")
    assert sess.tps < fast / 2, f"rate did not fall ({fast} -> {sess.tps})"


# ── stream failures: one abort is not a dead endpoint ─────────────────

def test_a_single_aborted_stream_does_not_exile_the_endpoint(tmp_cfg):
    """Stop/Pause aborts a stream mid-response; that used to mark the box
    offline (out of routing until the next 30 s probe cycle)."""
    orch = ModelOrchestrator(tmp_cfg)
    s = _server(orch)
    assert s.note_stream_failure() is False       # first: tolerated
    assert s.online is not False
    assert s.note_stream_failure() is True        # repeated: give up on it
    s.mark_online(False)
    assert s.online is False
    s.mark_online(True)                           # a working call clears it
    assert s.online is True
    assert s.note_stream_failure() is False


def test_pool_probes_on_demand_when_nothing_is_usable(tmp_cfg, monkeypatch):
    """A stalled pool must not sit out the periodic cycle: when every
    endpoint is offline, picking kicks an immediate probe."""
    orch = ModelOrchestrator(tmp_cfg)
    s = _server(orch)
    s.mark_online(False)                          # the only endpoint is down
    orch._servers = {"one": s}
    orch._active_ids = ["one"]
    orch._rebuild_layout()
    kicks = []
    monkeypatch.setattr(orch, "_kick_reprobe", lambda sids: kicks.append(sorted(sids)))
    assert orch._pick_server("tiny", pipeline_key="proj|0") is None
    assert kicks and kicks[0] == ["one"], kicks


# --------------------------------------------------------------------------- #
# thinking is a token too: the gray prefix
# --------------------------------------------------------------------------- #

def test_inline_thinking_is_marked_as_the_gray_prefix():
    """llama.cpp with reasoning_format=none (the default, and what the local
    Ternary-Bonsai boxes report) delivers the plan IN `content` and marks it
    only at its end — so the close marker sets the split retroactively, and
    the answer follows it."""
    s = Session(0, "code", 1, "prove it")
    plan = "<think>\nWe need to prove as many as possible."
    s.push(plan)                       # plan first, no marker yet
    s.push("</think>\n\ntheorem foo : True := by trivial")
    snap = s.snapshot()
    # the gray run ends AFTER the close marker: the marker is part of the plan
    # and must never show up in the answer
    assert snap["reasoning_len"] == len(plan) + len("</think>")
    assert snap["text"].startswith(plan)
    assert snap["text"][snap["reasoning_len"]:].lstrip().startswith("theorem")


def test_separated_reasoning_channel_is_the_gray_prefix():
    """When the server DOES separate the channel, every reasoning delta is
    thinking and the answer starts at the first content delta."""
    s = Session(1, "code", 2, "prove it")
    s.push("plan A", 1, True)
    s.push(" plan B", 1, True)
    s.push("theorem bar : True := by trivial")
    snap = s.snapshot()
    assert snap["reasoning_len"] == len("plan A plan B")
    assert snap["text"][snap["reasoning_len"]:].startswith("theorem")


def test_a_plain_answer_has_no_gray_prefix():
    s = Session(2, "code", 3, "prove it")
    s.push("theorem baz : True := by trivial")
    assert s.snapshot()["reasoning_len"] == 0
