import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch, request):
    """Every test gets private XDG dirs so nothing touches the real config.

    Tests marked `boundary` need a live PipeWire/Wayland session, which lives under the
    real XDG_RUNTIME_DIR, so that one variable is left untouched for them.
    """
    is_boundary = request.node.get_closest_marker("boundary") is not None
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        if var == "XDG_RUNTIME_DIR" and is_boundary:
            continue
        d = tmp_path / var.lower()
        d.mkdir()
        monkeypatch.setenv(var, str(d))
    yield tmp_path
