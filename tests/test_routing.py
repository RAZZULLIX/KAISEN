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


# ── parallel generations: SHARED pool (default), opt-in reserve / cap ──
#
# A slot is granted for ONE generation and released when the stream ends, so
# no project can park on the pool.  Endpoint choice: priority bracket ->
# cache affinity -> rotation across equals.  When projects contend, the head
# of the service queue goes first and spends its turn on every attempt, so
# generations ROTATE between projects.  Two knobs are OPT-IN per project:
# `max_parallel` (its spend cap) and `reserve` (hold its endpoints).

def test_alloc_priority_brackets_fill_in_order(orch):
    """The user's spec: parallel generations fill the highest-priority
    endpoint up to its max_concurrent, then the next priority.  port1
    (prio 2, cap 3), port2 (prio 2, cap 1), port3 (prio 1, cap 1): 5
    generations -> 3 on port1, 1 on port2, 1 on port3."""
    from collections import Counter
    p1 = _server(orch, "p1", tier="large", priority=2, max_concurrent=3)
    p2 = _server(orch, "p2", tier="large", priority=2, max_concurrent=1)
    p3 = _server(orch, "p3", tier="large", priority=1, max_concurrent=1)
    _register(orch, p1, p2, p3)
    assigned = []
    for i in range(5):          # five generations in flight at once
        sid = orch._pick_server("tiny", pipeline_key=f"eng|{i}")
        assert sid is not None
        assigned.append(sid)
    c = Counter(assigned)
    assert c["p1"] == 3, c   # cap 3 filled first (highest priority)
    assert c["p2"] == 1, c   # then the other prio-2 endpoint
    assert c["p3"] == 1, c   # then the lower-priority endpoint
    for sid in assigned:
        orch.release(sid)


def test_alloc_spreads_within_an_equal_priority_bracket(orch):
    """The field bug: parallel generations over N EQUAL endpoints (same
    tier AND priority, cap > 1 because the real slot count is unreadable)
    stacked on the first boxes — 6 generations landed 3+3 on two servers
    while the other four never received a request.  Equal endpoints are
    consumed in ROTATION: one generation each."""
    from collections import Counter
    servers = [_server(orch, f"eq{i}", tier="large", priority=1,
                       max_concurrent=3) for i in range(6)]
    _register(orch, *servers)
    assigned = []
    for i in range(6):
        sid = orch._pick_server("tiny", pipeline_key=f"eng|{i}")
        assert sid is not None
        assigned.append(sid)
    assert set(assigned) == {f"eq{i}" for i in range(6)}, assigned
    assert set(Counter(assigned).values()) == {1}, Counter(assigned)


def test_alloc_lower_priority_untouched_while_high_has_room(orch):
    """Fewer generations than the high bracket's capacity never spills
    down: the lower-priority endpoint stays unused until it is FULL."""
    hi = _server(orch, "hi", tier="large", priority=2, max_concurrent=6)
    lo = _server(orch, "lo", tier="large", priority=1, max_concurrent=2)
    _register(orch, hi, lo)
    for i in range(4):
        sid = orch._pick_server("tiny", pipeline_key=f"eng|{i}")
        assert sid == "hi"      # 4 < hi's cap 6 -> never spills to lo
        orch.release(sid)


def test_alloc_affinity_keeps_endpoint_across_generations(orch):
    """A generation returns to the endpoint that already holds its KV while
    that endpoint is free (cache affinity) — the same (engine, pipeline)
    key keeps its server instead of churning the pool."""
    a = _server(orch, "aff-a", tier="large", priority=2, max_concurrent=2)
    b = _server(orch, "aff-b", tier="large", priority=2, max_concurrent=2)
    _register(orch, a, b)
    first = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(first)
    second = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(second)
    assert first == second


def test_alloc_affinity_dropped_when_its_endpoint_goes_offline(orch):
    """When a generation's endpoint goes offline the stale affinity hint is
    ignored and the generation streams elsewhere."""
    a = _server(orch, "off-a", tier="large", priority=2, max_concurrent=2)
    b = _server(orch, "off-b", tier="large", priority=2, max_concurrent=2)
    _register(orch, a, b)
    first = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(first)
    orch._servers[first].mark_online(False)
    second = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(second)
    assert second != first
    assert second in ("off-a", "off-b")


