"""Startup capability check: the registry's max_concurrent/context_window
are corrected from the REAL values local llama.cpp boxes report (GET
/slots + /props) — local servers only, never remote."""
import json

import pytest

from kaisen.llm import ModelOrchestrator, Server


def _cfg_entry(**over):
    base = {
        "id": "srv", "type": "llama", "url": "http://127.0.0.1:8502/completion",
        "tier": "small", "max_concurrent": 2, "context_window": 50000,
    }
    base.update(over)
    return base


def _fake_get(monkeypatch, slots=1, n_ctx=105216, fail=False):
    """Stub requests.get: /slots -> slot list, /props -> n_ctx."""
    import requests

    def fake(url, **kw):
        if fail:
            raise requests.ConnectionError("down")
        if url.endswith("/slots"):
            resp = type("R", (), {"status_code": 200, "json": lambda s: [
                {"id": i, "is_processing": False} for i in range(slots)]})()
            return resp
        if url.endswith("/props"):
            resp = type("R", (), {"status_code": 200, "json": lambda s: {
                "default_generation_settings": {"n_ctx": n_ctx, "params": {}}}})()
            return resp
        if url.endswith("/health"):
            return type("R", (), {"status_code": 200, "json": lambda s: {}})()
        raise AssertionError(f"unexpected probe URL: {url}")

    monkeypatch.setattr("kaisen.llm.requests.get", fake)


def _servers_in_config(tmp_cfg):
    raw = json.loads(tmp_cfg.path.read_text())
    return {s["id"]: s for s in raw["llm"]["servers"]}


def _wait_until(pred, timeout=5.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("capability check did not take effect in time")


def test_boot_check_corrects_drift_and_persists(tmp_cfg, monkeypatch):
    tmp_cfg.llm["servers"] = [_cfg_entry()]
    tmp_cfg.save()
    _fake_get(monkeypatch, slots=1, n_ctx=105216)
    orch = ModelOrchestrator(tmp_cfg)   # boot thread runs the sweep async
    # wait for BOTH the in-memory correction and the config persist
    _wait_until(lambda: orch._servers["srv"].max_concurrent == 1)
    _wait_until(lambda: _servers_in_config(tmp_cfg)["srv"]["max_concurrent"] == 1)
    s = orch._servers["srv"]
    assert s.max_concurrent == 1        # 2 -> 1 real slot
    assert s.context_window == 105216   # 50000 -> real n_ctx
    persisted = _servers_in_config(tmp_cfg)["srv"]
    assert persisted["max_concurrent"] == 1
    assert persisted["context_window"] == 105216


def test_under_subscription_never_raised(tmp_cfg, monkeypatch):
    tmp_cfg.llm["servers"] = [_cfg_entry(max_concurrent=1, context_window=105216)]
    tmp_cfg.save()
    _fake_get(monkeypatch, slots=4, n_ctx=105216)
    orch = ModelOrchestrator(tmp_cfg)
    orch._startup_capability_check()
    s = orch._servers["srv"]
    assert s.max_concurrent == 1        # deliberate throttle respected
    assert s.context_window == 105216   # no drift, untouched


def test_remote_servers_never_probed(tmp_cfg, monkeypatch):
    tmp_cfg.llm["servers"] = [
        _cfg_entry(id="remote-1", type="openai", base_url="https://api.example.com",
                   max_concurrent=4, context_window=65536),
    ]
    tmp_cfg.save()
    probed = []
    monkeypatch.setattr("kaisen.llm.requests.get",
                        lambda url, **kw: probed.append(url) or (_ for _ in ()).throw(AssertionError))
    orch = ModelOrchestrator(tmp_cfg)
    orch._startup_capability_check()
    assert probed == []                 # nothing was fetched for the remote


def test_explicit_local_false_skips_llama(tmp_cfg, monkeypatch):
    tmp_cfg.llm["servers"] = [_cfg_entry(local=False)]
    tmp_cfg.save()
    probed = []
    monkeypatch.setattr("kaisen.llm.requests.get",
                        lambda url, **kw: probed.append(url) or (_ for _ in ()).throw(AssertionError))
    orch = ModelOrchestrator(tmp_cfg)
    orch._startup_capability_check()
    assert probed == []


def test_offline_server_left_untouched(tmp_cfg, monkeypatch):
    tmp_cfg.llm["servers"] = [_cfg_entry()]
    tmp_cfg.save()
    _fake_get(monkeypatch, fail=True)
    orch = ModelOrchestrator(tmp_cfg)
    orch._startup_capability_check()    # must not raise
    s = orch._servers["srv"]
    assert s.max_concurrent == 2 and s.context_window == 50000


def test_local_flag_defaults_from_type(tmp_cfg):
    llama = Server(_cfg_entry(), tmp_cfg)
    assert llama.local is True
    remote = Server(_cfg_entry(type="openai", base_url="https://x"), tmp_cfg)
    assert remote.local is False
    forced = Server(_cfg_entry(type="openai", local=True, base_url="https://x"), tmp_cfg)
    assert forced.local is True
