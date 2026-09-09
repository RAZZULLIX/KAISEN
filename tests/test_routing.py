"""Tier routing: cost-first server selection, estimates, capacity math."""
import threading
import time

import pytest

from kaisen.llm import ModelOrchestrator, Server, TIER_RANK, _cap_predict


def _server(orch, sid, tier="small", priority=1, max_concurrent=1,
            smartness=None, cost_in=0.0, cost_out=0.0, online=None,
            enabled=True, context_window=0):
    s = Server({
        "id": sid, "type": "llama", "url": f"http://127.0.0.1:1/{sid}",
        "tier": tier, "priority": priority, "max_concurrent": max_concurrent,
        "smartness": smartness, "cost_in": cost_in, "cost_out": cost_out,
        "enabled": enabled, "context_window": context_window,
    }, orch.cfg)
    s.mark_online(online)
    return s


@pytest.fixture
def orch(tmp_cfg):
    return ModelOrchestrator(tmp_cfg)


def _register(orch, *servers):
    orch._servers = {s.id: s for s in servers}
    orch._active_ids = [s.id for s in servers]
    orch._rr = 0


def test_tier_rank_ordering():
    assert TIER_RANK == {"tiny": 0, "small": 1, "large": 2}


def test_pick_lowest_tier_satisfying_min_tier(orch):
    tiny = _server(orch, "t", tier="tiny", priority=9)
    small = _server(orch, "s", tier="small", priority=9)
    large = _server(orch, "l", tier="large", priority=9)
    _register(orch, tiny, small, large)

    assert orch._pick_server("tiny") == "t"     # cheapest wins
    orch.release("t")
    assert orch._pick_server("small") == "s"    # tiny cannot satisfy
    orch.release("s")
    assert orch._pick_server("large") == "l"
    orch.release("l")


def test_pick_prefers_priority_within_tier(orch):
    lo = _server(orch, "lo", tier="small", priority=1)
    hi = _server(orch, "hi", tier="small", priority=10)
    _register(orch, lo, hi)
    assert orch._pick_server("tiny") == "hi"
    orch.release("hi")

def test_pick_rotates_among_equal_servers(orch):
    """Three identical servers (same tier/priority/cost/load): sequential
    picks must cycle through the WHOLE pool, not hammer the first one —
    the field bug where a 3-box setup only ever called box #1."""
    a = _server(orch, "a", tier="large")
    b = _server(orch, "b", tier="large")
    c = _server(orch, "c", tier="large")
    _register(orch, a, b, c)
    picks = []
    for _ in range(6):
        sid = orch._pick_server("tiny")
        assert sid is not None
        picks.append(sid)
        orch.release(sid)
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_pick_rotation_never_outranks_tier_or_priority(orch):
    """Round-robin is a tiebreak ONLY: a cheaper tier or a higher priority
    wins every single pick, even when it was the one picked most recently."""
    cheap = _server(orch, "cheap", tier="tiny")
    exp1 = _server(orch, "exp1", tier="large")
    exp2 = _server(orch, "exp2", tier="large")
    _register(orch, cheap, exp1, exp2)
    for _ in range(4):
        assert orch._pick_server("tiny") == "cheap"   # tiny always beats large
        orch.release("cheap")
    lo = _server(orch, "lo", tier="large", priority=1)
    hi = _server(orch, "hi", tier="large", priority=9)
    _register(orch, lo, hi)                           # resets the cursor
    for _ in range(3):
        assert orch._pick_server("tiny") == "hi"      # priority always beats rotation
        orch.release("hi")

def test_pick_busy_falls_through_to_next(orch):
    busy = _server(orch, "busy", tier="tiny", max_concurrent=1)
    free = _server(orch, "free", tier="small", max_concurrent=1)
    _register(orch, busy, free)
    assert busy.acquire()  # saturate the tiny one
    assert orch._pick_server("tiny") == "free"
    orch.release("busy")
    orch.release("free")


def test_pick_skips_disabled_and_banned(orch):
    off = _server(orch, "off", tier="tiny", enabled=False)
    banned = _server(orch, "ban", tier="tiny")
    banned.ban(seconds=60)
    ok_srv = _server(orch, "ok", tier="small")
    _register(orch, off, banned, ok_srv)
    assert orch._pick_server("tiny") == "ok"
    orch.release("ok")


def test_pick_excludes_known_offline(orch):
    dead = _server(orch, "dead", tier="tiny", online=False)
    live = _server(orch, "live", tier="tiny", online=True)
    _register(orch, dead, live)
    assert orch._pick_server("tiny") == "live"
    orch.release("live")


def test_pick_none_when_nothing_available(orch):
    dead = _server(orch, "dead", tier="tiny", online=False)
    _register(orch, dead)
    assert orch._pick_server("tiny") is None