def test_alloc_saturated_endpoint_falls_through_to_a_free_one(orch):
    """A full endpoint must never make a generation WAIT while another
    endpoint has a free slot — that is the "server that does not work"
    symptom (queued work next to idle slots)."""
    a = _server(orch, "sat-a", tier="large", priority=2, max_concurrent=2)
    b = _server(orch, "sat-b", tier="large", priority=2, max_concurrent=2)
    _register(orch, a, b)
    held = orch._pick_server("tiny", pipeline_key="eng|0")
    orch.release(held)                        # eng|0's generation finished
    hs = orch._servers[held]
    hs.acquire()
    hs.acquire()                              # another generation saturates it
    assert hs._inflight == hs._capacity
    got = orch._pick_server("tiny", pipeline_key="eng|0")
    assert got is not None and got != held    # streams on the free endpoint
    orch.release(got)
    hs.release()
    hs.release()


def test_alloc_plain_callers_keep_per_request_pick(orch):
    """No pipeline key (deepwork/suggest/repair) -> unchanged per-request
    selection: no affinity, no per-project generation count."""
    a = _server(orch, "plain-a", tier="large")
    b = _server(orch, "plain-b", tier="large")
    _register(orch, a, b)
    assert orch._pick_server("tiny") is not None
    assert orch._last == {}
    assert orch._live == {}


def test_alloc_full_pool_waits_not_oversubscribes(orch):
    """When every slot in the pool is streaming, a new generation gets None
    (waits) — an endpoint is never oversubscribed with a queue invisible
    behind the real slots."""
    a = _server(orch, "full-a", tier="large", priority=2, max_concurrent=2)
    b = _server(orch, "full-b", tier="large", priority=2, max_concurrent=1)
    _register(orch, a, b)
    assigned = []
    for i in range(3):          # 2 on a + 1 on b = whole pool in flight
        sid = orch._pick_server("tiny", pipeline_key=f"e|{i}")
        assert sid is not None
        assigned.append(sid)
    assert orch._pick_server("tiny", pipeline_key="e|9") is None
    for sid in assigned:
        orch.release(sid)


def test_alloc_capacity_shrink_is_respected(orch):
    """Config max_concurrent=6, then llama.cpp /slots learns the box really
    has 2: at most 2 generations stream on it, and the next one spills down
    the priority bracket instead of queueing invisibly behind them."""
    hi = _server(orch, "shrink-hi", tier="large", priority=2,
                 max_concurrent=6)
    lo = _server(orch, "shrink-lo", tier="large", priority=1,
                 max_concurrent=2)
    _register(orch, hi, lo)
    inflight = [orch._pick_server("tiny", pipeline_key=f"e|{i}")
                for i in range(6)]
    assert set(inflight) == {"shrink-hi"}     # 6 real slots before the shrink
    orch._servers["shrink-hi"]._detected_slots = 2
    for sid in inflight[:4]:                  # those generations end
        orch.release(sid)
    assert hi._inflight == 2
    got = orch._pick_server("tiny", pipeline_key="e|6")
    assert got == "shrink-lo"                 # spilling down, not queueing
    for sid in inflight[4:]:
        orch.release(sid)
    orch.release(got)


def test_projects_rotate_generations_on_a_shared_pool(orch):
    """Two projects, two endpoints: generations ROTATE — a project that
    already had its turn may NOT take a second slot while the other waits,
    and a slot freed when a generation ends is streamed on at once (no
    endpoint idles while work is queued)."""
    ep1 = _server(orch, "ep1", tier="large", max_concurrent=1)
    ep2 = _server(orch, "ep2", tier="large", max_concurrent=1)
    _register(orch, ep1, ep2)
    orch._need_enter("projA")
    orch._need_enter("projB")
    try:
        first = orch._pick_server("tiny", pipeline_key="projA|0")
        assert first == "ep1"                    # head of the queue
        # projB waits: projA may NOT take a second slot even though ep2 is
        # free — the fairness unit is one generation per project
        assert orch._pick_server("tiny", pipeline_key="projA|1") is None
        second = orch._pick_server("tiny", pipeline_key="projB|0")
        assert {first, second} == {"ep1", "ep2"}  # one slot each
        # saturated: nobody is oversubscribed
        assert orch._pick_server("tiny", pipeline_key="projA|1") is None
        assert orch._pick_server("tiny", pipeline_key="projB|1") is None
        # projA's generation ends -> the freed slot streams again immediately
        orch.release(first)
        orch._generation_done("projA|0")
        nxt = orch._pick_server("tiny", pipeline_key="projA|1")
        assert nxt == first
        assert orch._servers[nxt]._inflight == 1
        orch.release(nxt)
        orch.release(second)
    finally:
        orch._need_exit("projA")
        orch._need_exit("projB")


