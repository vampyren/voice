import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal

from voice.config import Config
from voice.daemon import Daemon, hotkey_specs
from voice.hotkey.keyspec import parse_keyspec


class FakeListener:
    def __init__(self):
        self.started = False
        self.specs = None

    def start(self): self.started = True
    def stop(self): self.started = False
    def capture_next(self, cb): self.cb = cb
    def modifiers_held(self): return False
    def devices_ok(self): return True


class FailingStopListener(FakeListener):
    """A listener whose stop() blows up, to exercise shutdown()'s error handling."""

    def stop(self):
        raise RuntimeError("evdev stop boom")


class FakeSender:
    name = "fake"
    def send_chord(self, codes): pass
    def available(self): return True


class FakeTray(QObject):
    """Records set_profiles() calls without touching any real QMenu/QAction."""

    state_changed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.calls = []

    def set_profiles(self, names, active):
        self.calls.append((list(names), active))

    def show(self):
        pass


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_hotkey_specs_reads_all_bindings(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.recall", "KEY_F14")
    specs = hotkey_specs(cfg)
    assert specs["dictate"] == parse_keyspec("KEY_F13")
    assert specs["recall"] == parse_keyspec("KEY_F14")
    assert specs["cancel"] == parse_keyspec("KEY_ESC")


def test_handle_commands_and_profile_switch(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda profile, secret: type("T", (), {
        "name": profile["backend"], "describe": lambda self: f"fake {profile['model']}",
        "warmup": lambda self: None, "transcribe": lambda self, *a: None})())
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender())
    d.build()
    assert d.handle({"cmd": "ping"}) == {"ok": True}
    st = d.handle({"cmd": "status"})
    assert st["ok"] and st["state"] == "idle" and st["profile"] == "local" and "large-v3-turbo" in st["backend"]
    assert d.handle({"cmd": "profile", "name": "openai"})["ok"]
    assert Config.load().get("stt.active") == "openai"
    assert "gpt-transcribe" in d.handle({"cmd": "status"})["backend"]
    bad = d.handle({"cmd": "profile", "name": "ghost"})
    assert bad["ok"] is False and "ghost" in bad["error"]
    assert d.handle({"cmd": "nope"})["ok"] is False
    d.shutdown()


def test_apply_config_rebinds_hotkeys(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender())
    d.build()
    cfg.set("hotkeys.dictate", "KEY_F20")
    cfg.save()
    d.apply_config()
    assert d.tracker.feed(parse_keyspec("KEY_F20").codes.__iter__().__next__(), 1) == [("dictate", "press")]
    d.shutdown()