def test_pick_fallback_when_all_qualifying_busy(orch):
    # Two tiny servers both busy; a large one free. The requirement
    # (tiny) is unsatisfiable -> fall back to ANY usable server instead
    # of stalling the pipeline.
    t1 = _server(orch, "t1", tier="tiny")
    t2 = _server(orch, "t2", tier="tiny")
    big = _server(orch, "big", tier="large")
    _register(orch, t1, t2, big)
    assert t1.acquire() and t2.acquire()
    got = orch._pick_server("tiny")
    assert got == "big"
    orch.release(got)
    orch.release("t1")
    orch.release("t2")


def test_server_smartness_tier_defaults(tmp_cfg):
    s = Server({"id": "x", "tier": "tiny"}, tmp_cfg)
    assert s.smartness == 2.0
    s2 = Server({"id": "y", "tier": "small"}, tmp_cfg)
    assert s2.smartness == 5.0
    s3 = Server({"id": "z", "tier": "large"}, tmp_cfg)
    assert s3.smartness == 8.0
    s4 = Server({"id": "w", "tier": "small", "smartness": 7.5}, tmp_cfg)
    assert s4.smartness == 7.5


def test_server_cost_from_cost_dict_and_flat_keys(tmp_cfg):
    s = Server({"id": "a", "cost": {"in": 1.5, "out": 60.0}}, tmp_cfg)
    assert s.cost_in == 1.5 and s.cost_out == 60.0
    s2 = Server({"id": "b", "cost_in": 0.5, "cost_out": 15.0}, tmp_cfg)
    assert s2.cost_in == 0.5 and s2.cost_out == 15.0
    s3 = Server({"id": "c"}, tmp_cfg)
    assert s3.cost_in == 0.0 and s3.cost_out == 0.0


def test_estimate_math(tmp_cfg):
    s = Server({"id": "a", "cost_in": 2.0, "cost_out": 10.0}, tmp_cfg)
    s.record(ok=True, seconds=10.0, tokens=200)  # tps = 20
    est = s.estimate(tokens_in=1000, tokens_out=500)
    assert est["tps"] == 20.0
    # 1000 in + 500 out = 1500 tokens at 20 tps = 75 s
    assert est["seconds"] == 75.0
    # (1000/1e6)*2 + (500/1e6)*10 = 0.002 + 0.005 = 0.007
    assert est["cost_usd"] == pytest.approx(0.007)
    # tokens_out defaults to half the input when omitted
    est2 = s.estimate(tokens_in=1000)
    assert est2["tokens_out"] == 500


def test_estimate_free_server(tmp_cfg):
    s = Server({"id": "local"}, tmp_cfg)
    est = s.estimate(tokens_in=10_000, tokens_out=2_000)
    assert est["cost_usd"] == 0.0
    assert est["tps"] == 10.0  # fallback when unmeasured


def test_acquire_release_capacity(tmp_cfg):
    s = Server({"id": "a", "max_concurrent": 2}, tmp_cfg)
    assert s.acquire() and s.acquire()
    assert not s.acquire()  # saturated
    assert s.busy
    s.release()
    assert not s.busy
    assert s.acquire()
    s.release()
    s.release()
    assert s._inflight == 0


def test_ban_blocks_acquire(tmp_cfg):
    s = Server({"id": "a"}, tmp_cfg)
    s.ban(seconds=60, reason="boom")
    assert s.banned
    assert not s.acquire()


def test_cap_predict_no_cap_by_default(orch):
    """KAISEN must NOT clamp generation output by default — a thinking model
    emits thousands of reasoning tokens before the answer; an 8k default
    truncates the thinking block (reply never reaches </think>). Call the
    model AS-IS: unlimited stays unlimited unless max_tokens is set."""
    out = _cap_predict({"n_predict": -1}, orch.cfg)
    assert out["n_predict"] == -1
    out2 = _cap_predict({"n_predict": None}, orch.cfg)
    assert out2["n_predict"] is None
    # explicit opt-in clamps
    orch.cfg.data["llm"]["max_tokens"] = 4096
    out3 = _cap_predict({"n_predict": -1}, orch.cfg)
    assert out3["n_predict"] == 4096


def test_cap_predict_explicit_value_untouched(orch):
    # Explicit finite n_predict is the caller's contract — only the
    # UNLIMITED form (None/-1) is capped.
    out = _cap_predict({"n_predict": 256}, orch.cfg)
    assert out["n_predict"] == 256


