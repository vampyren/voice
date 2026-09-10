"""The daemon side of the overlay protocol: spawn, throttle, survive, stop."""
import subprocess
import threading
import time

import pytest

from voice.ui.overlay_client import (FRAME_S, NO_LAYER_SHELL_EXIT, QUEUE_MAX, OverlayClient,
                                     default_launcher, helper_command, probe_helper)


def _client(launcher, clock=None, enabled=True):
    return OverlayClient(enabled, launcher=launcher, clock=clock)


def test_start_launches_the_helper_and_send_writes_one_json_line_each(helper_processes):
    client = _client(helper_processes)
    client.start()
    assert len(helper_processes.made) == 1
    client.send({"language": "sv"})
    client.send({"state": "recording"})
    client.send({"state": "error", "text": "boom"})
    assert client.flush()
    proc = helper_processes.made[0]
    assert proc.lines() == [{"language": "sv"}, {"state": "recording"},
                            {"state": "error", "text": "boom"}]
    assert proc.stdin.data.endswith(b"\n") and proc.stdin.data.count(b"\n") == 3
    client.stop()


def test_start_is_idempotent(helper_processes):
    client = _client(helper_processes)
    client.start()
    client.start()
    assert len(helper_processes.made) == 1
    client.stop()


def test_send_before_start_writes_nothing(helper_processes):
    client = _client(helper_processes)
    client.send({"state": "recording"})
    assert helper_processes.made == []


def test_a_disabled_client_never_launches_the_helper(helper_processes):
    client = _client(helper_processes, enabled=False)
    client.start()
    client.send({"state": "recording"})
    client.stop()
    assert helper_processes.made == []


def test_a_burst_of_levels_writes_one_and_keeps_the_latest(helper_processes):
    """30/s: within one frame only the first level goes out, and the newest one
    waits for the next window instead of being lost."""
    now = [0.0]
    client = _client(helper_processes, clock=lambda: now[0])
    client.start()
    for i in range(100):
        client.send({"level": i / 100})
    assert client.flush()
    proc = helper_processes.made[0]
    assert proc.lines() == [{"level": 0.0}]

    now[0] += FRAME_S
    client.send({"state": "transcribing"})
    assert client.flush()
    assert proc.lines() == [{"level": 0.0}, {"level": 0.99}, {"state": "transcribing"}]
    client.stop()


def test_levels_over_a_second_are_capped_at_thirty(helper_processes):
    now = [0.0]
    client = _client(helper_processes, clock=lambda: now[0])
    client.start()
    for i in range(1000):                      # 1 kHz of levels for one second
        now[0] = i / 1000
        client.send({"level": i / 1000})
    assert client.flush()
    written = helper_processes.made[0].lines()
    assert 25 <= len(written) <= 31, len(written)
    client.stop()


def test_a_dead_helper_is_restarted_once_and_then_stays_silent(helper_processes, caplog):
    client = _client(helper_processes)
    client.start()
    helper_processes.made[0].exit(1)

    with caplog.at_level("WARNING", logger="voice.ui.overlay_client"):
        client.send({"state": "recording"})
        assert client.flush()
        assert len(helper_processes.made) == 2                 # restarted exactly once
        assert helper_processes.made[1].lines() == [{"state": "recording"}]

        helper_processes.made[1].exit(1)
        client.send({"state": "transcribing"})
        client.send({"level": 0.5})
        assert client.flush()
    assert len(helper_processes.made) == 2                     # no second restart
    assert helper_processes.made[1].lines() == [{"state": "recording"}]
    assert sum("overlay" in r.getMessage() for r in caplog.records) >= 1

    client.stop()

    # The pill comes back the way the daemon brings it back - a fresh client on
    # the same launcher (`_rebuild_overlay_if_needed`), not a revival of this one.
    revived = _client(helper_processes)
    revived.start()
    assert len(helper_processes.made) == 3
    revived.send({"state": "done"})
    assert revived.flush()
    assert helper_processes.made[2].lines() == [{"state": "done"}]
    revived.stop()