def test_one_project_alone_takes_every_slot(orch):
    """Nothing is capped per project: a project alone in the queue takes
    every endpoint its generations ask for, so parallel_gens >= slots
    leaves no server idle."""
    servers = [_server(orch, f"solo{i}", tier="large", max_concurrent=1)
               for i in range(6)]
    _register(orch, *servers)
    orch._need_enter("solo")
    try:
        got = [orch._pick_server("tiny", pipeline_key=f"solo|{i}")
               for i in range(6)]
        assert set(got) == {f"solo{i}" for i in range(6)}   # the whole pool
        assert orch._pick_server("tiny", pipeline_key="solo|6") is None
        for sid in got:
            orch.release(sid)
    finally:
        orch._need_exit("solo")


def test_engine_with_nothing_waiting_stops_holding_its_turn(orch):
    """A project whose generations stopped waiting leaves the service queue,
    so a parked project cannot block the ones behind it."""
    ep1 = _server(orch, "q1", tier="large", max_concurrent=1)
    ep2 = _server(orch, "q2", tier="large", max_concurrent=1)
    _register(orch, ep1, ep2)
    orch._need_enter("gone")
    orch._need_enter("here")
    held = orch._pick_server("tiny", pipeline_key="gone|0")
    orch._need_exit("gone")
    assert orch._need_order == ["here"]
    orch.release(held)
    got = orch._pick_server("tiny", pipeline_key="here|0")
    assert got in ("q1", "q2")                # the parked project no longer blocks
    assert orch._servers[got]._inflight == 1
    orch.release(got)
    orch._need_exit("here")


def test_ineligible_head_does_not_block_the_queue(orch):
    """A project whose requirement filters the free capacity out (min_tier
    above the free endpoint's tier) spends its turn on the attempt instead
    of blocking the projects behind it."""
    small = _server(orch, "small-ep", tier="small", max_concurrent=1)
    _register(orch, small)
    orch._need_enter("bigonly")
    orch._need_enter("anytier")
    try:
        assert orch._pick_server("large", pipeline_key="bigonly|0") is None
        got = orch._pick_server("tiny", pipeline_key="anytier|0")
        assert got == "small-ep"
        orch.release(got)
    finally:
        orch._need_exit("bigonly")
        orch._need_exit("anytier")


# ── the two OPT-IN knobs: max_parallel (spend cap) and reserve ─────────

def test_max_parallel_caps_one_project_without_blocking_others(orch):
    """OPT-IN `engine.max_parallel`: a project stops taking slots once N of
    its generations are in flight — and, crucially, a capped project does
    NOT hold the pool: the project behind it is served."""
    servers = [_server(orch, f"cap{i}", tier="large", max_concurrent=1)
               for i in range(4)]
    _register(orch, *servers)
    orch._need_enter("A")
    try:
        a0 = orch._pick_server("tiny", pipeline_key="A|0", max_parallel=2)
        a1 = orch._pick_server("tiny", pipeline_key="A|1", max_parallel=2)
        assert a0 and a1 and a0 != a1
        assert orch._live["A"] == 2
        # capped: A's third generation waits — its turn is spent on the
        # attempt, so the queue keeps moving
        assert orch._pick_server("tiny", pipeline_key="A|2",
                                 max_parallel=2) is None
        # A has nothing else waiting (its 2 generations are streaming), so it
        # leaves the queue; B is served even though A is at its cap
        orch._need_exit("A")
        orch._need_enter("B")
        got = orch._pick_server("tiny", pipeline_key="B|0")
        assert got is not None
        assert orch._live["B"] == 1
        got2 = orch._pick_server("tiny", pipeline_key="B|1")
        assert got2 is not None               # B has no cap of its own
        # one of A's generations ends -> A may take one more (cap is a
        # ceiling on generations IN FLIGHT, not a lifetime budget)
        orch.release(a0)
        orch._generation_done("A|0")
        assert orch._live["A"] == 1
        orch._need_exit("B")
        orch._need_enter("A")
        again = orch._pick_server("tiny", pipeline_key="A|3", max_parallel=2)
        assert again is not None
        for sid in (a1, got, got2, again):
            orch.release(sid)
    finally:
        orch._need_exit("A")
        orch._need_exit("B")


