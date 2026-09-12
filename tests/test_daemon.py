import os
import threading
import time

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


def settle(qapp, predicate, timeout=5.0):
    """Pump the Qt event loop until `predicate()` is true, or give up.

    The daemon now does its slow reads on worker threads and hands the answers
    back through the bridge, so a test that wants the answer has to let Qt
    deliver it. Returns the predicate's last value so the caller can assert on
    it and see what it actually was.
    """
    deadline = time.monotonic() + timeout
    while True:
        qapp.processEvents()
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(0.005)


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


def _quiet_daemon(monkeypatch, **kwargs):
    """A built daemon with nothing in it that reaches the owner's machine."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(),
               tray=FakeTray(), notifier=QuietNotifier(), **kwargs)
    d.build()
    return d


def test_the_record_start_path_reads_the_cache_rather_than_running_pw_dump(
        isolated_xdg, qapp, monkeypatch):
    """`pw-dump` sat between the hotkey and `recorder.start()`, on the listener
    thread with the pipeline's lock held: latency before every single word, and
    a wedged PipeWire would have held that lock for the whole five-second bound,
    blocking stop(), cancel() and toggle() with it."""
    from voice.audio.capture import Source

    asked = []

    def slow_pw_dump(*args, **kwargs):
        asked.append(threading.current_thread())
        time.sleep(0.5)
        return [Source("mic", "Some Microphone", True)]

    monkeypatch.setattr("voice.daemon.capture_sources", slow_pw_dump)
    d = _quiet_daemon(monkeypatch)
    try:
        d._source_cache = [Source("mic", "Some Microphone", True)]
        started = time.perf_counter()
        answer = d.dictation.sv.sources()
        elapsed = time.perf_counter() - started
        assert elapsed < 0.05, f"the start path blocked for {elapsed:.3f} s"
        assert threading.current_thread() not in asked, \
            "pw-dump ran on the very thread that is about to start the recorder"
        assert [s.name for s in answer] == ["mic"], "the answer came from the cache"
    finally:
        d.shutdown()


def test_the_cache_still_reports_a_missing_microphone_and_an_unknown_one(
        isolated_xdg, qapp, monkeypatch):
    """The guard exists because pw-record "records" happily with no capture
    device. Reading a cache may not weaken it - and "nobody has asked yet" is
    not "no microphone", or a daemon would refuse to record before its first
    listing came back."""
    monkeypatch.setattr("voice.daemon.capture_sources", lambda *a, **k: [])
    d = _quiet_daemon(monkeypatch)
    try:
        d._source_cache = []
        assert d.dictation.sv.sources() == [], "an empty listing still means no microphone"
        d._source_cache = None
        assert d.dictation.sv.sources() is None, "not asked yet is not an answer"
    finally:
        d.shutdown()


def test_reading_the_cache_asks_for_a_fresh_listing_for_the_next_dictation(
        isolated_xdg, qapp, monkeypatch):
    """Off the start path, but not never: a microphone unplugged mid-session
    has to be noticed, and a dictation refused for want of one has to notice
    the moment it comes back."""
    from voice.audio.capture import Source

    monkeypatch.setattr("voice.daemon.capture_sources",
                        lambda *a, **k: [Source("mic", "Some Microphone", True)])
    d = _quiet_daemon(monkeypatch)
    try:
        d._source_cache = None
        d.dictation.sv.sources()
        assert settle(qapp, lambda: d._source_cache is not None), d._source_cache
        assert [s.name for s in d._source_cache] == ["mic"]
    finally:
        d.shutdown()


def test_open_settings_refreshes_the_reused_dialog(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
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


def test_open_settings_shows_the_key_the_desktop_really_holds(isolated_xdg, qapp, monkeypatch):
    """The Hotkeys tab's portal fields are a first-run preference that GNOME
    never applies, so the window has to show the effective trigger beside them -
    re-read as the window opens, not as the daemon started.

    The re-read is a D-Bus round trip and no longer holds the window shut, so
    the label is corrected a moment after it appears rather than before.
    """
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    held = {"dictate": ""}

    class Refreshing(FakeListener):
        def __init__(self, on_event, shortcuts, **kwargs):
            super().__init__()

        def shortcut_state(self):
            return STATE_BOUND if all(held.values()) else STATE_UNASSIGNED

        def effective_triggers(self):
            return dict(held)

        def refresh_triggers(self):
            held.update({"dictate": "F13"})     # assigned since the daemon started
            return dict(held)

    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.PortalListener", Refreshing)
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    cfg.save()
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    try:
        d.open_settings()
        label = d._settings.portal_effective["dictate"]
        assert settle(qapp, lambda: "F13" in label.text()), label.text()
        d._settings.close()
    finally:
        d.shutdown()


class SlowPortal(FakeListener):
    """A portal listener whose round trip takes as long as a real one can.

    `refresh_triggers` is a synchronous D-Bus call bounded by the listener's
    own REFRESH_TIMEOUT_S (2 s), and was measured at up to ~2.5 s on the
    owner's GNOME session. Held here by an Event so the test decides when the
    desktop answers.
    """

    held = "F13"
    answer = "Super+D"

    def __init__(self, on_event=None, shortcuts=None, **kwargs):
        super().__init__()
        self.release = threading.Event()
        self.calls = 0
        self._triggers = {"dictate": self.held}

    def shortcut_state(self):
        return STATE_BOUND

    def effective_triggers(self):
        return dict(self._triggers)

    def refresh_triggers(self):
        self.calls += 1
        self.release.wait(2.0)             # bounded, so a RED run still finishes
        self._triggers = {"dictate": self.answer}
        return dict(self._triggers)


def _slow_portal_daemon(monkeypatch, listener_cls=SlowPortal, sources=lambda: []):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.capture_sources", sources)
    monkeypatch.setattr("voice.daemon.PortalListener", listener_cls)
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    cfg.save()
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    return d


def test_opening_the_settings_window_does_not_wait_for_the_portal(isolated_xdg, qapp, monkeypatch):
    """The owner's "why is opening the setting so slow?".

    Asking the desktop what key it holds is a D-Bus round trip of up to a
    couple of seconds, and it used to happen on the Qt thread before the window
    was shown. The listener caches the answer and keeps it current from
    `ShortcutsChanged`, so the window can open on what is already known.
    """
    d = _slow_portal_daemon(monkeypatch)
    try:
        started = time.perf_counter()
        d.open_settings()
        elapsed = time.perf_counter() - started
        assert d._settings.isVisible()
        assert elapsed < 0.5, f"opening the window blocked for {elapsed:.2f} s"
        assert SlowPortal.held in d._settings.portal_effective["dictate"].text()
        d.listener.release.set()
        d._settings.close()
    finally:
        d.listener.release.set()
        d.shutdown()


def test_a_trigger_refresh_that_lands_late_updates_the_open_window(isolated_xdg, qapp, monkeypatch):
    """Opening on the cached answer is only honest if the fresh one arrives."""
    d = _slow_portal_daemon(monkeypatch)
    try:
        d.open_settings()
        label = d._settings.portal_effective["dictate"]
        assert SlowPortal.answer not in label.text()
        d.listener.release.set()
        assert settle(qapp, lambda: SlowPortal.answer in label.text()), label.text()
        d._settings.close()
    finally:
        d.listener.release.set()
        d.shutdown()


def test_listing_microphones_does_not_block_the_settings_window(isolated_xdg, qapp, monkeypatch):
    """`pw-dump` is a subprocess with a five second timeout, on the same path.

    Until it answers the window still has to offer the microphone the config
    names, or a Save made in the meantime would quietly write it away.
    """
    from voice.audio.capture import Source

    listing = threading.Event()

    def slow_sources():
        listing.wait(2.0)
        return [Source("alsa_input.obsbot", "OBSBOT Tiny 3", True)]

    d = _slow_portal_daemon(monkeypatch, sources=slow_sources)
    d.config.set("audio.device", "alsa_input.obsbot")
    d.config.save()
    try:
        started = time.perf_counter()
        d.open_settings()
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"opening the window blocked for {elapsed:.2f} s"
        combo = d._settings.device_combo
        assert combo.currentData() == "alsa_input.obsbot", "the configured microphone was dropped"
        listing.set()
        assert settle(qapp, lambda: combo.currentText() == "OBSBOT Tiny 3 (default)"), combo.currentText()
        assert combo.currentData() == "alsa_input.obsbot"
        d._settings.close()
    finally:
        listing.set()
        d.listener.release.set()
        d.shutdown()


def test_saving_settings_does_not_freeze_the_window_on_the_portal(isolated_xdg, qapp, monkeypatch):
    """`apply_config` runs on the Qt thread too - Save must not stall either."""
    d = _slow_portal_daemon(monkeypatch)
    try:
        started = time.perf_counter()
        d.apply_config()
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"applying the config blocked for {elapsed:.2f} s"
        d.listener.release.set()
        assert settle(qapp, lambda: d.listener.effective_triggers()["dictate"] == SlowPortal.answer)
    finally:
        d.listener.release.set()
        d.shutdown()


def test_open_settings_does_not_reset_a_visible_dialog(isolated_xdg, qapp, monkeypatch):
    """A second `voice settings` (or tray click) on an open dialog must raise the
    user's half-finished edits, not throw them away."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
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


def test_reload_re_reads_the_desktops_shortcut_assignment(isolated_xdg, qapp, monkeypatch):
    """The desktop owns the key, so ours goes stale the moment the user visits
    Keyboard Settings. `voice reload` asks the listener again rather than
    reporting what was true when the daemon started."""
    listed = {"dictate": ""}

    class Refreshing(FakeListener):
        def __init__(self, on_event, shortcuts, **kwargs):
            super().__init__()
            self.refreshed = 0

        def shortcut_state(self):
            return STATE_BOUND if all(listed.values()) else STATE_UNASSIGNED

        def effective_triggers(self):
            return dict(listed)

        def refresh_triggers(self):
            self.refreshed += 1
            listed.update({"dictate": "F13"})       # what Keyboard Settings now holds
            return dict(listed)

    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.PortalListener", Refreshing)
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    cfg.save()
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    try:
        assert d.handle({"cmd": "status"})["shortcut_state"] == STATE_UNASSIGNED
        d.apply_config()
        assert settle(qapp, lambda: d.listener.refreshed == 1), d.listener.refreshed
        st = d.handle({"cmd": "status"})
        assert st["shortcut_triggers"] == {"dictate": "F13"}
        assert st["shortcut_state"] == STATE_BOUND
    finally:
        d.shutdown()


