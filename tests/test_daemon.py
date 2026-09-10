import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal

from voice.config import Config
from voice.daemon import Daemon, hotkey_specs
from voice.hotkey.portal_listener import STATE_BOUND, STATE_DENIED, STATE_UNASSIGNED
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


class QuietNotifier:
    """Records notifications instead of spawning notify-send.

    Every Daemon in this file gets one: the real Notifier reaches the owner's
    actual desktop, and a suite that pops notifications is a suite nobody can
    run while working.
    """

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def notify(self, title, body, urgency="normal"):
        self.sent.append((title, body, urgency))

    def set_enabled(self, enabled):
        pass


class FakeSender:
    name = "fake"
    def send_chord(self, codes): pass
    def available(self): return True


class FakeTray(QObject):
    """Records set_profiles()/set_languages() without a real QMenu/QAction."""

    state_changed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.languages = []
        self.profile_hints = []

    def set_profiles(self, names, active):
        self.calls.append((list(names), active))

    def set_languages(self, codes, active):
        self.languages.append((list(codes), active))

    def set_profile_hint(self, text):
        self.profile_hints.append(text)

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
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), notifier=QuietNotifier())
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
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), notifier=QuietNotifier())
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
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=tray, notifier=QuietNotifier())
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
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=tray, notifier=QuietNotifier())
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
    d = Daemon(cfg, listener=FailingStopListener(), sender=FakeSender(), notifier=QuietNotifier())
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
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), notifier=QuietNotifier())
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
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), notifier=QuietNotifier())

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
    d = Daemon(Config.load(), listener=listener, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
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
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
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
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
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
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()

    d.open_settings()
    dialog = d._settings
    assert dialog.isVisible()
    dialog.hotkey_edit.setText("KEY_RIGHTCTRL")

    d.open_settings()
    assert dialog.hotkey_edit.text() == "KEY_RIGHTCTRL"
    dialog.close()
    d.shutdown()


# -- hotkey backend selection --------------------------------------------------
@pytest.mark.parametrize("setting,keyboards,seat,expected", [
    ("auto", True, True, "evdev"),
    ("auto", True, False, "portal"),      # remote session: /dev/input sees nothing
    ("auto", False, True, "portal"),      # no udev rule, no input group
    ("auto", False, False, "portal"),
    ("evdev", True, True, "evdev"),
    ("evdev", False, False, "evdev"),     # forced: the user gets what they asked for
    ("portal", True, True, "portal"),
    ("portal", False, False, "portal"),
])
def test_choose_hotkey_backend_covers_every_combination(isolated_xdg, setting, keyboards, seat, expected):
    from voice.daemon import choose_hotkey_backend
    cfg = Config.load()
    cfg.set("hotkeys.backend", setting)
    assert choose_hotkey_backend(cfg, keyboards, seat) == expected


def test_an_unusable_backend_setting_behaves_like_auto(isolated_xdg):
    from voice.daemon import choose_hotkey_backend
    cfg = Config.load()
    cfg.set("hotkeys.backend", "telepathy")
    assert choose_hotkey_backend(cfg, True, True) == "evdev"
    assert choose_hotkey_backend(cfg, False, True) == "portal"


def test_portal_shortcuts_only_include_configured_triggers(isolated_xdg):
    from voice.daemon import portal_shortcuts
    cfg = Config.load()
    assert portal_shortcuts(cfg) == {"dictate": "CTRL+space"}
    cfg.set("hotkeys.portal_recall", "CTRL+ALT+r")
    cfg.set("hotkeys.portal_cancel", "   ")
    assert portal_shortcuts(cfg) == {"dictate": "CTRL+space", "recall": "CTRL+ALT+r"}


@pytest.mark.parametrize("seat_output,session_id,seat_env,expected", [
    ("Seat=seat0\n", "3", None, True),
    ("Seat=\n", "3", None, False),                 # the VM's remote session
    (None, None, "seat0", True),                   # no loginctl answer, env knows
    (None, None, "", False),
    (None, None, None, True),                      # nothing known: assume local
])
def test_has_local_seat_reads_loginctl_then_the_environment(monkeypatch, seat_output, session_id, seat_env, expected):
    import subprocess

    from voice.daemon import has_local_seat

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["loginctl", "show-session"]
        if seat_output is None:
            raise FileNotFoundError("loginctl")
        return subprocess.CompletedProcess(cmd, 0, seat_output, "")

    monkeypatch.setattr("voice.daemon.subprocess.run", fake_run)
    monkeypatch.delenv("XDG_SESSION_ID", raising=False)
    monkeypatch.delenv("XDG_SEAT", raising=False)
    if session_id is not None:
        monkeypatch.setenv("XDG_SESSION_ID", session_id)
    if seat_env is not None:
        monkeypatch.setenv("XDG_SEAT", seat_env)
    assert has_local_seat() is expected


def test_build_wires_the_portal_listener_and_reports_it_in_status(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    made = {}

    class FakePortalListener(FakeListener):
        def __init__(self, on_event, shortcuts, **kwargs):
            super().__init__()
            made["on_event"], made["shortcuts"] = on_event, shortcuts

    monkeypatch.setattr("voice.daemon.PortalListener", FakePortalListener)
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    cfg.set("hotkeys.portal_recall", "CTRL+ALT+r")
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    assert isinstance(d.listener, FakePortalListener)
    assert made["shortcuts"] == {"dictate": "CTRL+space", "recall": "CTRL+ALT+r"}
    st = d.handle({"cmd": "status"})
    assert st["hotkey_backend"] == "portal"
    made["on_event"]("dictate", "press")                  # the events still reach the pipeline
    assert d.dictation.state.value != "idle"
    d.shutdown()


def test_build_uses_the_evdev_listener_when_configured(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.EvdevListener", lambda tracker, on_event: FakeListener())
    monkeypatch.setattr("voice.daemon.PortalListener", lambda *a, **k: pytest.fail("must not be built"))
    cfg = Config.load()
    cfg.set("hotkeys.backend", "evdev")
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    assert d.handle({"cmd": "status"})["hotkey_backend"] == "evdev"
    d.shutdown()


def test_portal_shortcuts_falls_back_when_the_config_predates_the_feature(isolated_xdg):
    from voice import paths
    from voice.daemon import portal_shortcuts
    paths.config_file().write_text('[hotkeys]\ndictate = "KEY_F13"\n')
    assert portal_shortcuts(Config.load()) == {"dictate": "CTRL+space"}


def test_a_portal_denial_notifies_even_though_binding_finishes_after_start(isolated_xdg, qapp, monkeypatch):
    """The portal answers on its own thread, so run()'s devices_ok() check is too
    early for it; the listener reports the outcome through on_ready instead."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    captured = {}

    class FakePortalListener(FakeListener):
        def __init__(self, on_event, shortcuts, **kwargs):
            super().__init__()
            captured["on_ready"] = kwargs.get("on_ready")

        def devices_ok(self):
            return None                                      # still waiting on the dialog

    monkeypatch.setattr("voice.daemon.PortalListener", FakePortalListener)
    notified = []
    notifier = type("N", (), {
        "notify": lambda self, title, body, urgency="normal": notified.append((title, urgency)),
        "set_enabled": lambda self, enabled: None})()
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=notifier)
    d.build()
    assert callable(captured["on_ready"])
    captured["on_ready"](STATE_BOUND)
    assert notified == []                                    # a working binding says nothing
    captured["on_ready"](STATE_DENIED)
    assert notified and notified[-1][1] == "critical"
    d.shutdown()


def _reporting_portal_daemon(monkeypatch, notifier, state=None, triggers=None, captured=None):
    """A built portal-backed daemon whose listener reports `state`/`triggers`."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    box = captured if captured is not None else {}

    class FakePortalListener(FakeListener):
        def __init__(self, on_event, shortcuts, **kwargs):
            super().__init__()
            box["on_ready"] = kwargs.get("on_ready")

        def devices_ok(self):
            return None if state is None else state == STATE_BOUND

        def shortcut_state(self):
            return state

        def effective_triggers(self):
            return dict(triggers or {})

    monkeypatch.setattr("voice.daemon.PortalListener", FakePortalListener)
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=notifier)
    d.build()
    return d


def test_a_shortcut_registered_without_a_key_is_reported_once(isolated_xdg, qapp, monkeypatch):
    """Success with no trigger attached is the silent failure this whole change
    is about: the user must be told, and told once, not on every reload."""
    notified = []
    notifier = type("N", (), {
        "notify": lambda self, title, body, urgency="normal": notified.append((title, body, urgency)),
        "set_enabled": lambda self, enabled: None})()
    captured = {}
    d = _reporting_portal_daemon(monkeypatch, notifier, state=STATE_UNASSIGNED,
                       triggers={"dictate": ""}, captured=captured)
    try:
        captured["on_ready"](STATE_UNASSIGNED)
        assert len(notified) == 1
        title, body, urgency = notified[0]
        assert "not assigned" in title.lower()
        assert "Keyboard Settings" in body and "voice" in body
        assert urgency == "critical"
        captured["on_ready"](STATE_UNASSIGNED)               # a reload rebinds; stay quiet
        assert len(notified) == 1
    finally:
        d.shutdown()


def test_status_carries_the_effective_portal_trigger_per_shortcut(isolated_xdg, qapp, monkeypatch):
    d = _reporting_portal_daemon(monkeypatch, QuietNotifier(), state=STATE_UNASSIGNED,
                       triggers={"dictate": "", "recall": "F14"})
    try:
        st = d.handle({"cmd": "status"})
        assert st["shortcut_state"] == STATE_UNASSIGNED
        assert st["shortcut_triggers"] == {"dictate": "", "recall": "F14"}
        assert st["keyboard"] is False                       # registered is not bound
    finally:
        d.shutdown()


def test_status_says_nothing_about_shortcuts_on_the_evdev_backend(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.EvdevListener", lambda tracker, on_event: FakeListener())
    cfg = Config.load()
    cfg.set("hotkeys.backend", "evdev")
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    try:
        st = d.handle({"cmd": "status"})
        assert "shortcut_state" not in st and "shortcut_triggers" not in st
    finally:
        d.shutdown()


# -- recording overlay ---------------------------------------------------------
def _overlay_daemon(cfg, monkeypatch, helper_processes, **kwargs):
    """A built daemon whose overlay helper is a FakeHelperProcess."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.default_launcher", lambda **kw: helper_processes())
    kwargs.setdefault("notifier", QuietNotifier())      # never the real notify-send
    fakes = dict(listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), **kwargs)
    d = Daemon(cfg, notifier=fakes.pop("notifier"), **fakes)
    d.build()
    d.overlay.start()
    return d


def _overlay_lines(d, helper_processes):
    assert d.overlay.flush(2.0)
    return helper_processes.made[0].lines()


def test_a_successful_dictation_drives_the_pill_through_its_states(isolated_xdg, qapp, monkeypatch,
                                                                   helper_processes):
    from voice.pipeline import State

    cfg = Config.load()
    cfg.set("general.language", "sv")
    d = _overlay_daemon(cfg, monkeypatch, helper_processes)
    d.dictation.on_state(State.RECORDING, "")
    d.dictation.on_state(State.TRANSCRIBING, "")
    d.dictation.on_state(State.INJECTING, "")          # says nothing on its own
    d.dictation.on_state(State.IDLE, "11 chars via portal in 0.9s")
    assert _overlay_lines(d, helper_processes) == [
        {"language": "sv"}, {"state": "recording"}, {"state": "transcribing"},
        {"state": "done"}]                             # exactly one checkmark
    d.shutdown()


def test_the_checkmark_is_sent_once_per_dictation(isolated_xdg, qapp, monkeypatch,
                                                  helper_processes):
    """INJECTING and the IDLE that follows it are one insertion, not two: a
    second `done` used to restart the checkmark's hold every time."""
    from voice.daemon import overlay_messages
    from voice.pipeline import State

    assert overlay_messages(State.INJECTING, "", "en") == []
    assert overlay_messages(State.INJECTING, "recall", "en") == []
    assert overlay_messages(State.IDLE, "7 chars via portal in 0.4s", "en") == [{"state": "done"}]


def test_a_failed_dictation_shows_the_error_and_does_not_hide_it(isolated_xdg, qapp, monkeypatch,
                                                                 helper_processes):
    from voice.pipeline import State

    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    d.dictation.on_state(State.RECORDING, "")
    d.dictation.on_state(State.TRANSCRIBING, "")
    d.dictation.on_state(State.ERROR, "cannot record: pw-record died")
    d.dictation.on_state(State.IDLE, "after error")        # never hides the message
    assert _overlay_lines(d, helper_processes) == [
        {"language": "en"}, {"state": "recording"}, {"state": "transcribing"},
        {"state": "error", "text": "cannot record: pw-record died"}]
    d.shutdown()


@pytest.mark.parametrize("detail", ["cancelled", "too short", "empty"])
def test_a_dictation_that_produced_nothing_hides_the_pill(isolated_xdg, qapp, monkeypatch,
                                                          helper_processes, detail):
    from voice.pipeline import State

    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    d.dictation.on_state(State.RECORDING, "")
    d.dictation.on_state(State.IDLE, detail)
    assert _overlay_lines(d, helper_processes)[-1] == {"state": "hidden"}
    d.shutdown()


def test_the_tray_still_sees_every_state_alongside_the_overlay(isolated_xdg, qapp, monkeypatch,
                                                               helper_processes):
    from voice.pipeline import State

    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    seen = []
    d.tray.state_changed.connect(lambda s, det: seen.append((s, det)))
    d.dictation.on_state(State.RECORDING, "")
    d.dictation.on_state(State.ERROR, "boom")
    qapp.processEvents()
    assert seen == [("recording", ""), ("error", "boom")]
    d.shutdown()


def test_microphone_levels_reach_the_helper(isolated_xdg, qapp, monkeypatch, helper_processes):
    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    assert d._recorder._on_level == d._on_level      # Recorder(on_level=...) is wired
    d._on_level(0.42)
    assert _overlay_lines(d, helper_processes) == [{"level": 0.42}]
    d.shutdown()


def test_no_helper_is_launched_when_the_overlay_is_switched_off(isolated_xdg, qapp, monkeypatch,
                                                                helper_processes):
    cfg = Config.load()
    cfg.set("ui.overlay", False)
    d = _overlay_daemon(cfg, monkeypatch, helper_processes)
    from voice.pipeline import State
    d.dictation.on_state(State.RECORDING, "")
    d._on_level(0.4)
    assert helper_processes.made == []
    assert d.overlay.enabled is False
    d.shutdown()


# -- language switch -----------------------------------------------------------
def test_language_command_validates_persists_and_tells_the_pill(isolated_xdg, qapp, monkeypatch,
                                                                helper_processes):
    cfg = Config.load()
    d = _overlay_daemon(cfg, monkeypatch, helper_processes)
    assert d.handle({"cmd": "status"})["language"] == "en"

    reply = {}
    worker = threading.Thread(target=lambda: reply.update(
        d.handle({"cmd": "language", "code": "sv"})))       # as the IPC server calls it
    worker.start()
    worker.join(timeout=2)
    assert reply == {"ok": True, "language": "sv"}
    assert Config.load().get("general.language") == "en"      # validated, not yet written
    qapp.processEvents()
    assert Config.load().get("general.language") == "sv"      # persisted on the Qt thread
    assert d.handle({"cmd": "status"})["language"] == "sv"
    assert _overlay_lines(d, helper_processes)[-2:] == [
        {"language": "sv"}, {"state": "notice", "text": "EN → SV"}]
    assert d.tray.languages[-1] == (["en", "sv"], "sv")

    bad = d.handle({"cmd": "language", "code": "svenska"})
    assert bad["ok"] is False and "svenska" in bad["error"]
    assert d.handle({"cmd": "language"})["ok"] is False
    qapp.processEvents()
    assert Config.load().get("general.language") == "sv"      # nothing was written
    d.shutdown()


def test_language_next_wraps_around_the_configured_cycle(isolated_xdg, qapp, monkeypatch,
                                                         helper_processes):
    cfg = Config.load()
    cfg.set("general.languages", ["en", "sv", "auto"])
    cfg.save()                    # the daemon re-reads the file before it writes
    d = _overlay_daemon(cfg, monkeypatch, helper_processes)
    for expected in ("sv", "auto", "en", "sv"):
        # "next" is resolved on the Qt thread, so the reply cannot name it yet.
        assert d.handle({"cmd": "language", "code": "next"}) == {"ok": True, "language": "pending"}
        qapp.processEvents()
        assert d.config.get("general.language") == expected
    d.shutdown()


def test_a_language_outside_the_cycle_is_still_accepted_when_it_is_a_real_code(
        isolated_xdg, qapp, monkeypatch, helper_processes):
    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    assert d.handle({"cmd": "language", "code": "de"})["ok"] is True
    qapp.processEvents()
    assert d.config.get("general.language") == "de"
    d.shutdown()


def test_the_tray_language_entries_route_to_the_language_command(isolated_xdg, qapp, monkeypatch,
                                                                 helper_processes):
    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    d._on_tray_action("language:sv")
    qapp.processEvents()
    assert d.config.get("general.language") == "sv"
    d.shutdown()


def test_the_language_toggle_hotkey_cycles_without_touching_the_pipeline(
        isolated_xdg, qapp, monkeypatch, helper_processes):
    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    d._on_hotkey("language_toggle", "press")
    qapp.processEvents()
    assert d.config.get("general.language") == "sv"
    assert Config.load().get("general.language") == "sv"   # and it reached the file
    assert d.dictation.state.value == "idle"          # it is not a dictation key
    d._on_hotkey("language_toggle", "release")        # release does nothing
    qapp.processEvents()
    assert d.config.get("general.language") == "sv"
    assert Config.load().get("general.language") == "sv"
    d.shutdown()


def test_the_language_toggle_is_bound_in_both_listeners(isolated_xdg):
    from voice.daemon import hotkey_specs, portal_shortcuts
    cfg = Config.load()
    cfg.set("hotkeys.language_toggle", "KEY_F15")
    cfg.set("hotkeys.portal_language_toggle", "CTRL+ALT+l")
    assert hotkey_specs(cfg)["language_toggle"] == parse_keyspec("KEY_F15")
    assert portal_shortcuts(cfg)["language_toggle"] == "CTRL+ALT+l"


def test_status_reports_what_the_pill_is_doing(isolated_xdg, qapp, monkeypatch, helper_processes):
    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    assert d.handle({"cmd": "status"})["overlay"] == "running"
    helper_processes.made[0].exit(2)                      # the helper refused to steal focus
    assert d.handle({"cmd": "status"})["overlay"] == "disabled: no layer-shell"
    assert len(helper_processes.made) == 1                # and was not restarted
    d.shutdown()


def test_the_fallback_window_flag_reaches_the_launcher(isolated_xdg, qapp, monkeypatch,
                                                       helper_processes):
    seen = []
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.default_launcher",
                        lambda **kw: seen.append(kw) or helper_processes())
    cfg = Config.load()
    cfg.set("ui.overlay_allow_fallback", True)
    cfg.set("ui.overlay_position", "top")
    cfg.set("general.language", "sv")
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    d.overlay.start()
    assert seen == [{"position": "top", "lang": "sv", "verbose": False, "allow_fallback": True}]
    d.shutdown()


def test_switching_to_the_language_already_in_force_does_nothing(isolated_xdg, qapp, monkeypatch,
                                                                helper_processes):
    """No save, no reload, and above all no "EN → EN" flashing on the pill."""
    cfg = Config.load()
    d = _overlay_daemon(cfg, monkeypatch, helper_processes)
    saved = []
    monkeypatch.setattr(cfg, "save", lambda: saved.append(1))
    applied = len(d.tray.languages)

    assert d.handle({"cmd": "language", "code": "en"}) == {"ok": True, "language": "en"}
    qapp.processEvents()
    assert saved == []
    assert len(d.tray.languages) == applied           # apply_config never ran
    assert _overlay_lines(d, helper_processes) == []  # no badge, no notice
    assert d.config.get("general.language") == "en"
    d.shutdown()


def test_a_cycle_of_one_language_makes_the_toggle_a_no_op(isolated_xdg, qapp, monkeypatch,
                                                          helper_processes):
    cfg = Config.load()
    cfg.set("general.languages", ["en"])
    cfg.save()
    d = _overlay_daemon(cfg, monkeypatch, helper_processes)
    assert d._next_language() is None                 # nowhere to go

    d._on_hotkey("language_toggle", "press")
    qapp.processEvents()
    assert d.config.get("general.language") == "en"
    assert _overlay_lines(d, helper_processes) == []
    d.shutdown()


def test_an_invalid_language_in_the_cycle_is_never_written(isolated_xdg, qapp, monkeypatch,
                                                          helper_processes):
    """`next` is resolved on the Qt thread, past the IPC check that rejects a
    typo, and Config.load() does not validate the cycle: a hand-edited
    general.languages used to put "zz9" straight into general.language."""
    cfg = Config.load()
    cfg.set("general.languages", ["en", "zz9"])
    cfg.save()
    notifier = QuietNotifier()
    d = _overlay_daemon(cfg, monkeypatch, helper_processes, notifier=notifier)

    d._on_hotkey("language_toggle", "press")
    qapp.processEvents()

    assert d.config.get("general.language") == "en"
    assert Config.load().get("general.language") == "en"    # and nothing reached the file
    assert _overlay_lines(d, helper_processes) == []        # no badge, no "EN → ZZ9"
    assert [title for title, _, _ in notifier.sent] == ["Unknown language in the cycle"]
    assert "zz9" in notifier.sent[0][1]
    d.shutdown()


def test_two_toggles_before_the_qt_thread_drains_are_two_steps(isolated_xdg, qapp, monkeypatch,
                                                               helper_processes):
    """`next` used to be resolved on the caller's thread, so a double tap
    resolved twice from the same current language and lost a step."""
    cfg = Config.load()
    cfg.set("general.languages", ["en", "sv", "auto"])
    cfg.save()
    d = _overlay_daemon(cfg, monkeypatch, helper_processes)

    replies = []

    def worker():
        replies.append(d.handle({"cmd": "language", "code": "next"}))
        replies.append(d.handle({"cmd": "language", "code": "next"}))

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2)
    assert replies == [{"ok": True, "language": "pending"}] * 2
    assert d.config.get("general.language") == "en"    # nothing applied yet

    qapp.processEvents()
    assert d.config.get("general.language") == "auto"  # two presses, two steps
    assert Config.load().get("general.language") == "auto"
    d.shutdown()


def test_a_language_changed_outside_a_recording_still_reaches_the_pill(
        isolated_xdg, qapp, monkeypatch, helper_processes):
    """The settings dialog writes the file and the daemon reloads it. The badge
    was only ever sent with `recording`, so a retry or a recall drew the old one."""
    from voice.pipeline import State

    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    external = Config.load()
    external.set("general.language", "sv")
    external.save()

    d.apply_config()                                   # what the dialog's `saved` does
    d.dictation.on_state(State.TRANSCRIBING, "retry")
    assert _overlay_lines(d, helper_processes) == [
        {"language": "sv"}, {"state": "transcribing"}]
    d.shutdown()


def test_the_recall_path_shows_the_current_language_too(isolated_xdg, qapp, monkeypatch,
                                                        helper_processes):
    from voice.pipeline import State

    cfg = Config.load()
    d = _overlay_daemon(cfg, monkeypatch, helper_processes)
    cfg.set("general.language", "sv")
    cfg.save()
    d.apply_config()
    d.dictation.on_state(State.INJECTING, "recall")
    d.dictation.on_state(State.IDLE, "9 chars via portal in 0.0s")
    assert _overlay_lines(d, helper_processes) == [{"language": "sv"}, {"state": "done"}]
    d.shutdown()


def test_the_language_is_not_repeated_while_it_stays_the_same(isolated_xdg, qapp, monkeypatch,
                                                              helper_processes):
    """The helper is started with --lang, so a badge it already shows is noise."""
    from voice.pipeline import State

    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    d.apply_config()
    d.dictation.on_state(State.TRANSCRIBING, "retry")
    d.dictation.on_state(State.IDLE, "4 chars via portal in 0.1s")
    assert _overlay_lines(d, helper_processes) == [
        {"state": "transcribing"}, {"state": "done"}]
    d.shutdown()


def test_reload_rebuilds_the_pill_only_when_the_ui_section_changed(
        isolated_xdg, qapp, monkeypatch, helper_processes):
    """[ui] is baked into the helper's command line, so `voice reload` used to
    apply everything except the pill's own settings."""
    seen = []
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.default_launcher",
                        lambda **kw: seen.append(kw) or helper_processes())
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    d.overlay.start()
    first = d.overlay
    assert seen[-1]["position"] == "bottom"

    d.apply_config()
    assert d.overlay is first                       # unchanged: the pill stays up
    assert len(helper_processes.made) == 1

    external = Config.load()
    external.set("ui.overlay_position", "top")
    external.save()
    d.apply_config()
    assert d.overlay is not first
    assert seen[-1]["position"] == "top"            # the new helper knows
    assert len(helper_processes.made) == 2
    assert first.status() == "stopped"              # and the old one was stopped
    d.shutdown()