def test_reserve_holds_endpoints_for_a_project(orch):
    """OPT-IN `engine.reserve`: the project HOLDS its endpoints across
    generations (its pipeline always has a server), and a non-reserving
    project cannot take a reserved slot."""
    ep1 = _server(orch, "res1", tier="large", max_concurrent=1)
    ep2 = _server(orch, "res2", tier="large", max_concurrent=1)
    _register(orch, ep1, ep2)
    orch._need_enter("owner")
    try:
        first = orch._pick_server("tiny", pipeline_key="owner|0", reserve=True)
        second = orch._pick_server("tiny", pipeline_key="owner|1", reserve=True)
        assert {first, second} == {"res1", "res2"}
        assert set(orch._hold.values()) == {"res1", "res2"}
        # both generations finish, but the endpoints stay HELD
        orch.release(first)
        orch._generation_done("owner|0")
        orch.release(second)
        orch._generation_done("owner|1")
        # the owner's pipeline comes back to ITS endpoint (no churn)
        again = orch._pick_server("tiny", pipeline_key="owner|0", reserve=True)
        assert again == first
        orch.release(again)
        orch._generation_done("owner|0")
        # the owner has nothing waiting; a shared project still gets nothing
        # because every slot is reserved
        orch._need_exit("owner")
        orch._need_enter("other")
        assert orch._pick_server("tiny", pipeline_key="other|0") is None
    finally:
        orch._need_exit("owner")
        orch._need_exit("other")


def test_reserve_released_when_the_engine_stops(orch):
    """Stopping/pausing a reserving project returns its endpoints to the
    shared pool (release_pipeline_slots)."""
    ep1 = _server(orch, "rel1", tier="large", max_concurrent=1)
    _register(orch, ep1)
    orch._need_enter("owner")
    try:
        held = orch._pick_server("tiny", pipeline_key="owner|0", reserve=True)
        assert held == "rel1"
        orch.release(held)
        orch._generation_done("owner|0")
        orch._need_exit("owner")
        orch._need_enter("other")
        assert orch._pick_server("tiny", pipeline_key="other|0") is None
        orch.release_pipeline_slots("owner")
        assert orch._hold == {}
        got = orch._pick_server("tiny", pipeline_key="other|0")
        assert got == "rel1"
        orch.release(got)
    finally:
        orch._need_exit("owner")
        orch._need_exit("other")


def test_generation_counter_tracks_release(orch):
    """`_generation_done` drops the finished generation from the project's
    in-flight count (what `max_parallel` is checked against) and wakes the
    waiters."""
    a = _server(orch, "count-a", tier="large", max_concurrent=1)
    _register(orch, a)
    sid = orch._pick_server("tiny", pipeline_key="countA|0")
    assert orch._live == {"countA": 1}
    orch.release(sid)
    orch._generation_done("countA|0")
    assert orch._live == {}
    assert orch._last == {"countA|0": sid}     # affinity survives the release


def test_pool_scales_to_hundreds_of_endpoints(orch):
    """The pool must drive hundreds of endpoints (this is what replaced the
    old Swarm layer): N generations over N single-slot endpoints land ONE
    per endpoint — no stacking (the twins bug) and no endpoint left idle —
    a fully saturated pool WAITS instead of over-subscribing an endpoint,
    and a freed slot is handed out again at once."""
    n = 400
    servers = [_server(orch, f"ep{i:03d}", tier="large", priority=1,
                       max_concurrent=1) for i in range(n)]
    _register(orch, *servers)

    assigned = [orch._pick_server("tiny", pipeline_key=f"p|{i}")
                for i in range(n)]
    assert all(assigned), "every endpoint must take exactly one generation"
    assert len(set(assigned)) == n, "generations must spread one per endpoint"

    # saturated: a new generation waits, it never double-books an endpoint
    assert orch._pick_server("tiny", pipeline_key="p|overflow") is None

    # a finished generation frees its endpoint for the next one immediately
    orch.release(assigned[0])
    orch._generation_done("p|0")
    got = orch._pick_server("tiny", pipeline_key="p|after")
    assert got is not None, "the freed slot must be usable at once"
    orch.release(got)