def test_profile_command_applies_config_only_on_qt_thread(isolated_xdg, qapp, monkeypatch):
    """`profile`/`reload` validate on the IPC thread but must defer every mutation -
    set, save and apply_config - to the Qt thread via _Bridge."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": p["backend"], "describe": lambda self: p.get("model", ""), "warmup": lambda self: None})())
    cfg = Config.load()
    tray = FakeTray()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=tray)
    d.build()
    tray.calls.clear()
    result = {}

    def worker():
        result["reply"] = d.handle({"cmd": "profile", "name": "openai"})

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2)
    assert result["reply"] == {"ok": True, "profile": "openai"}
    assert Config.load().get("stt.active") == "local"      # validated only; not yet written
    assert tray.calls == []                                # nothing applied on the IPC thread
    qapp.processEvents()
    assert Config.load().get("stt.active") == "openai"     # set + saved on the Qt thread
    assert tray.calls and tray.calls[-1][1] == "openai"    # and applied there too
    d.shutdown()


def test_build_survives_unknown_active_profile_and_notifies(isolated_xdg, qapp):
    cfg = Config.load()
    cfg.set("stt.active", "ghost")
    cfg.save()
    notified = []
    notifier = type("N", (), {
        "notify": lambda self, title, body, urgency="normal": notified.append((title, body, urgency)),
        "set_enabled": lambda self, enabled: None,
    })()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), notifier=notifier)
    d.build()  # must not raise even though stt.active refers to a missing profile
    st = d.handle({"cmd": "status"})
    assert st["ok"] and "ghost" in st["backend"]
    assert any(title == "Transcription profile problem" and urgency == "critical" for title, _, urgency in notified)
    d.shutdown()


def test_warmup_only_announces_idle_when_dictation_is_idle(isolated_xdg, qapp, monkeypatch):
    from voice.pipeline import State  # local: only this test needs the enum

    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    cfg = Config.load()
    tray = FakeTray()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=tray)
    d.build()
    emitted = []
    d.tray.state_changed.connect(lambda s, det: emitted.append((s, det)))

    d.dictation._state = State.RECORDING
    d._warmup()
    assert emitted == []                       # mid-dictation: must not announce "idle"

    d.dictation._state = State.IDLE
    d._warmup()
    assert emitted and emitted[-1][0] == "idle"
    d.shutdown()


def test_shutdown_cancels_recording_and_survives_listener_errors(isolated_xdg, qapp, monkeypatch, caplog):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    cfg = Config.load()
    d = Daemon(cfg, listener=FailingStopListener(), sender=FakeSender())
    d.build()
    cancelled = []
    d.dictation.cancel = lambda: cancelled.append(True)
    with caplog.at_level("ERROR", logger="voice.daemon"):
        d.shutdown()  # must not raise even though listener.stop() blows up
    assert cancelled == [True]
    assert "listener" in caplog.text.lower()


def test_apply_config_rebuilds_transcriber_only_when_profile_changes(isolated_xdg, qapp, monkeypatch):
    calls = []

    def fake_make_transcriber(profile, secret):
        calls.append(profile.get("backend"))
        return type("T", (), {"name": profile["backend"], "describe": lambda self: "x",
                              "warmup": lambda self: None})()

    monkeypatch.setattr("voice.daemon.make_transcriber", fake_make_transcriber)
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender())
    d.build()
    assert len(calls) == 1

    cfg.set("hotkeys.dictate", "KEY_F21")
    cfg.save()
    d.apply_config()
    assert len(calls) == 1   # hotkey-only change: transcriber not rebuilt

    cfg.set("stt.active", "openai")
    cfg.save()
    d.apply_config()
    assert len(calls) == 2   # profile changed: rebuilt exactly once
    d.shutdown()


def test_run_is_noop_when_already_running(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.is_running", lambda: True)
    sent = []
    monkeypatch.setattr("voice.daemon.send", lambda req: sent.append(req) or {"ok": True})
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender())

    def fail_build():
        raise AssertionError("build() must not run when a daemon is already running")

    d.build = fail_build
    assert d.run() == 0
    assert sent and sent[-1]["cmd"] == "settings"


def test_run_hands_over_when_the_socket_is_taken_after_the_initial_check(isolated_xdg, qapp, monkeypatch):
    """A second instance that passes is_running() but loses the bind race must hand
    over to the live daemon, never unlink its socket."""
    from PySide6.QtWidgets import QApplication

    from voice import ipc

    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    # If run() ever reaches the event loop here it has already taken over the
    # socket; 99 makes that unmistakable instead of hanging the suite.
    monkeypatch.setattr(QApplication, "exec", lambda self: 99)
    live = ipc.Server(lambda req: {"ok": True, "echo": req.get("cmd")})
    live.start()
    monkeypatch.setattr("voice.daemon.is_running", lambda: False)      # simulate losing the race
    sent = []
    monkeypatch.setattr("voice.daemon.send", lambda req: sent.append(req) or {"ok": True})
    listener = FakeListener()
    d = Daemon(Config.load(), listener=listener, sender=FakeSender(), tray=FakeTray())
    try:
        assert d.run() == 0
        assert sent and sent[-1]["cmd"] == "settings"
        assert listener.started is False                               # never took over the keyboard
        assert ipc.send({"cmd": "ping"}) == {"ok": True, "echo": "ping"}   # live socket survived
    finally:
        live.stop()


def test_broken_profile_fails_as_a_transcription_error_and_keeps_the_audio(isolated_xdg, qapp):
    """_prompt() runs before transcribe(); a ValueError there bypassed the
    TranscriptionError path, so the recording was discarded instead of kept."""
    import numpy as np

    cfg = Config.load()
    cfg.set("stt.active", "ghost")
    cfg.save()
    quiet = type("N", (), {"notify": lambda self, *a, **k: None,
                           "set_enabled": lambda self, enabled: None})()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=quiet)
    d.build()
    assert d._prompt() is None                     # no resolvable profile to read a prompt from

    d.dictation._executor = lambda fn: fn()        # run the worker inline
    d.history.keep_audio(np.ones(16000, dtype=np.int16))
    d.dictation.retry()

    assert d.dictation.last_error and "ghost" in d.dictation.last_error
    assert d.history.take_audio() is not None      # kept, so a retry is still possible
    d.shutdown()


def _must_not_build(*a, **kw):
    raise AssertionError("the daemon must not start with an unreadable config")


def test_main_reports_a_broken_config_and_exits_2(isolated_xdg, monkeypatch, capsys):
    from voice import paths
    from voice.daemon import main

    paths.config_file().write_text("this = [unclosed")
    notified = []
    monkeypatch.setattr("voice.daemon.Notifier", lambda *a, **kw: type("N", (), {
        "notify": lambda self, title, body, urgency="normal": notified.append((title, body, urgency)),
    })())
    monkeypatch.setattr("voice.daemon.Daemon", _must_not_build)

    assert main() == 2
    assert "config.toml" in capsys.readouterr().err
    assert notified and notified[-1][2] == "critical" and "config.toml" in notified[-1][1]


def test_apply_config_keeps_previous_settings_when_the_file_is_broken(isolated_xdg, qapp, monkeypatch):
    from voice import paths

    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    notified = []
    notifier = type("N", (), {
        "notify": lambda self, title, body, urgency="normal": notified.append((title, urgency)),
        "set_enabled": lambda self, enabled: None})()
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=notifier)
    d.build()

    paths.config_file().write_text("broken = [")
    d.apply_config()          # a reload of unparseable TOML must not abort mid-apply

    dictate = next(iter(parse_keyspec("KEY_F13").codes))
    assert d.tracker.feed(dictate, 1) == [("dictate", "press")]   # old binding still live
    assert d.config.get("hotkeys.dictate") == "KEY_F13"
    assert any("keeping previous settings" in title for title, _ in notified)
    assert notified[-1][1] == "critical"
    d.shutdown()


def test_warmup_does_not_occupy_the_dictation_worker(isolated_xdg, qapp, monkeypatch):
    """Loading a model takes tens of seconds. On the single dictation worker it
    blocks recall/retry, and a queued paste then fires whenever the load ends."""
    import time

    from voice.history import Entry
    from voice.inject.injector import InjectResult

    blocked, release = threading.Event(), threading.Event()

    class SlowTranscriber:
        name = "slow"

        def describe(self):
            return "slow"

        def warmup(self):
            blocked.set()
            release.wait(10)

        def transcribe(self, *a):
            raise AssertionError("not reached")

    class RecordingInjector:
        def __init__(self):
            self.texts = []

        def inject(self, text):
            self.texts.append(text)
            return InjectResult("fake", "ctrl+v", True)

    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: SlowTranscriber())
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray())
    d.build()
    injector = RecordingInjector()
    d.dictation.set_injector(injector)
    d.history.add(Entry("hello world", time.time(), "fake", 1.0, 0.1))

    try:
        d._start_warmup()
        assert blocked.wait(2)                       # the model load is in flight and stuck
        d.dictation.recall()                         # queued on the shared pipeline pool
        deadline = time.monotonic() + 2
        while not injector.texts and time.monotonic() < deadline:
            time.sleep(0.01)
        assert injector.texts == ["hello world"]     # completed while warmup is still blocked
        assert not release.is_set()
    finally:
        release.set()
    d.shutdown()


def test_open_settings_refreshes_the_reused_dialog(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.list_sources", lambda: [])
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray())
    d.build()

    d.open_settings()
    dialog = d._settings
    dialog.hotkey_edit.setText("KEY_RIGHTCTRL")     # edited, then abandoned
    dialog.close()

    d.open_settings()
    assert d._settings is dialog                    # same dialog instance
    assert dialog.hotkey_edit.text() == "KEY_F13"   # showing what is actually in force
    assert d.config.get("hotkeys.dictate") == "KEY_F13"
    dialog.close()
    d.shutdown()


def test_open_settings_does_not_reset_a_visible_dialog(isolated_xdg, qapp, monkeypatch):
    """A second `voice settings` (or tray click) on an open dialog must raise the
    user's half-finished edits, not throw them away."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.list_sources", lambda: [])
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray())
    d.build()

    d.open_settings()
    dialog = d._settings
    assert dialog.isVisible()
    dialog.hotkey_edit.setText("KEY_RIGHTCTRL")

    d.open_settings()
    assert dialog.hotkey_edit.text() == "KEY_RIGHTCTRL"
    dialog.close()
    d.shutdown()