def test_switching_the_pill_off_by_reload_stops_the_helper(isolated_xdg, qapp, monkeypatch,
                                                           helper_processes):
    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    external = Config.load()
    external.set("ui.overlay", False)
    external.save()
    d.apply_config()
    assert d.overlay.enabled is False
    assert len(helper_processes.made) == 1          # no second helper was started
    d.shutdown()


# -- quitting ------------------------------------------------------------------
def _live_threads() -> list[str]:
    """Non-daemon threads: the ones that keep the interpreter alive at exit."""
    return sorted(t.name for t in threading.enumerate()
                  if not t.daemon and t is not threading.main_thread() and t.is_alive())


def test_quit_returns_from_run_and_leaves_nothing_holding_the_process(
        isolated_xdg, qapp, monkeypatch, helper_processes):
    """`voice quit` unlinked the socket - so a second daemon could start - and
    then the process stayed alive: the dictation pool's worker is not a daemon
    thread, so the interpreter waits for it at exit."""
    import time

    from PySide6.QtCore import QTimer

    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.default_launcher", lambda **kw: helper_processes())
    monkeypatch.setattr("voice.daemon.is_running", lambda: False)
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())

    blocked, release = threading.Event(), threading.Event()

    def stuck():
        blocked.set()
        release.wait(10)

    def quit_it():
        d.dictation._executor(stuck)             # a job still running when we quit
        assert blocked.wait(2)
        assert d.handle({"cmd": "quit"}) == {"ok": True}

    QTimer.singleShot(20, quit_it)
    QTimer.singleShot(5000, qapp.quit)           # so a broken quit fails, not hangs
    started = time.monotonic()
    assert d.run() == 0
    assert time.monotonic() - started < 4.0, "app.exec() did not return on quit"

    # Even with that job still in flight: nothing the daemon starts may keep
    # the interpreter alive once the socket is gone.
    assert _live_threads() == []
    release.set()


