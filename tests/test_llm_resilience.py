"""LLM-layer resilience: first-token policy (NO limit by default; opt-in
hard cap), between-tokens nodata, error-kind classification, shared health
across orchestrators, background re-probe of offline servers, and
config-PUT deep merge.

All timing is compressed so the suite stays fast; the logic under test is
the DECISION path, not real prefill durations.
"""
import threading
import time
import uuid

import pytest
import requests

from kaisen import llm as L
from kaisen.config import FrameworkConfig

# Fixture lives in the server-API test module (shared live-server harness).
from tests.test_server_api import api  # noqa: F401
def _srv(tmp_cfg, sid=None):
    sid = sid or f"t-{uuid.uuid4().hex[:8]}"
    return L.Server({"id": sid, "type": "llama",
                     "url": f"http://127.0.0.1:1/{sid}/completion"}, tmp_cfg), sid


class SlowStream:
    """Fake streaming response: silent for `token_delay` seconds, then SSE
    lines; optionally raises mid-stream after N lines (server crash)."""

    def __init__(self, token_delay, tokens, fail_after=None, token_interval=0.0):
        self.token_delay = token_delay
        self.tokens = tokens
        self.fail_after = fail_after
        self.token_interval = token_interval
        self.stop_evt = threading.Event()
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        t0 = time.time()
        while time.time() - t0 < self.token_delay:
            if self.stop_evt.is_set():
                return
            time.sleep(0.01)
        for i, tok in enumerate(self.tokens):
            if self.fail_after is not None and i >= self.fail_after:
                raise requests.exceptions.ConnectionError("simulated mid-stream drop")
            if self.token_interval:
                time.sleep(self.token_interval)
            yield f'data: {{"content": "{tok}", "stop": false}}\n'.encode()
        n = len(self.tokens)
        yield f'data: {{"content": "", "stop": true, "tokens_predicted": {n}}}\n'.encode()

    def close(self):
        self.closed = True
        self.stop_evt.set()


# --------------------------------------------------------------------------- #
# first-token policy
# --------------------------------------------------------------------------- #

def test_first_byte_deadline_none_by_default(tmp_cfg):
    """Default (first_token_timeout=0): NO limit before the first token,
    whatever the prompt size or measured prefill speed — a slow box must
    not lose generations."""
    s, _ = _srv(tmp_cfg)
    assert s._first_byte_deadline("x" * 1600) is None
    s._prefill_tps = 10.0
    assert s._first_byte_deadline("x" * 160000) is None


def test_first_byte_deadline_flat_when_capped(tmp_cfg):
    """Opt-in cap: an explicit first_token_timeout IS the deadline — flat,
    predictable, independent of prompt size or learned speed."""
    s, _ = _srv(tmp_cfg)
    s.first_token_timeout = 250.0
    assert s._first_byte_deadline("x" * 64) == 250.0
    assert s._first_byte_deadline("x" * 160000) == 250.0


# --------------------------------------------------------------------------- #
# silence policy in _consume_stream
# --------------------------------------------------------------------------- #

def test_long_prefill_survives_by_default(tmp_cfg):
    """The field bug: slow prefills outlived every client-side deadline guess
    and the generation was DISCARDED while the server was still working.
    Default policy: no first-token limit — accept the output whenever it
    arrives, even when /slots telemetry is unhelpful."""
    s, _ = _srv(tmp_cfg)
    s.nodata_timeout = 0.5                     # strict between-tokens policy
    s._slots_snapshot = lambda: (None, None)   # must not matter
    stream = SlowStream(token_delay=3.0, tokens=["he", "llo"])
    t0 = time.time()
    content, tokens, ttft = s._consume_stream(stream, None, None, llama=True, prompt="x" * 1600)
    elapsed = time.time() - t0
    assert content == "hello" and tokens == 2
    assert ttft is not None and ttft >= 2.5     # really waited through the silence
    assert elapsed < 10


def test_first_token_cap_is_hard_when_set(tmp_cfg):
    """Opt-in protection: with first_token_timeout > 0, silence past the cap
    fails fast with kind 'timeout' — even when /slots shows visible work."""
    s, _ = _srv(tmp_cfg)
    s.first_token_timeout = 1.0
    s.nodata_timeout = 60.0
    s._slots_snapshot = lambda: (True, 999)    # visible work must NOT extend the cap
    stream = SlowStream(token_delay=10.0, tokens=["never"])
    t0 = time.time()
    with pytest.raises(L.ServerError) as ei:
        s._consume_stream(stream, None, None, llama=True, prompt="x" * 1600)
    assert ei.value.kind == "timeout"
    assert time.time() - t0 < 5.0              # failed at the cap, not at 10 s
    stream.stop_evt.set()


def test_streaming_generation_completes_while_tokens_flow(tmp_cfg):
    """A generation that keeps producing tokens is never cut off by a total
    time budget — only silence BETWEEN tokens (nodata) can fail it.  The old
    code killed in-flight streams past an estimated total budget, discarding
    generations that were still working."""
    s, _ = _srv(tmp_cfg)
    s.nodata_timeout = 60.0
    stream = SlowStream(token_delay=0.0, tokens=[f"t{i}" for i in range(40)],
                        token_interval=0.05)   # ~2 s of honest streaming
    t0 = time.time()
    content, tokens, ttft = s._consume_stream(stream, None, None, llama=True, prompt="x" * 64)
    assert tokens == 40 and len(content) > 0 and ttft is not None
    assert time.time() - t0 >= 1.5




