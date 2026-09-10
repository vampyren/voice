"""The daemon side of the overlay protocol: spawn, throttle, survive, stop."""
import subprocess

import pytest

from voice.ui.overlay_client import (FRAME_S, OverlayClient, default_launcher,
                                     helper_command, probe_helper)


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

    client.restart()
    assert len(helper_processes.made) == 3
    client.send({"state": "done"})
    assert client.flush()
    assert helper_processes.made[2].lines() == [{"state": "done"}]
    client.stop()


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


# -- focus: a plain window would swallow the paste -----------------------------
def test_without_layer_shell_the_helper_is_not_launched_at_all(monkeypatch, caplog):
    """GTK 4 dropped the accept-focus hints, so only a layer-shell surface can
    refuse focus; a plain pill takes it and the paste lands in the pill."""
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ("gtk4",))
    monkeypatch.delenv("VOICE_OVERLAY_ALLOW_PLAIN_WINDOW", raising=False)
    spawned = []
    with caplog.at_level("WARNING", logger="voice.ui.overlay_client"):
        assert default_launcher(popen=lambda cmd, **kw: spawned.append(cmd)) is None
    assert spawned == []
    assert "gtk4-layer-shell" in caplog.text


def test_layer_shell_present_launches_and_still_demands_it(monkeypatch):
    monkeypatch.setattr("voice.ui.overlay_client._probe",
                        lambda python: ("gtk4", "layer-shell"))
    seen = {}
    default_launcher(popen=lambda cmd, **kw: seen.update(cmd=cmd))
    assert "--require-layer-shell" in seen["cmd"]


def test_the_plain_window_fallback_can_be_allowed_explicitly(monkeypatch):
    monkeypatch.setattr("voice.ui.overlay_client._probe", lambda python: ("gtk4",))
    monkeypatch.setenv("VOICE_OVERLAY_ALLOW_PLAIN_WINDOW", "1")
    seen = {}
    default_launcher(popen=lambda cmd, **kw: seen.update(cmd=cmd))
    assert "--require-layer-shell" not in seen["cmd"]
