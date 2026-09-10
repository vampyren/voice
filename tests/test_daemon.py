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
    """`profile`/`reload` must save synchronously but defer apply_config() to the Qt thread
    (it touches Tray's QMenu/QAction), via _Bridge.apply_config."""
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
    assert Config.load().get("stt.active") == "openai"    # validated + saved synchronously
    assert tray.calls == []                                # apply_config not yet run on the Qt thread
    qapp.processEvents()
    assert tray.calls and tray.calls[-1][1] == "openai"    # now applied, on this (Qt) thread
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