def test_routing_prefers_fast_measured_server(orch, tmp_cfg):
    """Among same-priority servers, a box with a FAST measured average
    seconds must be preferred over a pathologically slow one — otherwise a
    slow llama.cpp wedge (a call taking minutes while peers finish in 0.2s)
    would be the round-robin victim and stall generations."""
    from kaisen.llm import Server
    fast = Server({"id": "fast", "type": "llama", "url": "http://x/fast"}, tmp_cfg)
    slow = Server({"id": "slow", "type": "llama", "url": "http://x/slow"}, tmp_cfg)
    # fast: 10 requests in 2s (0.2s avg); slow: 5 requests in 1000s (200s avg)
    fast._stats.update(requests=10, total_seconds=2.0)
    slow._stats.update(requests=5, total_seconds=1000.0)
    for s in (fast, slow):
        s.enabled = True
        s._health.banned_until = 0.0     # not banned
    orch._servers = {"fast": fast, "slow": slow}
    orch._active_ids = ["fast", "slow"]
    orch._rr = 0
    # pick thrice: the FAST box must be chosen every time (never round-robin
    # onto the slow one while fast is free).
    for _ in range(3):
        orch._servers["fast"]._inflight = 0   # keep fast free
        orch._servers["slow"]._inflight = 0
        assert orch._pick_server(min_tier="tiny") == "fast"
        # release the picked server's inflight (acquire happened in pick)
        orch._servers["fast"].release()
    # now mark fast busy -> slow is the usable fallback
    orch._servers["fast"]._inflight = orch._servers["fast"]._capacity
    assert orch._pick_server(min_tier="tiny") == "slow"


def test_status_aggregates_all_servers(orch):
    a = _server(orch, "a", tier="tiny")
    b = _server(orch, "b", tier="large")
    _register(orch, a, b)
    st = orch.status()
    assert sorted(s["id"] for s in st["servers"]) == ["a", "b"]
    assert st["active_ids"] == ["a", "b"]


# ── global cap-fill allocator (multi pipelines spread across endpoints) ──

def test_cap_fill_assigns_pipelines_priority_first(orch):
    """The user's spec: `multi=N` pipelines fill the highest-priority
    endpoint up to its max_concurrent, then the next priority.  port1
    (prio 2, cap 3), port2 (prio 2, cap 1), port3 (prio 1, cap 1):
    multi=5 -> 3 on port1, 1 on port2, 1 on port3."""
    p1 = _server(orch, "p1", tier="large", priority=2, max_concurrent=3)
    p2 = _server(orch, "p2", tier="large", priority=2, max_concurrent=1)
    p3 = _server(orch, "p3", tier="large", priority=1, max_concurrent=1)
    _register(orch, p1, p2, p3)
    assigned = {}
    for i in range(5):
        sid = orch._pick_server("tiny", pipeline_key=f"eng|{i}")
        assert sid is not None
        assigned[i] = sid
    from collections import Counter
    c = Counter(assigned.values())
    assert c["p1"] == 3, c   # cap 3 filled first (highest priority)
    assert c["p2"] == 1, c   # then the other prio-2 endpoint
    assert c["p3"] == 1, c   # then the lower-priority endpoint


def test_cap_fill_lower_priority_untouched_while_high_has_room(orch):
    """multi lower than the high tier's capacity never spills down: the
    lower-priority endpoint stays unused until the higher tier is FULL."""
    hi = _server(orch, "hi", tier="large", priority=2, max_concurrent=6)
    lo = _server(orch, "lo", tier="large", priority=1, max_concurrent=2)
    _register(orch, hi, lo)
    for i in range(4):
        sid = orch._pick_server("tiny", pipeline_key=f"eng|{i}")
        assert sid == "hi"      # 4 < hi's cap 6 -> never spills to lo
        orch.release(sid)


def test_cap_fill_reservation_is_sticky_across_generations(orch):
    """A pipeline keeps its endpoint slot across successive requests
    (sticky reservation) — the same (engine, pipeline) key returns the
    same server while it stays healthy."""
    a = _server(orch, "stick-a", tier="large", priority=2, max_concurrent=2)
    b = _server(orch, "stick-b", tier="large", priority=2, max_concurrent=2)
    _register(orch, a, b)
    first = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(first)
    second = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(second)
    assert first == second        # reservation survived the release


def test_cap_fill_stale_reservation_reassigned_when_offline(orch):
    """When a pipeline's held endpoint goes offline, the stale reservation
    is dropped and the pipeline is reassigned elsewhere."""
    a = _server(orch, "reassign-a", tier="large", priority=2, max_concurrent=2)
    b = _server(orch, "reassign-b", tier="large", priority=2, max_concurrent=2)
    _register(orch, a, b)
    first = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(first)
    # the held endpoint dies
    orch._servers[first].mark_online(False)
    second = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(second)
    assert second != first
    assert second in ("reassign-a", "reassign-b")