# -- writing the config without clobbering it ----------------------------------
def test_a_profile_switch_keeps_edits_made_to_the_file_meanwhile(isolated_xdg, qapp, monkeypatch):
    """The daemon holds a whole-document snapshot, so set+save wrote back
    everything - reverting a hand edit, or the settings dialog's own save."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()

    external = Config.load()
    external.set("stt.profiles.openai.model", "gpt-4o-mini-transcribe")
    external.set("audio.max_seconds", 42)
    external.save()

    d.handle({"cmd": "profile", "name": "openai"})
    qapp.processEvents()
    again = Config.load()
    assert again.get("stt.active") == "openai"                          # the switch landed
    assert again.get("stt.profiles.openai.model") == "gpt-4o-mini-transcribe"
    assert again.get("audio.max_seconds") == 42                         # and so did the edit
    d.shutdown()


def test_a_language_switch_keeps_edits_made_to_the_file_meanwhile(isolated_xdg, qapp, monkeypatch,
                                                                  helper_processes):
    d = _overlay_daemon(Config.load(), monkeypatch, helper_processes)
    external = Config.load()
    external.set("audio.max_seconds", 42)
    external.save()

    d.handle({"cmd": "language", "code": "sv"})
    qapp.processEvents()
    again = Config.load()
    assert again.get("general.language") == "sv"
    assert again.get("audio.max_seconds") == 42
    d.shutdown()


def test_a_profile_that_vanished_from_the_file_is_not_written(isolated_xdg, qapp, monkeypatch):
    """Validated on the IPC thread against the old document; by the time the Qt
    thread applies it the file may no longer define it, and writing it would
    leave a config the daemon itself rejects."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()

    from voice import paths
    text = paths.config_file().read_text().replace("[stt.profiles.openai]", "[stt.profiles.gone]")
    paths.config_file().write_text(text)

    d.handle({"cmd": "profile", "name": "openai"})
    qapp.processEvents()
    again = Config.load()
    assert again.get("stt.active") == "local"        # unchanged
    assert again.errors() == []
    d.shutdown()


