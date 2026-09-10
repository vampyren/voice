"""Wires every module together and runs the Qt event loop."""
from __future__ import annotations

import logging
import sys
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from voice import APP_NAME, __version__
from voice.audio.capture import Recorder, list_sources
from voice.config import Config
from voice.history import History
from voice.hotkey.evdev_listener import EvdevListener
from voice.hotkey.keyspec import KeySpec, Tracker, parse_keyspec
from voice.inject.clipboard import Clipboard
from voice.inject.fallback import make_key_sender
from voice.inject.injector import Injector, run_window_command
from voice.ipc import Server
from voice.pipeline import Dictation, Services, State
from voice.stt import make_transcriber
from voice.ui.notify import Notifier
from voice.ui.settings import SettingsDialog
from voice.ui.tray import Tray

log = logging.getLogger(__name__)


def hotkey_specs(config: Config) -> dict[str, KeySpec]:
    specs = {}
    for name in ("dictate", "recall", "cancel"):
        text = config.get(f"hotkeys.{name}", "") or ""
        try:
            specs[name] = parse_keyspec(text)
        except ValueError as exc:
            log.warning("ignoring hotkeys.%s: %s", name, exc)
            specs[name] = parse_keyspec("")
    return specs


def window_class_getter(config: Config) -> Callable[[], str | None]:
    return lambda: run_window_command(config.get("inject.active_window_command", "") or "")


class _Bridge(QObject):
    open_settings = Signal()
    quit = Signal()


class Daemon:
    def __init__(self, config: Config, *, listener=None, recorder=None, clipboard=None, sender=None,
                 notifier=None, tray=None):
        self.config = config
        self._listener_override = listener
        self._recorder = recorder or Recorder()
        self._clipboard = clipboard or Clipboard()
        self._sender = sender
        self._notifier = notifier or Notifier(bool(config.get("general.notifications", True)))
        self._tray = tray
        self._server: Server | None = None
        self._settings: SettingsDialog | None = None
        self._bridge = _Bridge()

    # -- construction -------------------------------------------------------
    def build(self) -> None:
        self.tracker = Tracker(hotkey_specs(self.config))
        self.history = History()
        self.listener = self._listener_override or EvdevListener(self.tracker, self._on_hotkey)
        sender = self._sender or make_key_sender()
        self.injector = Injector(self._clipboard, sender, self.config.get("inject", {}) or {},
                                 self.listener.modifiers_held, window_class_getter(self.config))
        services = Services(recorder=self._recorder, transcriber=self._make_transcriber(),
                            injector=self.injector, history=self.history, notify=self._notifier.notify,
                            config_getter=self.config.get, prompt_getter=self._prompt)
        self.dictation = Dictation(services)
        self.tray = self._tray or Tray(self._on_tray_action)
        self.dictation.on_state = lambda s, d: self.tray.state_changed.emit(s.value, d)
        self.tray.set_profiles(list(self.config.get("stt.profiles", {}) or {}), self.config.get("stt.active"))
        self._bridge.open_settings.connect(self.open_settings)
        self._bridge.quit.connect(self._quit)
        self._server = Server(self.handle)

    def _make_transcriber(self):
        name, profile = self.config.stt_profile()
        t = make_transcriber(profile, self.config.secret(profile))
        log.info("transcriber: %s (%s)", name, t.describe())
        return t

    def _prompt(self) -> str | None:
        return self.config.stt_profile()[1].get("prompt") or None

    # -- runtime ------------------------------------------------------------
    def run(self) -> int:
        app = QApplication.instance() or QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName(APP_NAME)
        self.build()
        self._server.start()
        self.listener.start()
        self.tray.show()
        from voice.pipeline import _thread_executor
        _thread_executor(self._warmup)
        if self.listener.devices_ok() is False:
            self._notifier.notify("No keyboard access", "Run the installer's udev step or add yourself to the input group.", "critical")
        log.info("%s %s ready", APP_NAME, __version__)
        code = app.exec()
        self.shutdown()
        return code

    def _warmup(self) -> None:
        try:
            self.dictation.sv.transcriber.warmup()
            reason = getattr(self.dictation.sv.transcriber, "fallback_reason", None)
            if reason:
                self._notifier.notify("Running on CPU", reason, "normal")
            self.tray.state_changed.emit("idle", self.dictation.sv.transcriber.describe())
        except Exception as exc:
            log.exception("warmup failed")
            self._notifier.notify("Model failed to load", str(exc), "critical")

    def shutdown(self) -> None:
        try:
            self.listener.stop()
        except Exception:
            pass
        if self._server:
            self._server.stop()

    def apply_config(self) -> None:
        self.config.reload()
        self.tracker.set_specs(hotkey_specs(self.config))
        self._notifier.set_enabled(bool(self.config.get("general.notifications", True)))
        try:
            self.dictation.set_transcriber(self._make_transcriber())
        except Exception as exc:
            self._notifier.notify("Transcription profile problem", str(exc), "critical")
        self.injector = Injector(self._clipboard, self.injector._sender, self.config.get("inject", {}) or {},
                                 self.listener.modifiers_held, window_class_getter(self.config))
        self.dictation.set_injector(self.injector)
        self.tray.set_profiles(list(self.config.get("stt.profiles", {}) or {}), self.config.get("stt.active"))
        from voice.pipeline import _thread_executor
        _thread_executor(self._warmup)

    # -- events ---------------------------------------------------------------
    def _on_hotkey(self, name: str, kind: str) -> None:
        self.dictation.on_hotkey(name, kind)

    def _on_tray_action(self, action: str) -> None:
        if action.startswith("profile:"):
            self.handle({"cmd": "profile", "name": action.split(":", 1)[1]})
        else:
            self.handle({"cmd": action})

    def open_settings(self) -> None:
        if self._settings is None:
            self._settings = SettingsDialog(self.config, self.listener.capture_next, list_sources)
            self._settings.saved.connect(self.apply_config)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    def _quit(self) -> None:
        app = QApplication.instance()
        if app:
            app.quit()

    # -- ipc ----------------------------------------------------------------
    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        d = self.dictation
        simple = {"start": d.start, "stop": d.stop, "toggle": d.toggle, "cancel": d.cancel,
                  "recall": d.recall, "retry": d.retry}
        if cmd == "ping":
            return {"ok": True}
        if cmd in simple:
            simple[cmd]()
            return {"ok": True, "state": d.state.value}
        if cmd == "status":
            return {"ok": True, "state": d.state.value, "profile": self.config.get("stt.active"),
                    "backend": d.sv.transcriber.describe(), "last_error": d.last_error,
                    "version": __version__, "keyboard": self.listener.devices_ok()}
        if cmd == "profile":
            name = request.get("name", "")
            if name not in (self.config.get("stt.profiles", {}) or {}):
                return {"ok": False, "error": f"unknown profile '{name}'"}
            self.config.set("stt.active", name)
            self.config.save()
            self.apply_config()
            return {"ok": True, "profile": name}
        if cmd == "reload":
            self.apply_config()
            return {"ok": True}
        if cmd == "settings":
            self._bridge.open_settings.emit()
            return {"ok": True}
        if cmd == "quit":
            self._bridge.quit.emit()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def main() -> int:
    config = Config.load()
    errs = config.errors()
    if errs:
        log.warning("config problems: %s", "; ".join(errs))
    return Daemon(config).run()