def test_reload_is_unbothered_by_a_listener_that_cannot_be_asked(isolated_xdg, qapp, monkeypatch):
    """The evdev listener has no such thing, and neither has a test's fake."""
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(),
               notifier=QuietNotifier())
    d.build()
    try:
        d.apply_config()
        assert d.effective_triggers() == {}
    finally:
        d.shutdown()


def test_the_daemon_exposes_the_effective_triggers_to_the_settings_window(isolated_xdg, qapp, monkeypatch):
    """Defect 1's Hotkeys tab shows the desktop's key beside each field, and it
    reads it from here rather than opening a portal session of its own."""
    d = _reporting_portal_daemon(monkeypatch, QuietNotifier(), state=STATE_UNASSIGNED,
                                 triggers={"dictate": "", "recall": "F14"})
    try:
        assert d.effective_triggers() == {"dictate": "", "recall": "F14"}
    finally:
        d.shutdown()


def test_a_shortcut_assigned_again_may_warn_again_if_it_is_lost(isolated_xdg, qapp, monkeypatch):
    """The one-shot latch must not outlive the problem it reported: once the
    desktop hands the key back, a later loss is news again."""
    notified = []
    notifier = type("N", (), {
        "notify": lambda self, title, body, urgency="normal": notified.append(title),
        "set_enabled": lambda self, enabled: None})()
    captured = {}
    d = _reporting_portal_daemon(monkeypatch, notifier, state=STATE_UNASSIGNED,
                                 triggers={"dictate": ""}, captured=captured)
    try:
        captured["on_ready"](STATE_UNASSIGNED)
        assert len(notified) == 1
        captured["on_ready"](STATE_BOUND)              # assigned in Keyboard Settings
        assert len(notified) == 1                      # a working binding says nothing
        captured["on_ready"](STATE_UNASSIGNED)         # and lost again later
        assert len(notified) == 2
    finally:
        d.shutdown()


def test_status_follows_a_shortcut_the_desktop_reassigned(isolated_xdg, qapp, monkeypatch):
    """End to end, with the real listener on the fake portal bus: the desktop
    rebinds our shortcut, and the daemon reports the new key without a restart.

    The fake bus lives with the listener's own tests; `tests/` is on sys.path
    for every test module, so it is imported rather than copied.
    """
    from hotkey.test_portal_listener import FakeConn, wait_for

    from voice.hotkey.portal_listener import PortalListener

    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    conn = FakeConn(bind_triggers={"dictate": ""})     # registered with no key attached
    monkeypatch.setattr("voice.daemon.PortalListener",
                        lambda on_event, shortcuts, **kw: PortalListener(
                            on_event, shortcuts, bus_factory=lambda bus="SESSION": conn, **kw))
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    d.listener.start()                                 # what run() does
    try:
        assert wait_for(lambda: d.listener.devices_ok() is not None)
        assert d.handle({"cmd": "status"})["shortcut_state"] == STATE_UNASSIGNED

        conn.emit_changed({"dictate": "F13"})          # the user assigned it in Settings
        assert wait_for(lambda: d.handle({"cmd": "status"})["shortcut_triggers"]
                        == {"dictate": "F13"})
        st = d.handle({"cmd": "status"})
        assert st["shortcut_state"] == STATE_BOUND
        assert st["keyboard"] is True
        assert d.effective_triggers() == {"dictate": "F13"}
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


def test_injecting_asks_for_the_fill_only_when_the_pill_is_about_to_be_hidden():
    """A layer-shell pill is never unmapped for the chord, so it keeps the
    completion it already runs when the checkmark arrives. `recall` never had
    a transcription behind it, so there is no fill to finish and nothing to
    wait for - re-inserting from the history must not slow down."""
    from voice.daemon import overlay_messages
    from voice.pipeline import State

    assert overlay_messages(State.INJECTING, "", "en") == []
    assert overlay_messages(State.INJECTING, "", "en", finish_fill=True) == [{"finish": True}]
    assert overlay_messages(State.INJECTING, "recall", "en", finish_fill=True) == []


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
        {"language": "sv"}, {"state": "notice", "swaps_language": True, "text": "EN → SV"}]
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
    assert seen == [{"position": "top-center", "margin_x": 0, "margin_y": 48, "lang": "sv",
                     "verbose": False, "allow_fallback": True}]
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
    assert seen[-1]["position"] == "bottom-center"

    d.apply_config()
    assert d.overlay is first                       # unchanged: the pill stays up
    assert len(helper_processes.made) == 1

    external = Config.load()
    external.set("ui.overlay_position", "top")
    external.save()
    d.apply_config()
    assert d.overlay is not first
    assert seen[-1]["position"] == "top-center"     # the new helper knows
    assert len(helper_processes.made) == 2
    assert first.status() == "stopped"              # and the old one was stopped
    d.shutdown()


def test_reload_moves_the_pill_when_only_a_margin_changed(
        isolated_xdg, qapp, monkeypatch, helper_processes):
    """The margins are baked into the command line exactly as the position is."""
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
    assert (seen[-1]["margin_x"], seen[-1]["margin_y"]) == (0, 48)

    external = Config.load()
    external.set("ui.overlay_position", "middle-right")
    external.set("ui.overlay_margin_x", 24)
    external.set("ui.overlay_margin_y", 0)
    external.save()
    d.apply_config()
    assert d.overlay is not first
    assert seen[-1]["position"] == "middle-right"
    assert (seen[-1]["margin_x"], seen[-1]["margin_y"]) == (24, 0)
    assert len(helper_processes.made) == 2
    assert first.status() == "stopped"
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


# -- a pill that takes keyboard focus (GNOME: no layer-shell) ------------------
class FakeProbe:
    """What `cached_probe()` answers: does the helper get a layer-shell surface?"""

    def __init__(self, command=("python3",), layer_shell=False):
        self.command = list(command) if command else None
        self.layer_shell = layer_shell


def _focus_daemon(monkeypatch, *, overlay=True, allow_fallback=True, layer_shell=False,
                  command=("python3",), mode="paste", pill_focus=None, settle_ms=None,
                  clock=None, window_command=None):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    monkeypatch.setattr("voice.ui.overlay_client.cached_probe",
                        lambda: FakeProbe(command, layer_shell))
    cfg = Config.load()
    cfg.set("ui.overlay", overlay)
    cfg.set("ui.overlay_allow_fallback", allow_fallback)
    cfg.set("inject.mode", mode)
    if pill_focus is not None:
        cfg.set("inject.pill_focus", pill_focus)
    if settle_ms is not None:
        cfg.set("inject.pill_settle_ms", settle_ms)
    if window_command is not None:
        # A test that wants the whole insertion sequence, restore included,
        # has to let the injector learn what window it is aiming at: an
        # unknown one is deliberately never restored over.
        cfg.set("inject.active_window_command", window_command)
    cfg.save()
    kwargs = {"clock": clock} if clock is not None else {}
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=FakeTray(),
               notifier=QuietNotifier(), **kwargs)
    d.build()
    return d


def test_a_pill_that_cannot_refuse_focus_makes_the_injector_hide_it(isolated_xdg, qapp, monkeypatch):
    """The owner's "ctrl-v dont work with auto paste".

    No layer-shell and the fallback window allowed: the pill is an ordinary
    window with the keyboard, so the injector is built to take it off screen
    for the chord rather than paste into it.
    """
    d = _focus_daemon(monkeypatch)
    try:
        assert d.injector._pill_policy == "hide"
        assert d.injector._settle_s == pytest.approx(0.15)
    finally:
        d.shutdown()


@pytest.mark.parametrize("kwargs", [
    {"layer_shell": True},                 # a real layer surface never takes focus
    {"allow_fallback": False},             # the helper exits instead of showing one
    {"overlay": False},                    # no pill at all
    {"command": None},                     # no helper can run here
    {"mode": "clipboard"},                 # not pasting anyway
])
def test_a_pill_that_is_not_in_the_way_leaves_the_paste_alone(isolated_xdg, qapp, monkeypatch, kwargs):
    d = _focus_daemon(monkeypatch, **kwargs)
    try:
        assert d.injector._pill_policy == "none"
    finally:
        d.shutdown()


@pytest.mark.parametrize("choice,expected", [
    ("hide", "hide"),
    ("clipboard", "clipboard"),
    ("paste", "paste"),
    ("nonsense", "hide"),                  # a hand edit falls back to the fix
])
def test_inject_pill_focus_chooses_what_happens(isolated_xdg, qapp, monkeypatch, choice, expected):
    d = _focus_daemon(monkeypatch, pill_focus=choice)
    try:
        assert d.injector._pill_policy == expected
    finally:
        d.shutdown()


def test_the_settle_is_configurable_without_a_rebuild(isolated_xdg, qapp, monkeypatch):
    """How long a compositor takes to hand focus back is a guess, so it is a key."""
    d = _focus_daemon(monkeypatch, settle_ms=320)
    try:
        assert d.injector._settle_s == pytest.approx(0.32)
    finally:
        d.shutdown()


def test_hiding_the_pill_takes_it_off_screen_and_waits_for_the_write(isolated_xdg, qapp, monkeypatch):
    """What the injector calls just before the chord."""
    d = _focus_daemon(monkeypatch)
    sent, flushed = [], []

    class FakeOverlay:
        def send(self, message): sent.append(message)
        def flush(self, timeout=1.0): flushed.append(timeout); return True
        def stop(self): pass
        def status(self): return "running"

    d.overlay = FakeOverlay()
    try:
        d._hide_pill_for_paste()
        assert sent == [{"state": "hidden"}]
        assert flushed, "the message has to have reached the helper before the chord"
    finally:
        d.shutdown()


