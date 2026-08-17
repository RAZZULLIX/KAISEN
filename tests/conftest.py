"""Shared fixtures. No network, no real engines — temp dirs only."""
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