# -- rebinding the portal shortcuts on save ------------------------------------
class FakePortalListener(FakeListener):
    """Records the shortcuts it was built with, like the real one binds them."""

    made: list = []

    def __init__(self, on_event, shortcuts, **kwargs):
        super().__init__()
        self.shortcuts = dict(shortcuts)
        FakePortalListener.made.append(self)


def _portal_daemon(monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    FakePortalListener.made = []
    monkeypatch.setattr("voice.daemon.PortalListener", FakePortalListener)
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    cfg.save()
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    d.listener.start()                                  # what run() does
    return d


def test_a_changed_portal_trigger_rebinds_on_reload(isolated_xdg, qapp, monkeypatch):
    """The portal binds once, when the session is created, so a trigger changed
    in the settings window used to need a daemon restart."""
    d = _portal_daemon(monkeypatch)
    first = d.listener
    assert len(FakePortalListener.made) == 1

    d.apply_config()
    assert d.listener is first                          # unchanged: no second dialog
    assert len(FakePortalListener.made) == 1

    external = Config.load()
    external.set("hotkeys.portal_dictate", "CTRL+ALT+d")
    external.set("hotkeys.portal_language_toggle", "CTRL+SHIFT+l")
    external.save()
    d.apply_config()

    assert len(FakePortalListener.made) == 2
    assert d.listener is not first
    assert d.listener.shortcuts == {"dictate": "CTRL+ALT+d", "language_toggle": "CTRL+SHIFT+l"}
    assert first.started is False                       # the old session was closed
    assert d.listener.started is True                   # and the new one is listening
    assert d.injector._modifiers_held == d.listener.modifiers_held  # the paste guard follows
    d.shutdown()


def test_a_changed_hotkey_backend_rebuilds_the_listener(isolated_xdg, qapp, monkeypatch):
    d = _portal_daemon(monkeypatch)
    monkeypatch.setattr("voice.daemon.EvdevListener", lambda tracker, on_event: FakeListener())
    external = Config.load()
    external.set("hotkeys.backend", "evdev")
    external.save()
    d.apply_config()
    assert d.hotkey_backend == "evdev"
    assert not isinstance(d.listener, FakePortalListener)
    assert d.handle({"cmd": "status"})["hotkey_backend"] == "evdev"
    d.shutdown()


class FakeEvdevListener(FakeListener):
    """Records every construction, so a needless rebuild is visible."""

    made: list = []

    def __init__(self, tracker, on_event):
        super().__init__()
        FakeEvdevListener.made.append(self)


def _evdev_daemon(monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    FakeEvdevListener.made = []
    monkeypatch.setattr("voice.daemon.EvdevListener", FakeEvdevListener)
    cfg = Config.load()
    cfg.set("hotkeys.backend", "evdev")
    cfg.save()
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    d.listener.start()
    return d


def test_a_portal_trigger_does_not_rebuild_the_evdev_listener(isolated_xdg, qapp, monkeypatch):
    """The snapshot carried the portal triggers whatever the backend was, so
    editing one tore down and rebuilt an evdev listener that never reads them -
    dropping the /dev/input file descriptors for nothing."""
    d = _evdev_daemon(monkeypatch)
    first = d.listener
    assert len(FakeEvdevListener.made) == 1

    external = Config.load()
    external.set("hotkeys.portal_dictate", "CTRL+ALT+d")
    external.set("hotkeys.portal_language_toggle", "CTRL+SHIFT+l")
    external.save()
    d.apply_config()

    assert len(FakeEvdevListener.made) == 1
    assert d.listener is first and first.started is True
    d.shutdown()


def test_a_listener_that_cannot_be_rebuilt_says_so_and_leaves_the_daemon_running(
        isolated_xdg, qapp, monkeypatch):
    """The old session is already closed when the new listener is built. A
    failure there used to propagate out of apply_config, leaving the stopped
    listener in place: no hotkeys, nothing said, and the rest of the reload
    (transcriber, injector, tray) never applied."""
    d = _portal_daemon(monkeypatch)
    first = d.listener

    def boom(*args, **kwargs):
        raise RuntimeError("portal said no")

    monkeypatch.setattr("voice.daemon.PortalListener", boom)
    external = Config.load()
    external.set("hotkeys.portal_dictate", "CTRL+ALT+d")
    external.set("general.language", "sv")
    external.save()

    d.apply_config()                                    # must not raise

    title, body, urgency = d._notifier.sent[-1]
    assert title == "Hotkeys are off"
    assert "portal said no" in body and urgency == "critical"
    assert d.listener is first                          # still an object, not a crater
    assert d.handle({"cmd": "status"})["ok"] is True    # and the daemon still answers
    assert d.config.get("general.language") == "sv"     # the rest of the reload applied
    d.shutdown()


# -- a profile per language -----------------------------------------------------
def _profile_daemon(cfg, monkeypatch, **kwargs):
    """A built daemon plus the list of models its transcribers were built from."""
    built: list[str] = []

    def fake_make_transcriber(profile, secret):
        built.append(str(profile.get("model")))
        return type("T", (), {"name": profile["backend"],
                              "describe": lambda self: f"fake {profile['model']}",
                              "warmup": lambda self: None,
                              "transcribe": lambda self, *a: None})()

    monkeypatch.setattr("voice.daemon.make_transcriber", fake_make_transcriber)
    kwargs.setdefault("notifier", QuietNotifier())
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), **kwargs)
    d.build()
    return d, built