def test_status_says_the_pill_is_hidden_for_the_chord(isolated_xdg, qapp, monkeypatch):
    d = _focus_daemon(monkeypatch)
    try:
        insertion = d.handle({"cmd": "status"})["insertion"]
        assert insertion.startswith("paste")
        assert "pill" in insertion and "focus" in insertion
    finally:
        d.shutdown()


def test_status_says_when_the_pill_has_cost_the_owner_auto_paste(isolated_xdg, qapp, monkeypatch):
    d = _focus_daemon(monkeypatch, pill_focus="clipboard")
    try:
        insertion = d.handle({"cmd": "status"})["insertion"]
        assert insertion.startswith("clipboard")
        assert "Ctrl+V" in insertion and "pill" in insertion
    finally:
        d.shutdown()


def test_status_is_quiet_when_nothing_is_in_the_way(isolated_xdg, qapp, monkeypatch):
    d = _focus_daemon(monkeypatch, layer_shell=True)
    try:
        assert d.handle({"cmd": "status"})["insertion"] == "paste"
    finally:
        d.shutdown()


def test_the_pill_goes_off_screen_settles_and_only_then_is_the_chord_sent(
        isolated_xdg, qapp, monkeypatch):
    """The whole fix, in order, through the daemon's real wiring.

    The pipeline drives INJECTING, then `injector.inject`, then the IDLE that
    carries the checkmark; this drives those three by hand so the ordering is
    observable in one log. The injector's clock is replaced, so every settle is
    recorded rather than waited for and nothing here reads the wall clock.

    The fill's run to the end of the track belongs at the front of this: the
    owner's pill cannot refuse focus, so it is unmapped for the chord, and
    unmapping it mid-sweep is exactly why the bar "never goes to the end".
    """
    from voice.daemon import PILL_FILL_S
    from voice.pipeline import State

    # A known, non-terminal window, so the sequence under test includes the
    # clipboard restore: an unverifiable paste deliberately keeps the
    # transcript instead, which is covered in tests/inject/test_injector.py.
    d = _focus_daemon(monkeypatch, settle_ms=180, window_command="echo firefox")
    log = []

    class RecordingOverlay:
        def send(self, message): log.append(message)
        def flush(self, timeout=1.0): return True
        def stop(self): pass
        def status(self): return "running"

    class RecordingSender:
        name = "fake"
        def send_chord(self, codes): log.append(("chord", codes))
        def available(self): return True

    class RecordingClipboard:
        def snapshot(self): return type("S", (), {"text": "old"})()
        def set_text(self, text): log.append(("copy", text))
        def restore(self, snap): log.append("restore"); return True

    d.overlay = RecordingOverlay()
    d.injector._sender = RecordingSender()
    d.injector._clip = RecordingClipboard()
    # The injector just sleeps; what each wait is for is its position, so the
    # log names it that way - before the pill is unmapped it can only be the fill.
    d.injector._sleep = lambda seconds: log.append(
        ("fill" if {"state": "hidden"} not in log else "settle", round(seconds, 3)))
    try:
        # A real dictation reads the focused window when recording starts, long
        # before this point; without it the paste is an unverifiable one and
        # deliberately keeps the clipboard, which is a different sequence.
        d._focused_window.capture()
        d._on_dictation_state(State.INJECTING)
        d.injector.inject("hello")
        d._on_dictation_state(State.IDLE, "5 chars via fake in 0.1s")
    finally:
        d.shutdown()

    assert log == [
        {"finish": True},                    # run the fill to the end of its track
        ("copy", "hello"),                   # which the clipboard work is paid out of
        ("fill", pytest.approx(PILL_FILL_S)),  # the rest of it, before anything hides
        {"state": "hidden"},                 # the pill lets go of the keyboard
        ("settle", 0.18),                    # the compositor hands focus back
        ("chord", [29, 47]),                 # and only now, ctrl+v
        ("settle", 0.15),                    # the injector's own paste settle
        "restore",
        {"state": "done"},                   # the pill comes straight back
    ], log


class _PillClock:
    """An injected clock the test moves itself; the daemon never reads the wall."""

    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


def _recording_pill_daemon(monkeypatch, *, clock=None, spend=0.0, answers=None, **kwargs):
    """A focus-stealing daemon with every moving part recorded in one log.

    `spend` is how long the clipboard work takes, which is time the fill's
    completion is running through anyway. `answers` is the helper saying how
    long its fill really needs; None is a helper that says nothing at all,
    which is every other test in this file and the behaviour to degrade to.
    """
    log = []
    d = _focus_daemon(monkeypatch, clock=clock, **kwargs)

    class RecordingOverlay:
        def send(self, message): log.append(message)
        def flush(self, timeout=1.0): return True
        def stop(self): pass
        def status(self): return "running"

    class AnsweringOverlay(RecordingOverlay):
        """A helper that talks back, the way the real one does."""

        def expect_finish(self): log.append("expect-finish")

        def finish_wait(self, timeout):
            log.append(("asked", round(timeout, 4)))
            return answers

    class RecordingSender:
        name = "fake"
        def send_chord(self, codes): log.append(("chord", codes))
        def available(self): return True

    class RecordingClipboard:
        def snapshot(self): return type("S", (), {"text": "old"})()

        def set_text(self, text):
            log.append(("copy", text))
            if clock is not None and spend:
                clock.advance(spend)

        def restore(self, snap): log.append("restore"); return True

    def sleep(seconds):
        # Named by position: nothing is waited for between the fill and the
        # hide except the fill, and nothing after the hide except settles.
        log.append(("fill" if {"state": "hidden"} not in log else "settle",
                    round(seconds, 4)))
        if clock is not None:
            clock.advance(seconds)

    d.overlay = RecordingOverlay() if answers is None else AnsweringOverlay()
    d.injector._sender = RecordingSender()
    d.injector._clip = RecordingClipboard()
    d.injector._sleep = sleep
    return d, log


def test_the_layer_shell_path_never_asks_for_a_fill_or_waits_for_one(
        isolated_xdg, qapp, monkeypatch):
    """Where the pill can refuse focus nothing hides it, so the completion
    stays where it was: in the checkmark's own lead-in, costing nothing."""
    from voice.pipeline import State

    d, log = _recording_pill_daemon(monkeypatch, layer_shell=True,
                                    window_command="echo firefox")
    try:
        assert d.injector._pill_policy == "none"
        d._focused_window.capture()          # as a real recording would have
        d._on_dictation_state(State.INJECTING)
        d.injector.inject("hello")
        d._on_dictation_state(State.IDLE, "5 chars via fake in 0.1s")
    finally:
        d.shutdown()

    assert {"finish": True} not in log
    assert log == [("copy", "hello"), ("chord", [29, 47]), ("fill", 0.15), "restore",
                   {"state": "done"}], log


def test_a_failed_transcription_asks_for_no_completion_and_waits_for_nothing(
        isolated_xdg, qapp, monkeypatch):
    """An error is not an operation to finish, and it never reaches the chord."""
    from voice.pipeline import State

    clock = _PillClock()
    d, log = _recording_pill_daemon(monkeypatch, clock=clock)
    try:
        d._on_dictation_state(State.ERROR, "whisper died")
        assert log == [{"state": "error", "text": "whisper died"}]
        assert d._pill_fill_wait() == 0.0, "nothing was armed to wait for"
    finally:
        d.shutdown()


def test_the_added_latency_is_only_what_the_injection_had_not_already_spent(
        isolated_xdg, qapp, monkeypatch):
    """The budget: the fill's completion costs the paste the part of itself
    that the injection did not use for its own work, and never more than the
    completion. Everything is measured on the injected clock."""
    from voice.daemon import PILL_FILL_S
    from voice.pipeline import State

    for spend, expected in ((0.0, PILL_FILL_S), (0.12, PILL_FILL_S - 0.12), (0.5, 0.0)):
        clock = _PillClock()
        d, log = _recording_pill_daemon(monkeypatch, clock=clock, spend=spend)
        try:
            d._on_dictation_state(State.INJECTING)
            d.injector.inject("hello")
            settle_s = d.injector._settle_s
        finally:
            d.shutdown()
        chord = next(i for i, e in enumerate(log)
                     if isinstance(e, tuple) and e[0] == "chord")
        fill = sum(seconds for kind, seconds in
                   [e for e in log if isinstance(e, tuple) and e[0] == "fill"])
        assert fill == pytest.approx(max(0.0, expected), abs=1e-6), log
        assert fill <= PILL_FILL_S, "never longer than the animation itself"
        # The budget, from the transcript landing to the chord going out: the
        # insertion's own work, then whatever is left of the completion, then
        # the unchanged settle. Waiting for the fill costs the difference and
        # nothing when the insertion took longer than the fill did.
        waited = spend + sum(seconds for kind, seconds in
                             [e for e in log[:chord] if isinstance(e, tuple)]
                             if kind in ("fill", "settle"))
        assert waited == pytest.approx(max(PILL_FILL_S, spend) + settle_s, abs=1e-6), log


def test_a_pill_that_says_it_is_not_animating_costs_the_paste_nothing(
        isolated_xdg, qapp, monkeypatch):
    """`finish_fill` answers 0 under reduced motion, and for a model that was
    never transcribing. The daemon could not see that - it armed the wait on
    the pill policy alone - so every paste paid up to FINISH seconds for an
    animation nobody was watching. The helper is asked, and says so."""
    from voice.pipeline import State

    clock = _PillClock()
    d, log = _recording_pill_daemon(monkeypatch, clock=clock, answers=0.0)
    started = clock.t
    try:
        d._on_dictation_state(State.INJECTING)
        d.injector.inject("hello")
    finally:
        d.shutdown()

    assert {"finish": True} in log                      # the fill is still asked for
    assert log.index("expect-finish") < log.index({"finish": True}), \
        "the answer has to be waited for before the question goes out"
    assert [e for e in log if isinstance(e, tuple) and e[0] == "fill"] == [], log
    assert clock.t - started == pytest.approx(d.injector._settle_s + 0.15, abs=1e-6)


