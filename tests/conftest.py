"""Shared fixtures. HERMETIC: no test may touch a real endpoint.

Two guarantees are enforced here, not assumed:

1. `get_config()` never returns the REAL `config.json`/`secrets.json`.
   A test that constructs an orchestrator without an explicit config used
   to pick up the user's live server registry, and its probe/reprobe
   loops then hammered those boxes (401s in their server logs whenever
   the suite ran).  The session fixture pins the process-wide config to a
   temp file with NO servers.

2. Every outbound HTTP call to a non-loopback host is REFUSED.  Tests may
   talk to their own fake servers on 127.0.0.1; anything else is a bug,
   and it fails loudly instead of leaking to production boxes.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from kaisen.config import FrameworkConfig  # noqa: E402
from kaisen.projects import ProjectRegistry  # noqa: E402


@pytest.fixture
def tmp_cfg(tmp_path):
    """FrameworkConfig backed by a temp config.json (fresh defaults)."""
    return FrameworkConfig(tmp_path / "config.json")


@pytest.fixture
def registry(tmp_path):
    """ProjectRegistry rooted at a temp projects/ dir."""
    root = tmp_path / "projects"
    root.mkdir()
    return ProjectRegistry(root)


@pytest.fixture(autouse=True)
def _reset_shared_workers():
    """The worker pool is a process-wide singleton — reset it between tests
    so no test inherits another's worker processes or handlers."""
    from kaisen.workers import reset_worker_pool
    reset_worker_pool()
    yield
    reset_worker_pool()


# ----------------------------------------------------------------------
# hermeticity guards (session scoped)
# ----------------------------------------------------------------------

# Ports that host REAL services on this machine (llama.cpp).  A test may
# talk to its own fake servers on any other loopback port; these are out of
# bounds even though they are local.
LIVE_SERVICE_PORTS = {8502, 8503, 8504}


def _is_local(host: str) -> bool:
    host = (host or "").split(":")[0].strip("[]").lower()
    return host in ("", "localhost", "127.0.0.1", "::1", "0.0.0.0")


def _port(netloc: str) -> int:
    try:
        tail = (netloc or "").rsplit(":", 1)[-1]
        return int(tail)
    except (ValueError, IndexError):
        return 0


@pytest.fixture(scope="session", autouse=True)
def _no_real_config():
    """Pin the process-wide config to a temp file with NO servers, so no
    test can inherit the user's live server registry."""
    import tempfile
    from kaisen import config as cfg_mod
    tmp = Path(tempfile.mkdtemp()) / "config.json"
    cfg = FrameworkConfig(tmp)
    cfg.llm["servers"] = []
    cfg.llm["active_ids"] = []
    saved = cfg_mod._config
    cfg_mod._config = cfg
    yield cfg
    cfg_mod._config = saved


@pytest.fixture(scope="session", autouse=True)
def _no_network(_no_real_config):
    """Refuse every outbound request to a non-loopback host.  This is the
    regression guard for "the suite probed the user's llama.cpp boxes"."""
    import requests
    from urllib.parse import urlparse

    real_request = requests.sessions.Session.request
    offender: list = []

    def guarded(self, method, url, *a, **kw):
        try:
            host = urlparse(str(url)).netloc
        except Exception:
            host = ""
        port = _port(host)
        if not _is_local(host) or port in LIVE_SERVICE_PORTS:
            offender.append((method, str(url)))
            raise AssertionError(
                f"HERMETIC TEST VIOLATION: {method} {url} — tests must never "
                f"touch a real endpoint (host {host!r}, port {port})")
        return real_request(self, method, url, *a, **kw)

    requests.sessions.Session.request = guarded
    try:
        yield
    finally:
        requests.sessions.Session.request = real_request
