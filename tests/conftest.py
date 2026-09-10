import json
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


@pytest.fixture(autouse=True)
def no_desktop_notifications(monkeypatch):
    """Nothing in the suite may pop a real notification on the owner's desktop.

    A Daemon built without an injected notifier used to reach notify-send for
    real. Every such construction is a test bug, and this says so rather than
    letting it out of the process.
    """
    import subprocess

    real_popen = subprocess.Popen

    def guard(argv, *args, **kwargs):
        if argv and str(argv[0]) == "notify-send":
            raise AssertionError(f"a test tried to notify the real desktop: {list(argv)}")
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr("voice.ui.notify.subprocess.Popen", guard)


@pytest.fixture(autouse=True)
def fresh_overlay_probe():
    """The helper probe is answered once per process; tests stub the interpreters."""
    from voice.ui.overlay_client import reset_probe_cache
    reset_probe_cache()
    yield
    reset_probe_cache()


class FakeStdin:
    """A stdin pipe that records what the daemon writes to the overlay helper."""

    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, blob: bytes) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        self.data += blob
        return len(blob)

    def flush(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def close(self) -> None:
        self.closed = True


class FakeHelperProcess:
    """Stands in for the `voice-overlay` helper: a Popen-shaped stdin sink."""

    def __init__(self):
        self.stdin = FakeStdin()
        self.returncode = None
        self.terminated = False
        self.waits = []

    # -- Popen surface ----------------------------------------------------
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    kill = terminate

    # -- test helpers -----------------------------------------------------
    def exit(self, code: int = 1) -> None:
        """Pretend the helper died on its own."""
        self.returncode = code

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.stdin.data.decode().splitlines()]


@pytest.fixture
def helper_processes():
    """A launcher that hands out FakeHelperProcess objects, and the list of them."""
    made: list[FakeHelperProcess] = []

    def launcher():
        made.append(FakeHelperProcess())
        return made[-1]

    launcher.made = made
    return launcher