def test_a_helper_that_only_starts_the_fill_later_still_gets_all_of_it(
        isolated_xdg, qapp, monkeypatch):
    """The deadline was `now + FINISH`, set where the message was *queued* -
    behind up to thirty level messages a second. A helper that got to the fill
    later had its deadline expire before the fill began, and the pill was
    unmapped mid-sweep: the exact defect this whole change exists to fix.

    Here the insertion spends half a second before it asks, which is well past
    that old deadline, and the helper answers that its fill has only just
    started."""
    from voice.daemon import PILL_FILL_S
    from voice.pipeline import State

    clock = _PillClock()
    d, log = _recording_pill_daemon(monkeypatch, clock=clock, spend=0.5,
                                    answers=PILL_FILL_S)
    try:
        d._on_dictation_state(State.INJECTING)
        d.injector.inject("hello")
    finally:
        d.shutdown()

    fill = [seconds for kind, seconds in
            [e for e in log if isinstance(e, tuple) and e[0] == "fill"]]
    assert fill == [pytest.approx(PILL_FILL_S)], log
    chord = next(i for i, e in enumerate(log) if isinstance(e, tuple) and e[0] == "chord")
    assert log.index({"state": "hidden"}) < chord, "and only then is the pill unmapped"


def test_a_helper_that_never_answers_leaves_the_old_estimate_in_place(
        isolated_xdg, qapp, monkeypatch):
    """An older helper, or one that is wedged, says nothing: the daemon must
    fall back to counting FINISH from the message rather than wait for ever."""
    from voice.daemon import PILL_FILL_S
    from voice.pipeline import State

    clock = _PillClock()
    d, log = _recording_pill_daemon(monkeypatch, clock=clock, answers=None)
    try:
        d._on_dictation_state(State.INJECTING)
        d.injector.inject("hello")
    finally:
        d.shutdown()
    assert [seconds for kind, seconds in
            [e for e in log if isinstance(e, tuple) and e[0] == "fill"]] == \
        [pytest.approx(PILL_FILL_S)], log


def test_a_recall_is_not_slowed_down_by_a_fill_that_never_ran(isolated_xdg, qapp, monkeypatch):
    """Re-inserting from the history has no transcription behind it."""
    from voice.pipeline import State

    clock = _PillClock()
    d, log = _recording_pill_daemon(monkeypatch, clock=clock)
    try:
        started = clock.t
        d._on_dictation_state(State.INJECTING, "recall")
        d.injector.inject("hello")
    finally:
        d.shutdown()
    assert {"finish": True} not in log
    assert clock.t - started == pytest.approx(d.injector._settle_s + 0.15, abs=1e-6), log


def test_a_reload_re_reads_the_pill_policy(isolated_xdg, qapp, monkeypatch):
    """Changing it in the settings window must not need a daemon restart."""
    d = _focus_daemon(monkeypatch)
    try:
        assert d.injector._pill_policy == "hide"
        external = Config.load()
        external.set("inject.pill_focus", "clipboard")
        external.save()
        d.apply_config()
        assert d.injector._pill_policy == "clipboard"
    finally:
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


def test_the_reused_settings_dialog_captures_with_the_current_listener(isolated_xdg, qapp, monkeypatch):
    """The dialog is built once and kept. It used to capture the *bound method*
    of the listener alive at that moment, so after any rebind "Capture key"
    called a stopped listener and the button sat on "Press a key..." for ever."""
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    d = _portal_daemon(monkeypatch)
    d.open_settings()
    dialog = d._settings
    first = d.listener
    dialog.close()

    external = Config.load()
    external.set("hotkeys.portal_dictate", "CTRL+ALT+d")
    external.save()
    d.apply_config()
    assert d.listener is not first                  # rebound, same backend

    d.open_settings()
    d._settings.shortcuts_button.click()            # the portal's own dialog path
    assert hasattr(d.listener, "cb")                # the live listener was asked
    assert not hasattr(first, "cb")                 # and the dead one was not
    d._settings.close()
    d.shutdown()