def _with_language_profiles(mapping: dict) -> Config:
    cfg = Config.load()
    cfg.set("general.language_profiles", mapping)
    cfg.save()
    return Config.load()


def test_switching_language_activates_the_mapped_profile(isolated_xdg, qapp, monkeypatch):
    """One reload, one save, one apply: the language and its model move together."""
    cfg = _with_language_profiles({"en": "local", "sv": "openai"})
    d, built = _profile_daemon(cfg, monkeypatch)
    assert built == ["large-v3-turbo"]

    d.handle({"cmd": "language", "code": "sv"})
    qapp.processEvents()

    on_disk = Config.load()
    assert on_disk.get("general.language") == "sv"
    assert on_disk.get("stt.active") == "openai"        # switched in the same save
    assert on_disk.errors() == []
    assert built == ["large-v3-turbo", "gpt-transcribe"]   # rebuilt exactly once
    assert d.handle({"cmd": "status"})["profile"] == "openai"
    d.shutdown()


def test_the_language_toggle_hotkey_also_moves_the_profile(isolated_xdg, qapp, monkeypatch):
    cfg = _with_language_profiles({"en": "local", "sv": "openai"})
    d, built = _profile_daemon(cfg, monkeypatch)
    d._on_hotkey("language_toggle", "press")
    qapp.processEvents()
    on_disk = Config.load()
    assert (on_disk.get("general.language"), on_disk.get("stt.active")) == ("sv", "openai")
    d._on_tray_action("language:en")                    # and back, from the tray
    qapp.processEvents()
    on_disk = Config.load()
    assert (on_disk.get("general.language"), on_disk.get("stt.active")) == ("en", "local")
    assert built == ["large-v3-turbo", "gpt-transcribe", "large-v3-turbo"]
    d.shutdown()


