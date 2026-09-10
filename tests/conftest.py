import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Every test gets private XDG dirs so nothing touches the real config."""
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        d = tmp_path / var.lower()
        d.mkdir()
        monkeypatch.setenv(var, str(d))
    yield tmp_path