def test_a_write_failure_never_reaches_the_caller(helper_processes):
    client = _client(helper_processes)
    client.start()
    proc = helper_processes.made[0]
    proc.stdin.close()                     # writing now raises ValueError
    client.send({"state": "recording"})    # must not raise
    assert client.flush()
    client.send({"state": "hidden"})       # and stays quiet afterwards
    assert client.flush()
    assert proc.stdin.data == b""
    client.stop()


def test_a_launcher_that_fails_disables_the_overlay_quietly(caplog):
    def boom():
        raise OSError("no such file")

    client = OverlayClient(True, launcher=boom)
    with caplog.at_level("WARNING", logger="voice.ui.overlay_client"):
        client.start()
        client.send({"state": "recording"})
    assert client.enabled is False
    assert len([r for r in caplog.records if "overlay" in r.getMessage()]) == 1
    client.stop()


def test_a_launcher_that_finds_no_interpreter_disables_the_overlay():
    client = OverlayClient(True, launcher=lambda: None)
    client.start()
    client.send({"state": "recording"})     # must not raise
    assert client.enabled is False
    client.stop()


def test_stop_closes_stdin_and_waits_at_most_a_second(helper_processes):
    client = _client(helper_processes)
    client.start()
    client.send({"state": "recording"})
    client.stop()
    proc = helper_processes.made[0]
    assert proc.stdin.closed is True
    assert proc.waits and all(w is not None and w <= 1.0 for w in proc.waits)
    client.send({"state": "recording"})     # after stop nothing more is written
    assert proc.lines() == [{"state": "recording"}]


def test_stop_terminates_a_helper_that_ignores_the_closed_stdin(helper_processes):
    client = _client(helper_processes)
    client.start()
    proc = helper_processes.made[0]

    def stubborn(timeout=None):
        proc.waits.append(timeout)
        raise subprocess.TimeoutExpired("voice-overlay", timeout)

    proc.wait = stubborn
    client.stop()
    assert proc.terminated is True


# -- launching the real helper -------------------------------------------------
def test_helper_command_prefers_the_system_python(monkeypatch, tmp_path):
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ("gtk4",))
    probe = probe_helper()
    assert probe.command[:3] == ["/usr/bin/python3", "-m", "voice.ui.overlay"]
    assert probe.features == ("gtk4",)
    assert probe.reason == ""


def test_helper_command_falls_back_to_the_console_script(monkeypatch):
    def probe(python):
        return ("gtk4", "layer-shell") if python != "/usr/bin/python3" else ()

    monkeypatch.setattr("voice.ui.overlay_client._probe", probe)
    monkeypatch.setattr("voice.ui.overlay_client.shutil.which",
                        lambda name: "/venv/bin/voice-overlay" if name == "voice-overlay" else None)
    got = probe_helper()
    assert got.command == ["/venv/bin/voice-overlay"]
    assert "layer-shell" in got.features


def test_no_interpreter_with_gi_means_no_command(monkeypatch):
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ())
    monkeypatch.setattr("voice.ui.overlay_client.shutil.which", lambda name: None)
    got = probe_helper()
    assert got.command is None
    assert "PyGObject" in got.reason


def test_default_launcher_passes_the_repo_and_the_session_environment(monkeypatch):
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ("gtk4", "layer-shell"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("PYTHONPATH", "/already/here")
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return "proc"

    assert default_launcher(position="top", lang="sv", popen=fake_popen) == "proc"
    assert seen["cmd"][:3] == ["/usr/bin/python3", "-m", "voice.ui.overlay"]
    assert "--position" in seen["cmd"] and "top" in seen["cmd"]
    assert "--lang" in seen["cmd"] and "sv" in seen["cmd"]
    env = seen["kwargs"]["env"]
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["PYTHONPATH"].split(":")[0].endswith("voice")
    assert "/already/here" in env["PYTHONPATH"]
    assert seen["kwargs"]["stdin"] is subprocess.PIPE


def test_default_launcher_rejects_a_nonsense_position(monkeypatch):
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ("gtk4", "layer-shell"))
    seen = {}
    default_launcher(position="sideways", lang="", popen=lambda cmd, **kw: seen.update(cmd=cmd))
    assert "sideways" not in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--position") + 1] == "bottom"


def test_helper_command_helper_is_the_probes_command(monkeypatch):
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ("gtk4",))
    assert helper_command() == probe_helper().command