def test_cap_fill_waits_on_transient_saturation(orch):
    """A healthy-but-busy held endpoint makes the pipeline WAIT (None) with
    the reservation kept — not churn to another endpoint."""
    a = _server(orch, "wait-a", tier="large", priority=2, max_concurrent=2)
    b = _server(orch, "wait-b", tier="large", priority=2, max_concurrent=2)
    _register(orch, a, b)
    held = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(held)
    # saturate the held endpoint's capacity with OTHER pipelines' requests
    hs = orch._servers[held]
    hs.acquire()
    hs.acquire()
    assert hs._inflight == hs._capacity
    got = orch._pick_server("tiny", pipeline_key="eng|0")
    assert got is None                     # waits, does not reassign
    assert orch._pipeline_slot.get("eng|0") == held   # reservation kept


def test_cap_fill_release_slots_on_engine_stop(orch):
    """release_pipeline_slots returns a stopped engine's reservations to
    the pool so later engines can use the endpoints."""
    a = _server(orch, "rel-a", tier="large", priority=2, max_concurrent=1)
    b = _server(orch, "rel-b", tier="large", priority=2, max_concurrent=1)
    _register(orch, a, b)
    sid = orch._pick_server("tiny", pipeline_key="engX|0")
    orch.release(sid)
    assert orch._pipeline_slot.get("engX|0") == sid
    orch.release_pipeline_slots("engX")
    assert "engX|0" not in orch._pipeline_slot
    # slot free again: another engine can be assigned it
    sid2 = orch._pick_server("tiny", pipeline_key="engY|0")
    assert sid2 is not None


def test_cap_fill_plain_callers_keep_per_request_pick(orch):
    """No pipeline_key (deepwork/suggest/repair) -> unchanged per-request
    selection, no reservation recorded."""
    a = _server(orch, "plain-a", tier="large")
    b = _server(orch, "plain-b", tier="large")
    _register(orch, a, b)
    assert orch._pick_server("tiny") is not None
    assert orch._pipeline_slot == {}


def test_cap_fill_rebalance_when_real_slots_shrink(orch):
    """The user's reality check: config max_concurrent=6 reserved 6
    pipelines, then llama.cpp /slots learns the box really has 2.  The
    excess 4 reservations MUST be evicted (newest first) and reassigned
    down the priority bracket — never queue invisibly behind 2 real
    slots."""
    hi = _server(orch, "shrink-hi", tier="large", priority=2,
                 max_concurrent=6)
    lo = _server(orch, "shrink-lo", tier="large", priority=1,
                 max_concurrent=2)
    _register(orch, hi, lo)
    # 6 pipelines reserve hi (config claims 6 slots)
    for i in range(6):
        sid = orch._pick_server("tiny", pipeline_key=f"e|{i}")
        assert sid == "shrink-hi"
        orch.release(sid)
    assert sum(1 for v in orch._pipeline_slot.values()
               if v == "shrink-hi") == 6
    # /slots learns hi really has 2 -> next pick rebalances
    orch._servers["shrink-hi"]._detected_slots = 2
    got = orch._pick_server("tiny", pipeline_key="e|6")
    if got:
        orch.release(got)
    # hi keeps 2 reservations (the cap); the 4 excess were evicted; the
    # new pipeline e|6 was assigned DOWN the priority bracket to lo.
    hi_kept = sum(1 for v in orch._pipeline_slot.values()
                  if v == "shrink-hi")
    assert hi_kept == 2, dict(orch._pipeline_slot)
    assert got == "shrink-lo"
    assert sum(1 for v in orch._pipeline_slot.values()
               if v == "shrink-lo") == 1
    # the 4 evicted pipelines re-request later: lo's quota (2) takes e|2
    # (e|6 already holds one); the rest WAIT, not over-queue.
    sid = orch._pick_server("tiny", pipeline_key="e|2")
    assert sid == "shrink-lo"
    orch.release(sid)
    # lo full (cap 2): the remaining evicted pipelines must WAIT
    assert orch._pick_server("tiny", pipeline_key="e|3") is None
    assert orch._pick_server("tiny", pipeline_key="e|4") is None
    assert orch._pick_server("tiny", pipeline_key="e|5") is None


def test_cap_fill_all_quotas_full_waits_not_oversubscribes(orch):
    """When every endpoint's real quota is full, a new pipeline gets None
    (wait) — NEVER an acquire-only fallback that queues invisibly behind
    real slots."""
    a = _server(orch, "full-a", tier="large", priority=2, max_concurrent=2)
    b = _server(orch, "full-b", tier="large", priority=2, max_concurrent=1)
    _register(orch, a, b)
    for i in range(3):          # 2 on a + 1 on b = both quotas full
        sid = orch._pick_server("tiny", pipeline_key=f"e|{i}")
        assert sid is not None
        orch.release(sid)
    got = orch._pick_server("tiny", pipeline_key="e|9")
    assert got is None          # waits, no oversubscription