def test_midstream_drop_is_stream_kind(tmp_cfg):
    """Server process dies mid-response (the 8503/8504 crash signature):
    classified 'stream' so callers mark it offline — and re-probe later."""
    s, _ = _srv(tmp_cfg)
    s.nodata_timeout = 5.0
    stream = SlowStream(token_delay=0.0, tokens=["a", "b", "c"], fail_after=2)
    with pytest.raises(L.ServerError) as ei:
        s._consume_stream(stream, None, None, llama=True, prompt="x" * 1600)
    assert ei.value.kind == "stream"


def test_cancel_still_wins_during_silence(tmp_cfg):
    s, _ = _srv(tmp_cfg)
    s.nodata_timeout = 5.0
    s._first_byte_deadline = lambda p: 60.0
    cancel = threading.Event()
    stream = SlowStream(token_delay=30.0, tokens=["x"])

    def set_later():
        time.sleep(0.4)
        cancel.set()
    threading.Thread(target=set_later, daemon=True).start()
    t0 = time.time()
    with pytest.raises(L.GenerationCancelled):
        s._consume_stream(stream, None, cancel, llama=True, prompt="x" * 1600)
    assert time.time() - t0 < 5.0
    stream.stop_evt.set()


# --------------------------------------------------------------------------- #
# error classification
# --------------------------------------------------------------------------- #

def _http_error(code):
    r = requests.models.Response()
    r.status_code = code
    return requests.exceptions.HTTPError(f"{code} Client Error", response=r)


def test_error_kinds():
    assert L._wrap_error(_http_error(401), "x").kind == "auth"
    assert L._wrap_error(_http_error(500), "x").kind == "http"
    assert L._wrap_error(requests.exceptions.ConnectionError("refused"), "x").kind == "connection"
    assert L._wrap_error(requests.exceptions.ReadTimeout(), "x").kind == "timeout"
    # mid-stream connection break = the process died, not a plain refusal
    assert L._wrap_error(requests.exceptions.ConnectionError("broken"), "x",
                         mid_stream=True).kind == "stream"


def test_auth_failure_does_not_mark_offline(tmp_cfg):
    """A 401 means the server ANSWERED — it is online; only the key is wrong.
    The pool must keep it routable (banned briefly) instead of exiling it."""
    s, sid = _srv(tmp_cfg)
    h = L.health_for(sid)
    h.set_online(True)
    err = L._wrap_error(_http_error(401), sid)
    # mirror the orchestrator's reaction:
    if err.kind in ("connection", "stream"):
        s.mark_online(False)
    elif err.kind == "auth":
        s.ban(seconds=300, reason=str(err))
    else:
        s.ban(seconds=30, reason=str(err))
    assert h.online is True                          # NOT marked offline
    assert h.banned                                   # ...but not hammered


# --------------------------------------------------------------------------- #
# shared health + re-probe
# --------------------------------------------------------------------------- #

def test_health_is_shared_across_orchestrators(tmp_cfg):
    """Engines/suggest/swarm each build their own orchestrator; one discovery
    must propagate everywhere (the old per-instance state diverged)."""
    sid = f"shared-{uuid.uuid4().hex[:8]}"
    tmp_cfg.llm["servers"] = [{"id": sid, "type": "llama",
                               "url": f"http://127.0.0.1:1/{sid}/completion"}]
    tmp_cfg.llm["active_ids"] = [sid]
    o1 = L.ModelOrchestrator(tmp_cfg)
    o2 = L.ModelOrchestrator(tmp_cfg)
    o1._servers[sid].mark_online(False)
    assert o2._servers[sid].online is False          # shared record
    o2._servers[sid].mark_online(True)
    assert o1._servers[sid].online is True


def test_reprobe_recovers_offline_server(tmp_cfg):
    sid = f"revive-{uuid.uuid4().hex[:8]}"
    h = L.health_for(sid)
    alive = {"v": False}

    def probe():
        return alive["v"]
    L.register_probe(sid, probe)
    h.set_online(False)
    L._reprobe_cycle()
    assert h.online is False                         # still down
    alive["v"] = True                                # the instance came back
    L._reprobe_cycle()
    assert h.online is True                          # rejoins automatically




# --------------------------------------------------------------------------- #
# GUI config save must not wipe fields the GUI doesn't show
# --------------------------------------------------------------------------- #

def test_config_put_deep_merges_llm_section(api):
    """The GUI sends llm.{read_timeout,nodata_timeout,connect_timeout,
    max_retries} only.  A shallow update wiped nodata_timeout's siblings
    (retry_backoff, routing, allowlists) and server.api_key on every save."""
    srv, base = api
    cfg0 = srv.cfg
    cfg0.llm["retry_backoff"] = 2.0
    cfg0.llm["routing"] = "adaptive"
    cfg0.data["server"]["api_key"] = "sekrit"
    r = requests.put(base + "/api/config", json={
        "llm": {"read_timeout": 999},
        "server": {"host": "127.0.0.1"},
    })
    assert r.status_code == 200
    cfg = requests.get(base + "/api/config").json()
    assert cfg["llm"]["read_timeout"] == 999          # new value applied
    assert "retry_backoff" in cfg["llm"]              # sibling SURVIVED
    assert cfg["llm"]["routing"] == "adaptive"        # and this one
    assert cfg0.data["server"]["api_key"] == "sekrit"  # secret not wiped
def test_reprobe_loop_is_idempotent():
    L.start_reprobe_loop()
    t1 = L._REPROBE_THREAD
    L.start_reprobe_loop()
    assert L._REPROBE_THREAD is t1                   # one thread per process