@pytest.mark.boundary
def test_the_real_probe_finds_gtk4_on_this_machine():
    got = probe_helper()
    assert got.command is not None, got.reason
    assert "gtk4" in got.features


# -- focus: a fallback window would swallow the paste --------------------------
def test_the_helper_is_told_to_refuse_a_focus_stealing_window(monkeypatch):
    """GTK 4 dropped the accept-focus hints, so only a layer-shell surface can
    refuse focus; by default the helper must exit rather than steal it."""
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ("gtk4",))
    seen = {}
    default_launcher(popen=lambda cmd, **kw: seen.update(cmd=cmd))
    assert "--require-layer-shell" in seen["cmd"]


def test_the_fallback_window_can_be_allowed_by_config(monkeypatch):
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ("gtk4",))
    seen = {}
    default_launcher(allow_fallback=True, popen=lambda cmd, **kw: seen.update(cmd=cmd))
    assert "--require-layer-shell" not in seen["cmd"]


def test_a_helper_that_refuses_to_steal_focus_disables_the_overlay(helper_processes, caplog):
    """Exit 2 is the helper saying "no layer-shell here": there is nothing to
    retry, so it must not be restarted and status must say why."""
    client = _client(helper_processes)
    client.start()
    helper_processes.made[0].exit(NO_LAYER_SHELL_EXIT)
    with caplog.at_level("WARNING", logger="voice.ui.overlay_client"):
        client.send({"state": "recording"})
        client.send({"level": 0.3})
    assert len(helper_processes.made) == 1                 # never restarted
    assert client.status() == "disabled: no layer-shell"
    assert client.enabled is False
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "ui.overlay_allow_fallback" in messages[0] and "gtk4-layer-shell" in messages[0]
    client.stop()


def test_a_helper_given_up_on_for_no_layer_shell_is_closed_and_reaped(helper_processes):
    """Giving up is not a reason to hold the pipe: the daemon runs for days, and
    an unclosed stdin fd plus an unreaped child would outlive every dictation."""
    client = _client(helper_processes)
    client.start()
    proc = helper_processes.made[0]
    proc.exit(NO_LAYER_SHELL_EXIT)
    client.send({"state": "recording"})
    assert client.status() == "disabled: no layer-shell"
    assert proc.stdin.closed is True, "the helper's stdin fd was leaked"
    assert proc.waits, "the helper was never reaped"
    client.stop()


def test_a_helper_given_up_on_after_its_second_exit_is_closed_and_reaped(helper_processes):
    """The other give-up path in _alive(): the restart budget is spent and the
    helper that exited again is dropped - it has to be let go of too."""
    client = _client(helper_processes)
    client.start()
    helper_processes.made[0].exit(1)
    client.send({"state": "recording"})                    # spends the one restart
    assert client.flush(2.0)
    second = helper_processes.made[1]
    second.exit(1)
    client.send({"state": "transcribing"})                 # ... and now it stays off
    assert client.status() == "disabled: helper exited (1)"
    assert second.stdin.closed is True, "the helper's stdin fd was leaked"
    assert second.waits, "the helper was never reaped"
    client.stop()


def test_status_follows_the_helper(helper_processes):
    client = _client(helper_processes)
    assert client.status() == "not started"
    client.start()
    assert client.status() == "running"
    helper_processes.made[0].exit(1)
    assert client.status() == "running"                    # restarted once, silently
    helper_processes.made[1].exit(1)
    assert client.status() == "disabled: helper exited (1)"
    client.stop()
    assert client.status() == "disabled: helper exited (1)"   # the diagnosis survives
    assert OverlayClient(False).status() == "off"


def test_a_healthy_client_reports_stopped_after_shutdown(helper_processes):
    client = _client(helper_processes)
    client.start()
    client.stop()
    assert client.status() == "stopped"


def test_the_helper_is_verbose_when_the_daemon_is(monkeypatch):
    """`voice --verbose daemon` must be able to show what the pill received."""
    monkeypatch.setattr("voice.ui.overlay_client._probe",
                        lambda python: ("gtk4", "layer-shell"))
    seen = {}
    default_launcher(popen=lambda cmd, **kw: seen.update(cmd=cmd))
    assert "--verbose" not in seen["cmd"]
    default_launcher(verbose=True, popen=lambda cmd, **kw: seen.update(cmd=cmd))
    assert "--verbose" in seen["cmd"]