def test_the_evdev_capture_button_follows_a_rebuilt_listener(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    d = _evdev_daemon(monkeypatch)
    d.open_settings()
    dialog, first = d._settings, d.listener
    dialog.close()

    external = Config.load()
    external.set("hotkeys.backend", "auto")         # still evdev here, but rebuilt
    external.save()
    monkeypatch.setattr("voice.daemon.keyboards_are_readable", lambda: True)
    monkeypatch.setattr("voice.daemon.has_local_seat", lambda: True)
    d.apply_config()
    assert d.listener is not first

    d.open_settings()
    d._settings.capture_button.click()
    assert hasattr(d.listener, "cb")
    assert not hasattr(first, "cb")
    d._settings.close()
    d.shutdown()


def test_a_backend_change_rebuilds_the_settings_dialog_portal_to_evdev(isolated_xdg, qapp, monkeypatch):
    """The backend is baked into the dialog's widget set, so a reused dialog
    showed the old backend's fields - portal trigger boxes for a listener that
    no longer reads them, and no way to capture the key that now applies."""
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    d = _portal_daemon(monkeypatch)
    d.open_settings()
    first_dialog = d._settings
    assert set(first_dialog.portal_edits)
    first_dialog.close()

    monkeypatch.setattr("voice.daemon.EvdevListener", lambda tracker, on_event: FakeListener())
    external = Config.load()
    external.set("hotkeys.backend", "evdev")
    external.save()
    d.apply_config()

    d.open_settings()
    assert d._settings is not first_dialog
    assert d._settings.portal_edits == {}
    assert d._settings.shortcuts_button is None
    assert d._settings.capture_button.isHidden() is False
    d._settings.close()
    d.shutdown()


def test_a_backend_change_rebuilds_the_settings_dialog_evdev_to_portal(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    d = _evdev_daemon(monkeypatch)
    d.open_settings()
    first_dialog = d._settings
    assert first_dialog.portal_edits == {}
    first_dialog.close()

    monkeypatch.setattr("voice.daemon.PortalListener", FakePortalListener)
    external = Config.load()
    external.set("hotkeys.backend", "portal")
    external.save()
    d.apply_config()

    d.open_settings()
    assert d._settings is not first_dialog
    assert set(d._settings.portal_edits) == {"dictate", "recall", "cancel", "language_toggle"}
    assert d._settings.capture_button.isHidden() is True
    d._settings.close()
    d.shutdown()


def test_a_backend_change_replaces_the_window_the_user_is_looking_at(isolated_xdg, qapp, monkeypatch):
    """Saving the backend change from this very window is the commonest way to
    make it: the reload must not leave the old backend's fields on screen."""
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    d = _portal_daemon(monkeypatch)
    d.open_settings()
    first_dialog = d._settings
    assert first_dialog.isVisible()

    monkeypatch.setattr("voice.daemon.EvdevListener", lambda tracker, on_event: FakeListener())
    external = Config.load()
    external.set("hotkeys.backend", "evdev")
    external.save()
    d.apply_config()

    assert d._settings is not first_dialog
    assert d._settings.isVisible()
    assert d._settings.portal_edits == {}
    assert first_dialog.isVisible() is False
    d._settings.close()
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


# -- the settings window wrote the desktop's own shortcut store -----------------
def test_a_desktop_shortcut_write_rebinds_even_when_the_config_did_not_move(
        isolated_xdg, qapp, monkeypatch):
    """Writing GNOME's dconf key changes the key the *desktop* holds, which no
    config snapshot can notice - and without a rebind the old one stays live
    until the daemon is restarted."""
    d = _portal_daemon(monkeypatch)
    first = d.listener
    d.rebind_hotkeys()                                  # what the window asks for
    d.apply_config()
    assert d.listener is not first
    assert len(FakePortalListener.made) == 2
    assert first.started is False and d.listener.started is True
    d.shutdown()


def test_a_rebind_request_binds_at_once_rather_than_at_some_later_reload(
        isolated_xdg, qapp, monkeypatch):
    """The window asks for this the moment the desktop's store took a new key,
    and that can happen with no save behind it at all ("Change…", then Close).
    Arming a flag for the next apply_config meant the promised immediate rebind
    never happened: the old key stayed live until something unrelated reloaded
    the config, and then tore the listener down in the middle of it."""
    d = _portal_daemon(monkeypatch)
    first = d.listener
    external = Config.load()                            # what the window just wrote
    external.set("hotkeys.portal_dictate", "CTRL+ALT+d")
    external.save()

    d.rebind_hotkeys()

    assert d.listener is not first
    assert len(FakePortalListener.made) == 2
    assert first.started is False and d.listener.started is True
    # Bound to what the file holds now, not to the snapshot the daemon had.
    assert FakePortalListener.made[-1].shortcuts["dictate"] == "CTRL+ALT+d"
    d.shutdown()


def test_a_rebind_request_leaves_nothing_armed_for_a_later_reload(
        isolated_xdg, qapp, monkeypatch):
    """The flag outlived the request it belonged to, so an unrelated
    apply_config - minutes later, with whatever config held then - tore the
    listener down again and could show the desktop's permission dialog for it."""
    d = _portal_daemon(monkeypatch)
    d.rebind_hotkeys()
    made = len(FakePortalListener.made)
    listener = d.listener

    d.apply_config()                                    # something else entirely

    assert d.listener is listener and len(FakePortalListener.made) == made
    d.shutdown()


def test_nothing_rebinds_when_no_one_asked_and_nothing_changed(isolated_xdg, qapp, monkeypatch):
    """The rebind shows the desktop's permission dialog, so it must not happen
    on every Save."""
    d = _portal_daemon(monkeypatch)
    first = d.listener
    d.apply_config()
    assert d.listener is first and len(FakePortalListener.made) == 1
    d.shutdown()


def test_the_settings_window_rebind_request_reaches_the_daemon(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    d = _portal_daemon(monkeypatch)
    d.open_settings()
    first = d.listener
    d._settings.shortcuts_rebound.emit()                # the store took a new key
    d._settings.saved.emit()                            # and the file was written
    assert d.listener is not first
    # One rebind for the one change: the desktop's permission dialog must not
    # be asked for twice because the window said both things.
    assert len(FakePortalListener.made) == 2
    d._settings.close()
    d.shutdown()


def test_a_window_that_only_wrote_the_desktop_still_gets_its_rebind(
        isolated_xdg, qapp, monkeypatch):
    """"Change…" hands the key over on the spot and emits this with no save
    behind it: that is the whole path the promise "the new key works at once"
    rests on."""
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    d = _portal_daemon(monkeypatch)
    d.open_settings()
    first = d.listener
    d._settings.shortcuts_rebound.emit()                # and no `saved` after it
    assert d.listener is not first and d.listener.started is True
    d._settings.close()
    d.shutdown()


# -- the pill must not claim an insertion that did not happen ------------------
@pytest.mark.parametrize("method", ["clipboard", "clipboard-only", "clipboard-pill"])
def test_the_pill_says_copied_when_the_text_was_only_copied(method):
    """The owner runs inject.mode = "clipboard" on purpose: the pill flashed
    "Inserted" and then they still had to press Ctrl+V themselves."""
    from voice.daemon import DONE_COPIED, overlay_messages
    from voice.pipeline import State

    messages = overlay_messages(State.IDLE, f"11 chars via {method} in 0.9s", "en")
    assert messages == [{"state": "done", "text": DONE_COPIED}]
    assert "Ctrl+V" in DONE_COPIED


@pytest.mark.parametrize("method", ["portal", "wtype", "ydotool"])
def test_a_real_paste_still_says_inserted(method):
    from voice.daemon import overlay_messages
    from voice.pipeline import State

    assert overlay_messages(State.IDLE, f"11 chars via {method} in 0.9s", "en") == [
        {"state": "done"}]


# -- the rebuilt settings window has to learn the desktop's keys ---------------
class ReadyPortalListener(FakeListener):
    """A portal listener that only answers once it has been started.

    Exactly like the real one: `refresh_triggers` short-circuits while the
    connection is None, which is the whole of this bug.
    """

    key = "F13"

    def __init__(self, on_event, shortcuts, **kwargs):
        super().__init__()
        self.on_ready = kwargs.get("on_ready")
        self.answered = False

    def shortcut_state(self):
        return STATE_BOUND if self.answered else None

    def effective_triggers(self):
        return {"dictate": self.key} if self.answered else {}

    def refresh_triggers(self):
        # The real one returns what it already has while its connection is
        # still being opened, which is as long as the desktop takes to ask.
        return self.effective_triggers()

    def answer(self):
        """The portal has bound the shortcut, on the listener thread."""
        self.answered = True
        self.on_ready(STATE_BOUND)


def test_a_dialog_rebuilt_for_a_new_backend_is_told_what_the_desktop_holds(
        isolated_xdg, qapp, monkeypatch):
    """The window was thrown away and rebuilt *before* the new listener started,
    so it asked a listener that could not answer and sat on "waiting for an
    answer" until it was closed and reopened."""
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    monkeypatch.setattr("voice.daemon.PortalListener", ReadyPortalListener)
    d = _evdev_daemon(monkeypatch)
    d.open_settings()
    external = Config.load()
    external.set("hotkeys.backend", "portal")
    external.save()
    d.apply_config()

    label = d._settings.portal_effective["dictate"]
    assert settle(qapp, lambda: label.text(), timeout=0.3)
    assert ReadyPortalListener.key not in label.text()   # the portal has not answered

    d.listener.answer()                                 # it does, a moment later
    assert settle(qapp, lambda: ReadyPortalListener.key in label.text()), label.text()
    d._settings.close()
    d.shutdown()


def test_the_desktops_answer_reaches_a_window_that_is_already_open(
        isolated_xdg, qapp, monkeypatch):
    """The portal answers on its own thread, whenever the user accepts its
    dialog; a window opened before that must not keep showing nothing."""
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    monkeypatch.setattr("voice.daemon.PortalListener", ReadyPortalListener)
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    cfg.save()
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    d = Daemon(cfg, sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    d.open_settings()
    label = d._settings.portal_effective["dictate"]
    # Let the refresh the window starts for itself finish first, so what is
    # asserted below is the push and not that background read arriving late.
    assert settle(qapp, lambda: not d._refreshers["triggers"].is_alive())
    assert ReadyPortalListener.key not in label.text()      # nothing answered yet

    d.listener.answer()                                     # from the listener thread
    assert settle(qapp, lambda: ReadyPortalListener.key in label.text()), label.text()
    d._settings.close()
    d.shutdown()


# -- and whether the pill can be placed at all --------------------------------
def test_the_settings_window_is_told_this_desktop_cannot_place_the_pill(
        isolated_xdg, qapp, monkeypatch):
    """The probe spawns interpreters, so it runs off the Qt thread and lands in
    the window afterwards - like the microphone list and the triggers."""
    from voice.ui.overlay_client import HelperProbe

    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    monkeypatch.setattr("voice.daemon.cached_probe", lambda: HelperProbe(
        ["/usr/bin/python3", "-m", "voice.ui.overlay"], ("gtk4",)))
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(),
               notifier=QuietNotifier())
    d.build()
    d.open_settings()
    warning = d._settings.placement_warning
    assert settle(qapp, lambda: warning.isVisibleTo(d._settings)), "the window never said so"
    d._settings.close()
    d.shutdown()


def test_a_desktop_that_can_place_the_pill_gets_no_warning(isolated_xdg, qapp, monkeypatch):
    from voice.ui.overlay_client import HelperProbe

    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    monkeypatch.setattr("voice.daemon.cached_probe", lambda: HelperProbe(
        ["/usr/bin/python3", "-m", "voice.ui.overlay"], ("gtk4", "layer-shell")))
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    d = Daemon(Config.load(), listener=FakeListener(), sender=FakeSender(), tray=FakeTray(),
               notifier=QuietNotifier())
    d.build()
    d.open_settings()
    assert settle(qapp, lambda: not d._refreshers["layer_shell"].is_alive())
    qapp.processEvents()
    assert d._settings.placement_warning.isVisibleTo(d._settings) is False
    d._settings.close()
    d.shutdown()


# -- showing the owner where the pill will land --------------------------------
class FakePreviewTimer:
    """Stands in for the Qt single-shot that ends the preview."""

    made: list = []

    def __init__(self, seconds, done):
        self.seconds, self.done, self.cancelled = seconds, done, False
        FakePreviewTimer.made.append(self)

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.done()


def _preview_daemon(monkeypatch, cfg=None, **kwargs):
    """A daemon whose helper is fake and whose preview clock is ours."""
    from tests.conftest import FakeHelperProcess

    spawns: list[dict] = []
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    made: list = []

    def launcher(**kw):
        spawns.append(kw)
        made.append(FakeHelperProcess())
        return made[-1]

    monkeypatch.setattr("voice.daemon.default_launcher", launcher)
    FakePreviewTimer.made = []
    d = Daemon(cfg or Config.load(), listener=FakeListener(), sender=FakeSender(),
               tray=FakeTray(), notifier=QuietNotifier(),
               preview_timer=FakePreviewTimer, **kwargs)
    d.build()
    d.overlay.start()
    d.helpers, d.spawns = made, spawns
    return d


def _lines(d, index=-1):
    assert d.overlay.flush(2.0)
    return d.helpers[index].lines()


def test_the_preview_shows_the_real_pill_where_it_was_dropped(isolated_xdg, qapp, monkeypatch):
    """The owner's "can it show the position on the desktop for say 5 sec when I
    drop it in settings?" - the helper is restarted at the placement being tried
    and shows a recording pill."""
    from voice.daemon import PREVIEW_SECONDS

    d = _preview_daemon(monkeypatch)
    reply = d.handle({"cmd": "preview_pill", "position": "top-right",
                      "margin_x": 12, "margin_y": 30})
    assert reply["ok"] is True
    assert d.spawns[-1]["position"] == "top-right"
    assert (d.spawns[-1]["margin_x"], d.spawns[-1]["margin_y"]) == (12, 30)
    states = [m for m in _lines(d) if "state" in m]
    assert states[0] == {"state": "recording"}
    assert any("level" in m for m in _lines(d)), "the pill has nothing to show"
    assert FakePreviewTimer.made[-1].seconds == PREVIEW_SECONDS
    d.shutdown()


def test_the_preview_is_not_a_dictation(isolated_xdg, qapp, monkeypatch):
    """No history entry, no state machine change: it is a picture, not a recording."""
    from voice.pipeline import State

    d = _preview_daemon(monkeypatch)
    d.handle({"cmd": "preview_pill", "position": "top-left", "margin_x": 0, "margin_y": 0})
    assert d.dictation.state is State.IDLE
    assert d.history.last() is None
    d.shutdown()


def test_the_preview_hides_itself_and_puts_the_pill_back(isolated_xdg, qapp, monkeypatch):
    d = _preview_daemon(monkeypatch)
    d.handle({"cmd": "preview_pill", "position": "top-left", "margin_x": 4, "margin_y": 4})
    preview_lines = _lines(d)
    FakePreviewTimer.made[-1].fire()
    assert preview_lines[-1] != {"state": "hidden"}      # it was still showing
    assert d.helpers[-2].lines()[-1] == {"state": "hidden"}
    # and the helper that is running now is back at the configured placement
    assert d.spawns[-1]["position"] == "bottom-center"
    assert (d.spawns[-1]["margin_x"], d.spawns[-1]["margin_y"]) == (0, 48)
    d.shutdown()


def test_a_real_dictation_ends_the_preview_at_once(isolated_xdg, qapp, monkeypatch):
    """A preview left on screen would be mistaken for the recording itself."""
    from voice.pipeline import State

    d = _preview_daemon(monkeypatch)
    d.handle({"cmd": "preview_pill", "position": "top-left", "margin_x": 0, "margin_y": 0})
    preview = d.helpers[-1]
    d._on_dictation_state(State.RECORDING, "")
    assert preview.lines()[-1] == {"state": "hidden"}
    assert FakePreviewTimer.made[-1].cancelled is True
    assert d.spawns[-1]["position"] == "bottom-center"   # the real pill is back
    assert _lines(d)[-1] == {"state": "recording"}       # and it is recording
    d.shutdown()


def test_the_preview_is_refused_when_there_is_no_pill(isolated_xdg, qapp, monkeypatch):
    cfg = Config.load()
    cfg.set("ui.overlay", False)
    d = _preview_daemon(monkeypatch, cfg)
    reply = d.handle({"cmd": "preview_pill", "position": "top-left",
                      "margin_x": 0, "margin_y": 0})
    assert reply["ok"] is False and "off" in reply["error"]
    assert d.spawns == []
    d.shutdown()


def test_the_preview_is_refused_while_a_dictation_is_running(isolated_xdg, qapp, monkeypatch):
    from voice.pipeline import State

    d = _preview_daemon(monkeypatch)
    d.dictation._state = State.RECORDING
    reply = d.handle({"cmd": "preview_pill", "position": "top-left",
                      "margin_x": 0, "margin_y": 0})
    assert reply["ok"] is False and "dicta" in reply["error"]
    d.dictation._state = State.IDLE
    d.shutdown()


def test_a_nonsense_placement_is_refused_rather_than_shown(isolated_xdg, qapp, monkeypatch):
    d = _preview_daemon(monkeypatch)
    before = len(d.spawns)                              # the real pill is already up
    assert d.handle({"cmd": "preview_pill", "position": "sideways",
                     "margin_x": 0, "margin_y": 0})["ok"] is False
    assert d.handle({"cmd": "preview_pill", "position": "top-left",
                     "margin_x": "x", "margin_y": 0})["ok"] is False
    assert d.handle({"cmd": "preview_pill", "position": "top-left",
                     "margin_x": True, "margin_y": 0})["ok"] is False
    assert len(d.spawns) == before                      # and nothing was started
    d.shutdown()


def test_a_second_preview_replaces_the_first(isolated_xdg, qapp, monkeypatch):
    """Nudging the pill repeatedly must not leave a queue of helpers behind."""
    d = _preview_daemon(monkeypatch)
    d.handle({"cmd": "preview_pill", "position": "top-left", "margin_x": 0, "margin_y": 0})
    d.handle({"cmd": "preview_pill", "position": "top-right", "margin_x": 0, "margin_y": 0})
    assert FakePreviewTimer.made[0].cancelled is True
    assert d.spawns[-1]["position"] == "top-right"
    FakePreviewTimer.made[-1].fire()
    assert d.spawns[-1]["position"] == "bottom-center"
    d.shutdown()


def test_the_settings_window_previews_through_the_daemon(isolated_xdg, qapp, monkeypatch):
    """The window asks over the same command the CLI would use, so there is one
    path into the preview and one place it is validated."""
    monkeypatch.setattr("voice.daemon.capture_sources", lambda: [])
    d = _preview_daemon(monkeypatch)
    d.open_settings()
    d._settings.pill_placer.set_placement("top-right", 10, 20)
    d._settings.pill_placer.placement_changed.emit()
    d._settings.preview_timer.timeout.emit()
    assert d.spawns[-1]["position"] == "top-right"
    assert (d.spawns[-1]["margin_x"], d.spawns[-1]["margin_y"]) == (10, 20)
    assert "Showing" in d._settings.preview_note.text()
    d._settings.close()
    d.shutdown()


def test_the_dictation_vocabulary_is_built_from_the_dictionary(isolated_xdg, qapp):
    """The words the user listed plus the spellings their replacements aim at,
    read fresh on the worker so an edit applies to the next dictation."""
    cfg = Config.load()
    cfg.set("dictionary.hotwords", ["Hollyland Lark"])
    cfg.save()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    try:
        assert d.dictation.sv.hotwords_getter() == "Hollyland Lark, CachyOS, OBSBOT"
    finally:
        d.shutdown()


def test_an_unusable_dictionary_costs_the_vocabulary_not_the_dictation(isolated_xdg, qapp):
    """_hotwords() runs on the worker just before transcribe(); raising there
    would bypass the TranscriptionError path and discard the recording."""
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender(), tray=FakeTray(), notifier=QuietNotifier())
    d.build()
    try:
        cfg.set("dictionary.replacements", "not a list at all")
        assert d._hotwords() is None or isinstance(d._hotwords(), str)
    finally:
        d.shutdown()


# -- a press the pipeline had nothing to do with -----------------------------
#: Pressing dictate while a dictation is still transcribing or pasting changed
#: nothing and said nothing, so the owner pressed again - and that press landed
#: after the pipeline had gone idle and started a recording instead.

def test_a_press_during_the_work_is_answered_on_the_pill():
    from voice.daemon import busy_messages
    from voice.pipeline import State

    assert busy_messages(State.TRANSCRIBING, "none") == [{"state": "notice", "text": "Still working"}]
    assert busy_messages(State.INJECTING, "none") == [{"state": "notice", "text": "Still pasting"}]


def test_nothing_is_put_on_screen_while_the_pill_is_hidden_for_the_chord():
    from voice.daemon import busy_messages
    from voice.pipeline import State

    # Under the `hide` policy the injector has taken a focus-stealing pill off
    # screen to send the paste chord. Mapping it again for a notice hands it
    # the keyboard back, the chord lands in the pill instead of the window, and
    # `restore_clipboard` then overwrites the transcript with the old contents
    # 150 ms later - the exact data loss this branch exists to end.
    assert busy_messages(State.INJECTING, "hide") == []
    # Transcribing is safe: the pill is on screen throughout, so a notice only
    # changes the text of a window that already has whatever focus it will get.
    assert busy_messages(State.TRANSCRIBING, "hide") == [{"state": "notice", "text": "Still working"}]


def test_states_that_can_act_on_a_press_say_nothing_extra():
    from voice.daemon import busy_messages
    from voice.pipeline import State

    # These never reach `on_busy`; if they ever did, a notice would be a lie.
    assert busy_messages(State.IDLE, "none") == []
    assert busy_messages(State.RECORDING, "none") == []


def test_the_busy_notice_is_short_enough_for_the_pill_well():
    from voice.daemon import DONE_COPIED, busy_messages
    from voice.pipeline import State

    for state in (State.TRANSCRIBING, State.INJECTING):
        text = busy_messages(state, "none")[0]["text"]
        assert len(text) <= len(DONE_COPIED), \
            f"{text!r} is wider than the pill's well already fits"


def test_a_press_landing_on_the_error_state_is_not_told_work_is_going_on():
    from voice.daemon import busy_messages
    from voice.pipeline import State

    # ERROR is entered and left inside a single `_fail` call, having already
    # raised a "Dictation failed" notification. "Still working" on top of that
    # would contradict the thing the owner was just told.
    assert busy_messages(State.ERROR, "none") == []


def test_no_notice_reaches_the_pill_while_it_is_hidden_for_a_paste(isolated_xdg, qapp, monkeypatch):
    """The press is answered, but never by putting the pill back on screen.

    `toggle()` samples the state under the lock and reports it after releasing,
    so a press made during TRANSCRIBING can be delivered while the injector has
    already taken the focus-stealing pill off screen for the chord. Mapping it
    again hands it the keyboard, the chord lands in the pill, and
    `restore_clipboard` overwrites the transcript 150 ms later.
    """
    from voice.pipeline import State

    d = _focus_daemon(monkeypatch)
    sent = []

    class FakeOverlay:
        def send(self, message): sent.append(message)
        def flush(self, timeout=1.0): return True
        def stop(self): pass
        def status(self): return "running"

    d.overlay = FakeOverlay()
    try:
        d._on_dictation_state(State.INJECTING)
        d._hide_pill_for_paste()
        assert sent[-1] == {"state": "hidden"}
        before = len(sent)

        # A press made a moment earlier, arriving now.
        d._on_dictation_busy(State.TRANSCRIBING)

        assert len(sent) == before, \
            f"the pill was put back on screen mid-paste: {sent[before:]}"

        # Once the insertion is over the pill is fair game again.
        d._on_dictation_state(State.IDLE, "5 chars via fake in 0.1s")
        d._on_dictation_state(State.TRANSCRIBING)
        before = len(sent)
        d._on_dictation_busy(State.TRANSCRIBING)
        assert sent[before:] == [{"state": "notice", "text": "Still working"}]
    finally:
        d.shutdown()


def test_a_notice_arriving_during_the_hide_flush_is_refused(isolated_xdg, qapp, monkeypatch):
    """The dangerous half-second, driven deliberately.

    `_hide_pill_for_paste` waits on the helper for up to OVERLAY_HIDE_FLUSH_S
    after unmapping the pill. That wait is ample scheduling room for the hotkey
    thread to deliver a press made moments earlier, and a notice delivered
    there remaps a focus-stealing pill with the chord already on its way.
    """
    import threading as _threading

    from voice.pipeline import State

    d = _focus_daemon(monkeypatch)
    sent, raced = [], []

    class FakeOverlay:
        def send(self, message): sent.append(message)

        def flush(self, timeout=1.0):
            if not raced:
                raced.append(True)
                press = _threading.Thread(
                    target=lambda: d._on_dictation_busy(State.TRANSCRIBING))
                press.start()
                press.join(5)
                assert not press.is_alive(), "the notice blocked on the pill lock"
            return True

        def stop(self): pass
        def status(self): return "running"

    d.overlay = FakeOverlay()
    try:
        d._on_dictation_state(State.INJECTING)
        before = len(sent)

        d._hide_pill_for_paste()

        assert raced, "the interleaving under test was never reached"
        assert sent[before:] == [{"state": "hidden"}], \
            f"something reached the pill while it was hidden for the chord: {sent[before:]}"
    finally:
        d.shutdown()


def test_an_unverifiable_paste_tells_the_owner_how_to_paste_it_themselves():
    from voice.daemon import overlay_messages
    from voice.pipeline import State

    msgs = overlay_messages(State.IDLE, "7 chars via paste-blind in 0.4s", "en",
                            blind_hint="Copied")
    assert msgs == [{"state": "done", "text": "Copied"}]


def test_a_verified_paste_keeps_its_plain_checkmark():
    from voice.daemon import overlay_messages
    from voice.pipeline import State

    msgs = overlay_messages(State.IDLE, "7 chars via portal in 0.4s", "en",
                            blind_hint="Copied")
    assert msgs == [{"state": "done"}]


def test_an_unverifiable_paste_with_nothing_useful_to_suggest_stays_quiet():
    from voice.daemon import overlay_messages
    from voice.pipeline import State

    msgs = overlay_messages(State.IDLE, "7 chars via paste-blind in 0.4s", "en")
    assert msgs == [{"state": "done"}]




def test_the_pill_lock_is_the_same_object_for_the_life_of_the_daemon(isolated_xdg, qapp, monkeypatch):
    """A lock re-created per transition guards nothing.

    Two threads then hold two different objects and both walk into
    `_send_overlay_locked` together - which is the race the notice path was
    given a lock to prevent in the first place.
    """
    from voice.pipeline import State

    d = _focus_daemon(monkeypatch)

    class FakeOverlay:
        def send(self, message): pass
        def flush(self, timeout=1.0): return True
        def stop(self): pass
        def status(self): return "running"

    d.overlay = FakeOverlay()
    try:
        first = d._overlay_lock
        for state in (State.RECORDING, State.TRANSCRIBING, State.INJECTING,
                      State.IDLE, State.ERROR):
            d._on_dictation_state(state, "")
            assert d._overlay_lock is first, \
                f"the pill lock was replaced while handling {state.value}"
        d._on_dictation_busy(State.TRANSCRIBING)
        assert d._overlay_lock is first
    finally:
        d.shutdown()


# -- a window command that will not answer must not cost every paste ---------
#: The command runs between the pill being unmapped and the chord being sent.
#: One that hangs costs every dictation its full timeout; one that is secretly
#: interactive - KWin's `queryWindowInfo` may be a window *picker* rather than
#: a query - would put a crosshair grab in front of every paste. Losing the
#: terminal chord is the lesser harm, and the pill says so out loud.

def test_a_window_command_that_times_out_is_not_asked_again(caplog):
    from voice.daemon import FocusedWindow

    calls = []

    def never_answers(cmd, on_timeout=None):
        calls.append(cmd)
        if on_timeout is not None:
            on_timeout()
        return None

    window = FocusedWindow(lambda: "some-hanging-command", run=never_answers)
    with caplog.at_level("WARNING", logger="voice.daemon"):
        window.capture(); assert window() is None
        window.capture(); assert window() is None
        window.capture(); assert window() is None

    assert calls == ["some-hanging-command"], \
        f"a command that timed out was asked {len(calls)} times"
    assert "some-hanging-command" in caplog.text


def test_a_window_command_that_answers_keeps_being_asked():
    from voice.daemon import FocusedWindow

    calls = []

    def answers(cmd, on_timeout=None):
        calls.append(cmd)
        return "org.kde.konsole"

    window = FocusedWindow(lambda: "a-good-command", run=answers)
    window.capture(); assert window() == "org.kde.konsole"
    window.capture(); assert window() == "org.kde.konsole"
    assert len(calls) == 2


def test_an_empty_answer_is_not_treated_as_a_failure():
    from voice.daemon import FocusedWindow

    calls = []

    def nothing_focused(cmd, on_timeout=None):
        calls.append(cmd)
        return None

    window = FocusedWindow(lambda: "a-good-command", run=nothing_focused)
    window.capture(); assert window() is None
    window.capture(); assert window() is None
    assert len(calls) == 2, "no window focused is not the same as no answer coming"


def test_no_command_configured_runs_nothing():
    from voice.daemon import FocusedWindow

    window = FocusedWindow(lambda: "", run=lambda cmd, on_timeout=None: "never")
    window.capture()
    assert window() is None


def test_a_window_command_given_up_on_stays_given_up_across_a_reload(isolated_xdg, qapp, monkeypatch):
    """`apply_config` rebuilt the asker, and with it its memory.

    Every language switch, profile switch and settings save therefore re-armed
    a command already known to hang, and the next paste paid its full timeout
    again - including whatever the command does while hanging.
    """
    d = _focus_daemon(monkeypatch, window_command="a-hanging-command")
    try:
        d._focused_window._give_up("a-hanging-command")
        assert d._focused_window.usable is False
        before = d._focused_window

        d.apply_config()

        assert d._focused_window is before, "the asker was rebuilt by a reload"
        assert d._focused_window.usable is False
        assert d.injector._window_class is before
    finally:
        d.shutdown()


def test_status_says_when_every_paste_has_gone_blind(isolated_xdg, qapp, monkeypatch):
    """A configured command that stopped answering still looks healthy.

    `voice doctor` reads this: without it, a machine where every paste is
    silently downgraded reports "focused window read with: <cmd>" and passes.
    """
    d = _focus_daemon(monkeypatch, window_command="a-hanging-command")
    try:
        assert d.handle({"cmd": "status"})["window_command_usable"] is True
        d._focused_window._give_up("a-hanging-command")
        assert d.handle({"cmd": "status"})["window_command_usable"] is False
    finally:
        d.shutdown()


def test_the_terminal_warning_is_re_checked_on_reload_but_not_repeated(isolated_xdg, qapp, monkeypatch, caplog):
    """Clearing inject.terminal_classes in the settings window is enough.

    From then on nothing is recognised as a terminal and every terminal paste
    is discarded - and before this the owner heard nothing until the next
    daemon restart.
    """
    d = _focus_daemon(monkeypatch)
    try:
        d.config.set("inject.terminal_classes", ["konsole"])
        d.config.set("inject.active_window_command", "echo konsole")
        d.config.save()
        d._terminal_warning = ""
        with caplog.at_level("WARNING", logger="voice.daemon"):
            d.apply_config()
        assert "terminal_classes is empty" not in caplog.text

        d.config.set("inject.terminal_classes", [])
        d.config.save()
        caplog.clear()
        with caplog.at_level("WARNING", logger="voice.daemon"):
            d.apply_config()
        assert "terminal_classes is empty" in caplog.text, \
            "a reload into the silent-discard state said nothing"

        caplog.clear()
        with caplog.at_level("WARNING", logger="voice.daemon"):
            d.apply_config()
        assert "terminal_classes is empty" not in caplog.text, \
            "the same warning was repeated on a reload that changed nothing"
    finally:
        d.shutdown()


def test_the_language_notice_also_refuses_to_remap_a_pill_hidden_for_a_paste(isolated_xdg, qapp, monkeypatch):
    """Every route to the pill has to respect the hide, not just the busy one.

    The language toggle runs on the Qt thread while the injector runs on the
    worker, so a tap of it during the paste window mapped a focus-stealing
    pill, the chord landed in it, and `restore_clipboard` took the transcript.
    """
    from voice.pipeline import State

    d = _focus_daemon(monkeypatch)
    sent = []

    class FakeOverlay:
        def send(self, message): sent.append(message)
        def flush(self, timeout=1.0): return True
        def stop(self): pass
        def status(self): return "running"

    d.overlay = FakeOverlay()
    try:
        d._on_dictation_state(State.INJECTING)
        d._hide_pill_for_paste()
        before = len(sent)

        d._set_language("sv")

        assert not [m for m in sent[before:] if m.get("state") == "notice"], \
            f"the language toggle remapped the pill mid-paste: {sent[before:]}"
    finally:
        d.shutdown()


def test_a_new_window_command_re_arms_one_that_had_been_given_up_on():
    """The give-up message tells the owner to set a working command.

    Keyed to nothing, the flag then short-circuited that command too: every
    paste stayed blind and doctor kept naming the old command, until a restart
    nothing told the owner to perform.
    """
    from voice.daemon import FocusedWindow

    configured = ["a-hanging-command"]
    calls = []

    def run(cmd, on_timeout=None):
        calls.append(cmd)
        if cmd == "a-hanging-command":
            if on_timeout is not None:
                on_timeout()
            return None
        return "org.kde.konsole"

    window = FocusedWindow(lambda: configured[0], run=run)
    window.capture(); assert window() is None
    window.capture(); assert window() is None and window.usable is False
    assert calls == ["a-hanging-command"], "the hang was re-armed"

    configured[0] = "a-working-command"           # the owner takes the advice

    window.capture(); assert window() == "org.kde.konsole"
    assert window.usable is True
    assert calls == ["a-hanging-command", "a-working-command"]


def test_the_terminal_warning_returns_after_a_trip_through_clipboard_mode(isolated_xdg, qapp, monkeypatch, caplog):
    """Switching away from pasting and back must not suppress the warning.

    The early return left the old text stored, so the identical warning was
    then discarded as "already said" while the silent-discard state was live.
    """
    d = _focus_daemon(monkeypatch)
    try:
        with caplog.at_level("WARNING", logger="voice.daemon"):
            d._warn_if_terminals_can_never_be_pasted_into(notify=False)
        assert "silently discard" in caplog.text

        d.config.set("inject.mode", "clipboard")
        d.config.save()
        d._warn_if_terminals_can_never_be_pasted_into(notify=False)

        d.config.set("inject.mode", "paste")
        d.config.save()
        caplog.clear()
        with caplog.at_level("WARNING", logger="voice.daemon"):
            d._warn_if_terminals_can_never_be_pasted_into(notify=False)
        assert "silently discard" in caplog.text, \
            "the warning stayed suppressed after a trip through clipboard mode"
    finally:
        d.shutdown()


def test_the_pill_placement_preview_refuses_to_map_during_a_paste(isolated_xdg, qapp, monkeypatch):
    """The preview is a focus-stealing window like any other.

    Mapped between the pill being unmapped and its chord being sent, it takes
    the keyboard, the chord lands in the preview, and `restore_clipboard`
    overwrites the transcript 150 ms later.
    """
    from voice.pipeline import State

    d = _focus_daemon(monkeypatch)
    stopped = []

    class FakeOverlay:
        def send(self, message): pass
        def flush(self, timeout=1.0): return True
        def stop(self): stopped.append(True)
        def status(self): return "running"

    d.overlay = FakeOverlay()
    try:
        d._on_dictation_state(State.INJECTING)
        d._hide_pill_for_paste()

        d._preview_pill("top-center", 0, 0)

        assert stopped == [], "the live pill was torn down mid-paste"
        assert d._preview is None, "a preview pill was mapped mid-paste"

        # The control: with no paste in flight the same call does map one, so
        # the assertions above are about the guard and not about the fakes.
        d._on_dictation_state(State.IDLE, "5 chars via fake in 0.1s")
        d._preview_pill("top-center", 0, 0)
        assert d._preview is not None and stopped, \
            "the preview never maps at all; the test above proves nothing"
        d._end_pill_preview()
    finally:
        d.shutdown()


def test_the_terminal_warning_returns_after_a_trip_through_the_clipboard_pill_policy(
        isolated_xdg, qapp, monkeypatch, caplog):
    d = _focus_daemon(monkeypatch)
    try:
        with caplog.at_level("WARNING", logger="voice.daemon"):
            d._warn_if_terminals_can_never_be_pasted_into(notify=False)
        assert "silently discard" in caplog.text

        was, d._pill_policy = d._pill_policy, "clipboard"
        d._warn_if_terminals_can_never_be_pasted_into(notify=False)
        d._pill_policy = was

        caplog.clear()
        with caplog.at_level("WARNING", logger="voice.daemon"):
            d._warn_if_terminals_can_never_be_pasted_into(notify=False)
        assert "silently discard" in caplog.text, \
            "the warning stayed suppressed after a trip through the clipboard policy"
    finally:
        d.shutdown()


# -- the recurring defect, converted into an invariant ------------------------
#: Mapping the pill while a paste is in flight hands a focus-stealing window
#: the keyboard, the chord lands in it, and `restore_clipboard` overwrites the
#: transcript. Across one review cycle this was found four times at three
#: different call sites - the busy notice, the same notice as a race, the
#: language notice, and the placement preview - each fixed by remembering to
#: consult the guard. Remembering is the wrong mechanism, so this asserts it
#: instead: only the guarded sender may put the pill back on screen.

PILL_CHOKEPOINT = "_send_overlay_locked"
#: The one state that cannot map the pill, so it needs no guard to send.
SAFE_STATES = {"hidden"}


def _pill_mapping_sends():
    """(function name, state) for every `self.overlay.send` that can map the pill."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "voice" / "daemon.py"
    tree = ast.parse(source.read_text())
    found = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            call = node.func
            if not (isinstance(call, ast.Attribute) and call.attr == "send"
                    and isinstance(call.value, ast.Attribute)
                    and call.value.attr == "overlay"):
                continue
            for arg in node.args:
                if not isinstance(arg, ast.Dict):
                    # Not a literal: it came from somewhere else and cannot be
                    # judged here, so it has to be the chokepoint's own send.
                    found.append((func.name, "<not a literal>"))
                    continue
                for key, value in zip(arg.keys, arg.values):
                    if getattr(key, "value", None) != "state":
                        continue
                    state = getattr(value, "value", "<computed>")
                    if state not in SAFE_STATES:
                        found.append((func.name, state))
    return found


def test_only_the_guarded_sender_can_put_the_pill_back_on_screen():
    offenders = [(fn, state) for fn, state in _pill_mapping_sends()
                 if fn != PILL_CHOKEPOINT]
    assert offenders == [], (
        "these send the pill a state that maps it, without going through "
        f"{PILL_CHOKEPOINT} and its mid-paste guard: {offenders}. Route them "
        "through _send_overlay(..., refuse_while_hidden=True) instead - a pill "
        "mapped during a paste takes the chord and the transcript is lost.")


def test_the_invariant_above_is_actually_looking_at_something():
    """A scan that finds nothing would pass for the wrong reason."""
    assert _pill_mapping_sends(), "the AST scan matched no pill sends at all"


# -- ask before the pill is on screen ----------------------------------------
#: The owner's idea, and a better one than asking at paste time: when the
#: dictate key is pressed the pill has not appeared yet, so the desktop still
#: names the window being dictated into. Asking later means asking while a
#: focus-stealing pill holds the keyboard, which is why the answer had to be
#: hidden-then-asked, timed against a settle, and distrusted in some modes.

def test_the_focused_window_is_captured_and_replayed():
    from voice.daemon import FocusedWindow

    live = ["konsole"]
    window = FocusedWindow(lambda: "a-command", run=lambda cmd, on_timeout=None: live[0])

    assert window() is None, "nothing asked for yet, so nothing to report"
    window.capture()
    live[0] = "the-pill"                      # the pill has taken focus since
    assert window() == "konsole"
    assert window() == "konsole", "the answer is replayed, not re-asked"


def test_a_capture_that_finds_nothing_reports_nothing():
    from voice.daemon import FocusedWindow

    window = FocusedWindow(lambda: "a-command", run=lambda cmd, on_timeout=None: None)
    window.capture()
    assert window() is None


def test_the_window_is_captured_before_the_pill_is_told_to_appear(isolated_xdg, qapp, monkeypatch):
    """Order matters: the pill takes the keyboard when it maps."""
    from voice.pipeline import State

    d = _focus_daemon(monkeypatch, window_command="echo konsole")
    order = []
    real_capture = d._focused_window.capture

    def capture():
        order.append("asked")
        real_capture()

    d._focused_window.capture = capture

    class FakeOverlay:
        def send(self, message): order.append(("pill", message.get("state")))
        def flush(self, timeout=1.0): return True
        def stop(self): pass
        def status(self): return "running"

    d.overlay = FakeOverlay()
    try:
        d._on_dictation_state(State.RECORDING)
        assert order and order[0] == "asked", \
            f"the pill was told to appear before the window was read: {order}"
        assert d._focused_window() == "konsole"
    finally:
        d.shutdown()


def test_an_unverifiable_paste_names_no_key_it_cannot_know_is_right():
    """The owner pasting into a browser was told to press Ctrl+Shift+V.

    Where the focused window cannot be read, neither chord can be recommended -
    a terminal wants Ctrl+Shift+V and a browser wants Ctrl+V, and naming either
    is wrong half the time. What IS true either way is that the transcript is
    on the clipboard, so that is all the pill claims.
    """
    from voice.daemon import DONE_UNVERIFIED, overlay_messages
    from voice.pipeline import State

    assert "Shift" not in DONE_UNVERIFIED and "Ctrl" not in DONE_UNVERIFIED
    msgs = overlay_messages(State.IDLE, "7 chars via paste-blind in 0.4s", "en",
                            blind_hint=DONE_UNVERIFIED)
    assert msgs == [{"state": "done", "text": "Copied"}]


def test_retrying_a_recording_reads_the_window_again(isolated_xdg, qapp, monkeypatch):
    """Retry happens minutes later, very possibly in a different window.

    Replaying the class captured for the original recording chose that window's
    chord AND reported the paste as verified, so `restore_clipboard` wiped the
    transcript after a chord the new window had discarded.
    """
    from voice.pipeline import State

    d = _focus_daemon(monkeypatch, window_command="echo konsole")
    try:
        d._focused_window.capture()
        assert d._focused_window() == "konsole"

        d.config.set("inject.active_window_command", "echo firefox")
        d.config.save()
        d._on_dictation_state(State.TRANSCRIBING, "retry")

        assert d._focused_window() == "firefox", \
            "a retry pasted using the window the original recording was made in"
    finally:
        d.shutdown()


def test_a_pill_that_never_takes_focus_reads_the_window_at_paste_time(isolated_xdg, qapp, monkeypatch):
    """Capturing early is a workaround for a pill that steals the keyboard.

    Where the pill cannot steal it - a real layer-shell surface - reading live
    just before the chord is strictly better: it follows the owner if they move
    to another window while the transcription runs.
    """
    d = _focus_daemon(monkeypatch, layer_shell=True, window_command="echo konsole")
    try:
        assert d._pill_policy == "none"
        # No capture has happened, and it still answers: it is reading now.
        assert d.injector._window_class() == "konsole"
    finally:
        d.shutdown()


def test_a_pill_that_steals_focus_uses_the_window_read_before_it_appeared(isolated_xdg, qapp, monkeypatch):
    d = _focus_daemon(monkeypatch, window_command="echo konsole")
    try:
        assert d._pill_policy == "hide"
        # Nothing captured yet, so nothing to report - it is not reading live,
        # because live would mean reading while the pill holds the keyboard.
        assert d.injector._window_class() is None
        d._focused_window.capture()
        assert d.injector._window_class() == "konsole"
    finally:
        d.shutdown()


def test_reading_the_window_never_blocks_the_key_that_started_the_recording():
    """The capture ran on the hotkey thread, holding the pipeline lock.

    On a box where the command takes 400 ms, the pill did not appear for 400 ms
    and a 150 ms push-to-talk tap could not stop before then.
    """
    import threading as _threading
    import time as _time

    from voice.daemon import FocusedWindow

    released = _threading.Event()

    def slow(cmd, on_timeout=None):
        released.wait(3)
        return "konsole"

    window = FocusedWindow(lambda: "a-slow-command", run=slow)
    started = _time.monotonic()
    window.capture()
    assert _time.monotonic() - started < 0.3, "capture blocked its caller"

    released.set()
    assert window() == "konsole", "the answer never arrived for the paste"


def test_a_window_command_that_gave_up_is_reported_as_itself(isolated_xdg, qapp, monkeypatch, caplog):
    """Not as "this desktop will not say which window has the keyboard".

    On a Plasma box that names the window perfectly well, a command that timed
    out once would otherwise be reported as a mute desktop - pointing the owner
    at the wrong fix, and contradicting what `voice doctor` tells them.
    """
    d = _focus_daemon(monkeypatch, window_command="a-command-that-hangs")
    try:
        d._focused_window._give_up("a-command-that-hangs")
        with caplog.at_level("WARNING", logger="voice.daemon"):
            d._warn_if_terminals_can_never_be_pasted_into(notify=False)

        assert "stopped answering" in caplog.text
        assert "will not say which window" not in caplog.text
    finally:
        d.shutdown()