def test_a_language_that_maps_to_the_active_profile_changes_nothing(isolated_xdg, qapp, monkeypatch):
    cfg = _with_language_profiles({"en": "local", "sv": "local"})
    d, built = _profile_daemon(cfg, monkeypatch)
    d.handle({"cmd": "language", "code": "sv"})
    qapp.processEvents()
    assert Config.load().get("stt.active") == "local"
    assert built == ["large-v3-turbo"]        # nothing to rebuild
    d.shutdown()


def test_a_missing_mapped_profile_notifies_and_keeps_the_current_one(isolated_xdg, qapp, monkeypatch):
    """The map can name a profile the owner has since deleted; the language still
    switches, the model does not, and they are told why exactly once."""
    cfg = _with_language_profiles({"sv": "local-swedish"})     # never defined here
    notifier = QuietNotifier()
    d, built = _profile_daemon(cfg, monkeypatch, notifier=notifier)

    d.handle({"cmd": "language", "code": "sv"})
    qapp.processEvents()

    on_disk = Config.load()
    assert on_disk.get("general.language") == "sv"      # the language switch stands
    assert on_disk.get("stt.active") == "local"         # the profile is untouched
    assert built == ["large-v3-turbo"]                  # so nothing was rebuilt
    assert len(notifier.sent) == 1
    title, body, urgency = notifier.sent[0]
    assert title == "Language profile missing"
    assert "'local-swedish'" in body and "sv" in body
    d.shutdown()