# -- stop() must not deadlock on a helper that stopped reading -----------------
class _FullPipe:
    """A pipe whose write parks holding the buffer lock, like a real
    BufferedWriter flushing to a child that never reads: `close()` then blocks
    too, which is what made stop() hang forever."""

    def __init__(self):
        self.data = b""
        self.closed = False
        self.entered = threading.Event()
        self._lock = threading.Lock()
        self._broken = threading.Event()

    def write(self, blob: bytes) -> int:
        with self._lock:
            self.entered.set()
            self._broken.wait(10)
            raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        with self._lock:
            if self._broken.is_set():
                raise BrokenPipeError(32, "Broken pipe")

    def close(self) -> None:
        with self._lock:                    # the writer holds it while parked
            self.closed = True

    def break_pipe(self) -> None:
        self._broken.set()


class _WedgedHelper:
    """A helper that stopped reading its stdin and only dies when signalled."""

    def __init__(self):
        self.stdin = _FullPipe()
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("voice-overlay", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdin.break_pipe()             # the child is gone: the write EPIPEs

    kill = terminate


def test_stop_kills_the_helper_instead_of_blocking_on_its_stdin():
    """A writer parked on a full pipe holds the stdin lock, so closing stdin
    first can never return. The child has to go first."""
    proc = _WedgedHelper()
    client = _client(lambda: proc)
    client.start()
    client.send({"state": "recording"})
    assert proc.stdin.entered.wait(2.0)       # the writer is parked in the pipe

    stopped = threading.Event()

    def stop():
        client.stop()
        stopped.set()

    threading.Thread(target=stop, name="stopper", daemon=True).start()
    assert stopped.wait(1.0), "stop() blocked on the helper's stdin"
    assert proc.terminated is True


# -- a helper that stops taking writes -----------------------------------------
class _SlowStdin:
    """A pipe whose write is still in flight when someone swaps the process."""

    def __init__(self):
        self.data = b""
        self.closed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, blob: bytes) -> int:
        self.entered.set()
        self.release.wait(5)
        if self.closed:
            raise ValueError("I/O operation on closed file")
        self.data += blob
        return len(blob)

    def flush(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def close(self) -> None:
        self.closed = True


def test_a_helper_that_stops_taking_writes_is_restarted_once(helper_processes):
    """A write failure is the helper dying mid-message, so it falls under the
    restart-once policy - it used to disable the pill for the whole session."""
    client = _client(helper_processes)
    client.start()
    helper_processes.made[0].stdin.close()          # writing now raises ValueError
    client.send({"state": "recording"})
    assert client.flush(2.0)
    assert len(helper_processes.made) == 2          # replaced, not given up on
    assert client.status() == "running"

    client.send({"state": "transcribing"})          # later messages reach the new one
    assert client.flush(2.0)
    assert helper_processes.made[1].lines() == [{"state": "transcribing"}]

    helper_processes.made[1].stdin.close()
    client.send({"state": "done"})
    assert client.flush(2.0)
    assert len(helper_processes.made) == 2          # and then it stays off
    assert client.status() == "disabled: helper not writable"
    client.stop()


def test_a_swap_under_an_in_flight_write_does_not_kill_the_new_helper(helper_processes):
    """`status()` respawns inline, on its own thread: it closes the old stdin
    while the writer may still be inside write(). The ValueError that raises
    belongs to the process that is already gone."""
    client = _client(helper_processes)
    client.start()
    slow = _SlowStdin()
    helper_processes.made[0].stdin = slow
    client.send({"state": "recording"})
    assert slow.entered.wait(2.0)                   # the writer is inside write()

    helper_processes.made[0].exit(1)
    assert client.status() == "running"             # closes the old stdin under it
    assert len(helper_processes.made) == 2
    slow.release.set()                              # the stale write now raises

    client.send({"state": "transcribing"})
    assert client.flush(2.0)
    assert client.status() == "running"
    assert helper_processes.made[1].lines() == [{"state": "transcribing"}]
    client.stop()


def test_send_returns_at_once_while_the_helper_is_respawned(helper_processes):
    """The respawn re-runs the interpreter probe; send() is called from the
    pw-record reader thread and must never wait for it."""
    def launcher():
        if helper_processes.made:                   # the respawn, not the first start
            time.sleep(0.5)                         # like the interpreter probe
        return helper_processes()

    client = _client(launcher)
    client.start()
    helper_processes.made[0].exit(1)

    started = time.monotonic()
    client.send({"state": "recording"})
    assert time.monotonic() - started < 0.05

    assert client.flush(2.0)
    assert len(helper_processes.made) == 2
    assert helper_processes.made[1].lines() == [{"state": "recording"}]
    client.stop()


def test_the_interpreter_probe_runs_once_per_process(monkeypatch):
    """Every respawn used to pay for the probe again - up to 10 s of subprocess."""
    probed = []
    monkeypatch.setattr("voice.ui.overlay_client._probe",
                        lambda python: probed.append(python) or ("gtk4",))
    default_launcher(popen=lambda cmd, **kw: "proc")
    default_launcher(popen=lambda cmd, **kw: "proc")
    assert probed == ["/usr/bin/python3"]


# -- one death must cost exactly one restart -----------------------------------
def test_a_write_in_flight_when_the_helper_dies_costs_no_second_restart(helper_processes):
    """`_alive()` spends the restart budget and queues the respawn behind a write
    that is already parked on the dead helper's pipe. That write then fails, and
    it must not be read as a *second* death - one exit, one restart."""
    client = _client(helper_processes)
    client.start()
    proc = helper_processes.made[0]
    slow = _SlowStdin()
    proc.stdin = slow
    client.send({"state": "recording"})
    assert slow.entered.wait(2.0)                   # the writer is parked in write()

    proc.exit(1)                                    # the helper dies under it
    slow.close()                                    # so the parked write will raise
    client.send({"state": "transcribing"})          # notices the exit, queues a respawn
    slow.release.set()                              # and now the stale write fails

    assert client.flush(2.0)
    assert len(helper_processes.made) == 2, "one exit must buy exactly one restart"
    assert client.status() == "running"

    client.send({"state": "done"})                  # later messages reach the new helper
    assert client.flush(2.0)
    assert {"state": "done"} in helper_processes.made[1].lines()
    client.stop()


def test_a_respawn_that_cannot_be_queued_can_still_be_retried(helper_processes):
    """When the queue is full the respawn sentinel does not fit, so no restart
    happened - and the budget must not have been spent on it either."""
    client = _client(helper_processes)
    client.start()
    proc = helper_processes.made[0]
    slow = _SlowStdin()
    proc.stdin = slow
    client.send({"state": "recording"})
    assert slow.entered.wait(2.0)
    for i in range(QUEUE_MAX + 4):                  # no room left for the sentinel
        client.send({"state": f"filler-{i}"})

    proc.exit(1)
    client.send({"state": "late"})                  # sees the death, cannot queue it
    assert len(helper_processes.made) == 1

    slow.release.set()                              # the writer drains the backlog
    assert client.flush(2.0)
    client.send({"state": "again"})                 # the restart is still affordable
    assert client.flush(2.0)
    assert len(helper_processes.made) == 2, "the failed queueing burnt the restart"
    assert {"state": "again"} in helper_processes.made[1].lines()
    assert client.status() == "running"
    client.stop()


# -- stop() must not leak the writer thread ------------------------------------
def test_stop_releases_the_writer_even_when_the_queue_is_full():
    """put_nowait(None) is dropped on a full queue, so the sentinel never
    arrives: the writer thread survives stop() and stdin is never closed."""
    proc = _WedgedHelper()
    client = _client(lambda: proc)
    client.start()
    client.send({"state": "recording"})
    assert proc.stdin.entered.wait(2.0)             # the writer is parked in write()
    for i in range(QUEUE_MAX + 4):                  # ... and the queue has no room
        client.send({"state": f"filler-{i}"})
    writer = client._writer

    stopped = threading.Event()
    threading.Thread(target=lambda: (client.stop(), stopped.set()),
                     name="stopper", daemon=True).start()
    assert stopped.wait(5.0), "stop() never returned"
    writer.join(2.0)
    assert not writer.is_alive(), "stop() leaked the writer thread"
    assert proc.stdin.closed is True, "stop() left the helper's stdin open"