def test_a_manual_profile_switch_leaves_the_language_alone(isolated_xdg, qapp, monkeypatch):
    cfg = _with_language_profiles({"en": "local", "sv": "openai"})
    d, built = _profile_daemon(cfg, monkeypatch)
    d.handle({"cmd": "profile", "name": "groq"})
    qapp.processEvents()
    on_disk = Config.load()
    assert on_disk.get("stt.active") == "groq"
    assert on_disk.get("general.language") == "en"      # the map is not applied backwards
    d.shutdown()


def test_status_and_the_tray_say_which_language_chose_the_profile(isolated_xdg, qapp, monkeypatch):
    cfg = _with_language_profiles({"sv": "openai"})
    d, _ = _profile_daemon(cfg, monkeypatch)
    status = d.handle({"cmd": "status"})
    assert status["profile"] == "local" and status["profile_language"] is None
    assert d.tray.profile_hints[-1] == "local"

    d.handle({"cmd": "language", "code": "sv"})
    qapp.processEvents()
    status = d.handle({"cmd": "status"})
    assert status["profile"] == "openai" and status["profile_language"] == "sv"
    assert d.tray.profile_hints[-1] == "openai (for sv)"

    d.handle({"cmd": "profile", "name": "groq"})        # chosen by hand: no "for sv"
    qapp.processEvents()
    status = d.handle({"cmd": "status"})
    assert status["profile"] == "groq" and status["profile_language"] is None
    assert d.tray.profile_hints[-1] == "groq"
    d.shutdown()
