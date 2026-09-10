"""Wires every module together and runs the Qt event loop."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from voice import APP_NAME, __version__
from voice.audio.capture import Recorder, list_sources
from voice.config import Config, is_language_code
from voice.history import History
from voice.hotkey.evdev_listener import EvdevListener
from voice.hotkey.keyspec import KeySpec, Tracker, parse_keyspec
from voice.hotkey.portal_listener import PortalListener
from voice.inject.clipboard import Clipboard
from voice.inject.fallback import make_key_sender
from voice.inject.injector import Injector, run_window_command
from voice.ipc import (NEXT_LANGUAGE, PENDING_LANGUAGE, IPCError, Server, is_running,
                       send)
from voice.pipeline import Dictation, Services, State
from voice.stt import make_transcriber
from voice.stt.base import TranscriptionError
from voice.ui.notify import Notifier
from voice.ui.overlay_client import OverlayClient, default_launcher
from voice.ui.settings import SettingsDialog
from voice.ui.tray import Tray

log = logging.getLogger(__name__)

HOTKEY_BACKENDS = ("evdev", "portal")

#: Every hotkey the daemon binds, in both listeners. `language_toggle` is
#: handled here rather than in the pipeline: it changes settings, not state.
HOTKEY_NAMES = ("dictate", "recall", "cancel", "language_toggle")


SEAT_TIMEOUT_S = 2


def has_local_seat() -> bool:
    """Whether this login session owns a seat, i.e. a local screen and keyboard.

    A remote-desktop session has none - and that is exactly the case where
    /dev/input carries no keystrokes. When nothing can be determined we say True:
    guessing "remote" would move a working evdev setup onto the portal.
    """
    session = os.environ.get("XDG_SESSION_ID")
    if session:
        try:
            done = subprocess.run(["loginctl", "show-session", session, "-p", "Seat"],
                                  capture_output=True, text=True, timeout=SEAT_TIMEOUT_S)
            for line in done.stdout.splitlines():
                if line.startswith("Seat="):
                    return bool(line.split("=", 1)[1].strip())
        except Exception:
            log.debug("loginctl seat lookup failed", exc_info=True)
    seat = os.environ.get("XDG_SEAT")
    if seat is not None:
        return bool(seat.strip())
    return True


def keyboards_are_readable() -> bool:
    """True if at least one keyboard device can be opened (udev rule or input group)."""
    from voice.hotkey.evdev_listener import list_keyboards
    try:
        devices = list_keyboards()
    except Exception:
        log.debug("keyboard probe failed", exc_info=True)
        return False
    for dev in devices:                       # the probe must not hold the fds open
        try:
            dev.close()
        except Exception:
            log.debug("closing a probed device failed", exc_info=True)
    return bool(devices)


def choose_hotkey_backend(config: Config, keyboards_readable: bool, has_local_seat: bool) -> str:
    """Which hotkey listener to build. An unusable setting is treated as "auto"."""
    setting = str(config.get("hotkeys.backend", "auto") or "auto").strip().lower()
    if setting in HOTKEY_BACKENDS:
        return setting
    if setting != "auto":
        log.warning("unknown hotkeys.backend %r; choosing automatically", setting)
    return "evdev" if (keyboards_readable and has_local_seat) else "portal"


def portal_shortcuts(config: Config) -> dict[str, str]:
    """The shortcut ids to bind through the portal, with their XDG triggers."""
    shortcuts = {}
    for name in HOTKEY_NAMES:
        trigger = config.portal_trigger(name)
        if trigger:
            shortcuts[name] = trigger
    return shortcuts


def hotkey_specs(config: Config) -> dict[str, KeySpec]:
    specs = {}
    for name in HOTKEY_NAMES:
        text = config.get(f"hotkeys.{name}", "") or ""
        try:
            specs[name] = parse_keyspec(text)
        except ValueError as exc:
            log.warning("ignoring hotkeys.%s: %s", name, exc)
            specs[name] = parse_keyspec("")
    return specs


#: Pipeline details that mean "there is nothing to show": the recording is
#: gone and the pill must come off the screen at once.
NOTHING_TO_SHOW = ("cancelled", "too short", "empty")

#: The detail the pipeline uses for the IDLE that immediately follows an error.
#: The pill is showing the message and times itself out; hiding it here would
#: replace a two-second explanation with nothing.
AFTER_ERROR = "after error"


def overlay_messages(state: State, detail: str, language: str) -> list[dict]:
    """The pill protocol for one pipeline transition, in order.

    Pure so the mapping can be read (and tested) without a daemon: the states
    the user must see are recording, transcribing, the checkmark and errors.
    """
    if state is State.RECORDING:
        # The badge first, so the pill never appears showing the old language.
        return [{"language": language}, {"state": "recording"}]
    if state is State.TRANSCRIBING:
        return [{"state": "transcribing"}]
    if state is State.INJECTING:
        # Nothing: the checkmark belongs to the IDLE that follows a successful
        # insertion. Saying `done` here too sent it twice per dictation.
        return []
    if state is State.ERROR:
        return [{"state": "error", "text": detail or "dictation failed"}]
    if state is State.IDLE:
        if detail in NOTHING_TO_SHOW:
            return [{"state": "hidden"}]
        if detail and detail != AFTER_ERROR:
            return [{"state": "done"}]        # insertion finished; helper hides itself
    return []


def window_class_getter(config: Config) -> Callable[[], str | None]:
    return lambda: run_window_command(config.get("inject.active_window_command", "") or "")


class _BrokenTranscriber:
    """Placeholder used when the active stt profile can't be built; keeps the daemon alive."""

    name = "broken"

    def __init__(self, reason: str):
        self._reason = reason

    def describe(self) -> str:
        return self._reason

    def warmup(self) -> None:
        pass

    def transcribe(self, *args, **kwargs):
        raise TranscriptionError(self._reason)


class _Bridge(QObject):
    """Hops work from the IPC thread onto the Qt thread.

    Everything reachable from `handle` that mutates state, touches the config file
    or drives Tray's QMenu/QAction must travel through one of these signals.
    """

    open_settings = Signal()
    apply_config = Signal()
    set_profile = Signal(str)
    set_language = Signal(str)
    quit = Signal()


class Daemon:
    def __init__(self, config: Config, *, listener=None, recorder=None, clipboard=None, sender=None,
                 notifier=None, tray=None):
        self.config = config
        self._listener_override = listener
        # The pill is built in build(); the recorder is not, so it forwards
        # levels through this daemon rather than holding the client itself.
        self.overlay: OverlayClient | None = None
        self._recorder = recorder or Recorder(on_level=self._on_level)
        self._clipboard = clipboard or Clipboard()
        self._sender = sender
        self._notifier = notifier or Notifier(bool(config.get("general.notifications", True)))
        self._tray = tray
        self._server: Server | None = None
        self._settings: SettingsDialog | None = None
        self._active_profile: tuple[str, dict] | None = None
        #: The language the pill was last told about; see _send_overlay.
        self._overlay_language = str(config.get("general.language", "en") or "en")
        #: The [ui] settings the running helper was started with; see _make_overlay.
        self._overlay_settings: tuple | None = None
        self._bridge = _Bridge()

    # -- construction -------------------------------------------------------
    def build(self) -> None:
        self.tracker = Tracker(hotkey_specs(self.config))
        self.history = History()
        self.listener, self.hotkey_backend = self._make_listener()
        self._sender = self._sender or make_key_sender()
        self.injector = Injector(self._clipboard, self._sender, self.config.get("inject", {}) or {},
                                 self.listener.modifiers_held, window_class_getter(self.config))
        self._active_profile = self._profile_snapshot()
        services = Services(recorder=self._recorder, transcriber=self._make_transcriber(),
                            injector=self.injector, history=self.history, notify=self._notifier.notify,
                            config_getter=self.config.get, prompt_getter=self._prompt)
        self.dictation = Dictation(services)
        self.tray = self._tray or Tray(self._on_tray_action)
        self.overlay = self._make_overlay()
        self.dictation.on_state = self._on_dictation_state
        self.tray.set_profiles(list(self.config.get("stt.profiles", {}) or {}), self.config.get("stt.active"))
        self.tray.set_languages(self.config.languages(), self.config.get("general.language"))
        self._bridge.open_settings.connect(self.open_settings)
        self._bridge.apply_config.connect(self.apply_config)
        self._bridge.set_profile.connect(self._set_profile)
        self._bridge.set_language.connect(self._set_language)
        self._bridge.quit.connect(self._quit)
        self._server = Server(self.handle)

    def _make_overlay(self) -> OverlayClient:
        """The recording pill's supervisor. Disabled means: never spawn anything."""
        enabled = bool(self.config.get("ui.overlay", True))
        position = str(self.config.get("ui.overlay_position", "bottom") or "bottom")
        # The helper is started with --lang, so that is the badge it already
        # shows: what we track here is the last language it was *told*. A
        # respawn reads it again rather than the one baked in at build time.
        self._overlay_language = str(self.config.get("general.language", "en") or "en")
        self._overlay_settings = self._overlay_snapshot()
        verbose = log.isEnabledFor(logging.DEBUG)
        allow_fallback = bool(self.config.get("ui.overlay_allow_fallback", False))
        return OverlayClient(enabled, launcher=lambda: default_launcher(
            position=position, lang=self._overlay_language, verbose=verbose,
            allow_fallback=allow_fallback))

    def _overlay_snapshot(self) -> tuple:
        """Everything _make_overlay bakes into the helper's command line.

        Not the language: that travels as a message to the running helper, and
        restarting the pill for it would take it off the screen mid-notice.
        """
        return (dict(self.config.get("ui", {}) or {}), log.isEnabledFor(logging.DEBUG))

    def _rebuild_overlay_if_needed(self) -> None:
        """A [ui] change needs a new helper; anything else leaves it running."""
        if self.overlay is None or self._overlay_snapshot() == self._overlay_settings:
            return
        log.info("recording overlay settings changed; restarting the helper")
        self.overlay.stop()
        self.overlay = self._make_overlay()
        self.overlay.start()

    def _on_level(self, level: float) -> None:
        """Audio reader thread. Must not block: the client queues and returns."""
        if self.overlay is not None:
            self.overlay.send({"level": round(float(level), 3)})

    def _on_dictation_state(self, state: State, detail: str = "") -> None:
        """One pipeline transition, to the tray and to the pill.

        The tray comes first and by signal, as before; the pill is decoration
        and its client swallows every failure, so neither can delay the other.
        """
        self.tray.state_changed.emit(state.value, detail)
        if self.overlay is None:
            return
        language = str(self.config.get("general.language", "en") or "en")
        self._send_overlay(overlay_messages(state, detail, language), language)

    def _send_overlay(self, messages: list[dict], language: str) -> None:
        """Send one transition's messages, badge first when it has changed.

        The pill draws the badge from the last language it was told about, and
        the language can change from anywhere - the toggle, the tray, the
        settings dialog - between two states. So every state message is
        preceded by the language whenever it has moved since the last one sent.
        """
        for message in messages:
            if "language" in message:
                self._overlay_language = str(message["language"])
            elif "state" in message and language != self._overlay_language:
                self.overlay.send({"language": language})
                self._overlay_language = language
            self.overlay.send(message)

    def _sync_overlay_language(self) -> None:
        """Tell a pill that is already on screen about a language changed elsewhere."""
        if self.overlay is None:
            return
        language = str(self.config.get("general.language", "en") or "en")
        if language != self._overlay_language:
            self.overlay.send({"language": language})
            self._overlay_language = language

    def _make_listener(self):
        """The hotkey listener plus the name of the backend it represents.

        Everything downstream talks to `self.listener` only, so the two backends
        stay interchangeable.
        """
        if self._listener_override is not None:
            # Injected by tests: report what the config asks for without probing
            # devices or spawning loginctl.
            return self._listener_override, choose_hotkey_backend(self.config, True, True)
        backend = choose_hotkey_backend(self.config, keyboards_are_readable(), has_local_seat())
        log.info("hotkey backend: %s", backend)
        if backend == "portal":
            return PortalListener(self._on_hotkey, portal_shortcuts(self.config),
                                  on_ready=self._on_hotkeys_ready), backend
        return EvdevListener(self.tracker, self._on_hotkey), backend

    def _on_hotkeys_ready(self, ok: bool) -> None:
        """Called from the portal listener thread once the desktop has answered."""
        if ok:
            return
        self._notifier.notify("Shortcut not registered",
                              "The desktop did not bind the global shortcut. Check its shortcut settings, "
                              "check hotkeys.portal_dictate, or run install.sh so the portal can resolve "
                              "this app.", "critical")

    def _profile_snapshot(self) -> tuple[str, dict] | None:
        """The active (name, profile) pair, or None if stt.active is unresolvable."""
        try:
            return self.config.stt_profile()
        except Exception:
            return None

    def _make_transcriber(self):
        try:
            name, profile = self.config.stt_profile()
            t = make_transcriber(profile, self.config.secret(profile))
            log.info("transcriber: %s (%s)", name, t.describe())
            return t
        except Exception as exc:
            log.exception("failed to build transcriber")
            self._notifier.notify("Transcription profile problem", str(exc), "critical")
            return _BrokenTranscriber(str(exc))

    def _prompt(self) -> str | None:
        # Runs on the worker just before transcribe(). A broken active profile must
        # not raise here: that bypasses the TranscriptionError path and the audio
        # would be discarded instead of kept for a retry.
        try:
            return self.config.stt_profile()[1].get("prompt") or None
        except Exception:
            log.debug("no prompt available for the active profile", exc_info=True)
            return None

    # -- runtime ------------------------------------------------------------
    def run(self) -> int:
        if is_running():
            return self._hand_over()
        app = QApplication.instance() or QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName(APP_NAME)
        self.build()
        try:
            self._server.start()
        except IPCError:
            # Another instance claimed the socket between the check above and the
            # bind. Hand over without starting the listener or touching its socket.
            return self._hand_over()
        self.listener.start()
        self.overlay.start()
        self.tray.show()
        self._start_warmup()
        # The portal listener answers asynchronously and reports through
        # _on_hotkeys_ready; only the evdev listener knows its state by now.
        if self.hotkey_backend != "portal" and self.listener.devices_ok() is False:
            self._notifier.notify("No keyboard access", "Run the installer's udev step or add yourself to the input group.", "critical")
        log.info("%s %s ready", APP_NAME, __version__)
        code = app.exec()
        self.shutdown()
        return code

    def _hand_over(self) -> int:
        """Defer to the daemon that already owns the socket."""
        log.info("%s already running; opening settings instead", APP_NAME)
        try:
            send({"cmd": "settings"})
        except IPCError:
            pass
        return 0

    def _start_warmup(self) -> None:
        # Its own thread, never the pipeline pool: a model load takes tens of
        # seconds and that pool has a single worker, so recall/retry would sit
        # behind it with their state already reserved and paste on completion.
        threading.Thread(target=self._warmup, name="warmup", daemon=True).start()

    def _warmup(self) -> None:
        try:
            self.dictation.sv.transcriber.warmup()
            reason = getattr(self.dictation.sv.transcriber, "fallback_reason", None)
            if reason:
                self._notifier.notify("Running on CPU", reason, "normal")
            if self.dictation.state == State.IDLE:
                self.tray.state_changed.emit("idle", self.dictation.sv.transcriber.describe())
        except Exception as exc:
            log.exception("warmup failed")
            self._notifier.notify("Model failed to load", str(exc), "critical")

    def shutdown(self) -> None:
        """Give every thread we own its marching orders, in order, bounded.

        Nothing here may raise or block: the socket is unlinked at the end, so
        anything that hangs after that point leaves a daemon nobody can reach
        and nobody can quit.
        """
        try:
            self.dictation.cancel()
        except Exception:
            log.exception("failed to cancel in-flight recording during shutdown")
        try:
            self.listener.stop()
        except Exception:
            log.exception("failed to stop listener during shutdown")
        if self.overlay is not None:
            self.overlay.stop()
        try:
            # The pool's worker is not a daemon thread: left running it holds
            # the interpreter open at exit, long after the socket is gone.
            self.dictation.shutdown()
        except Exception:
            log.exception("failed to stop the dictation worker during shutdown")
        if self._server:
            self._server.stop()

    def apply_config(self) -> None:
        """Re-reads config. Runs on the Qt thread only (see _Bridge.apply_config)."""
        try:
            self.config.reload()
        except ValueError as exc:
            # Unparseable TOML: the CLI has already printed "ok", so the only way
            # the user learns nothing was applied is this notification.
            log.warning("config reload failed, keeping previous settings: %s", exc)
            self._notifier.notify("Config error, keeping previous settings", str(exc), "critical")
            return
        self.tracker.set_specs(hotkey_specs(self.config))
        self._notifier.set_enabled(bool(self.config.get("general.notifications", True)))
        current = self._profile_snapshot()
        if current != self._active_profile:
            self._active_profile = current
            self.dictation.set_transcriber(self._make_transcriber())
            self._start_warmup()
        self.injector = Injector(self._clipboard, self._sender, self.config.get("inject", {}) or {},
                                 self.listener.modifiers_held, window_class_getter(self.config))
        self.dictation.set_injector(self.injector)
        self.tray.set_profiles(list(self.config.get("stt.profiles", {}) or {}), self.config.get("stt.active"))
        self.tray.set_languages(self.config.languages(), self.config.get("general.language"))
        self._rebuild_overlay_if_needed()
        self._sync_overlay_language()

    def _set_profile(self, name: str) -> None:
        """Qt thread: persist the profile switch, then apply it."""
        try:
            self.config.set("stt.active", name)
            self.config.save()
        except Exception as exc:
            log.exception("could not save the profile switch")
            self._notifier.notify("Could not save settings", str(exc), "critical")
            return
        self.apply_config()

    def _set_language(self, code: str) -> None:
        """Qt thread: persist the language switch, apply it, then show it.

        `code` may be the literal "next": the cycle is resolved *here*, on the
        thread that owns the config, so two toggles queued before this drains
        are two steps rather than the same one twice.
        """
        previous = str(self.config.get("general.language", "en") or "en")
        code = self._next_language() if code == NEXT_LANGUAGE else code
        if not code or code == previous:
            # Nowhere to go (a cycle with one entry, or the language asked for
            # is already in force). Saving and flashing "EN → EN" is worse than
            # doing nothing at all.
            return
        try:
            self.config.set("general.language", code)
            self.config.save()
        except Exception as exc:
            log.exception("could not save the language switch")
            self._notifier.notify("Could not save settings", str(exc), "critical")
            return
        self.apply_config()             # sends the new badge (_sync_overlay_language)
        if self.overlay is not None:
            self.overlay.send({"state": "notice",
                               "text": f"{previous.upper()} \u2192 {code.upper()}"})

    def _next_language(self) -> str | None:
        """The next language in the cycle, or None when there is nowhere to go.

        A language outside the cycle enters it at the first entry; a cycle that
        would step onto the language already in force (it has a single entry)
        is not a cycle, and the toggle does nothing.
        """
        cycle = self.config.languages()
        if not cycle:
            return None
        current = str(self.config.get("general.language", "en") or "en")
        target = (cycle[0] if current not in cycle
                  else cycle[(cycle.index(current) + 1) % len(cycle)])
        return None if target == current else target

    # -- events ---------------------------------------------------------------
    def _on_hotkey(self, name: str, kind: str) -> None:
        if name == "language_toggle":
            # Not a dictation key: it edits settings, so it goes through the
            # same IPC path as `voice language next` and lands on the Qt thread.
            if kind == "press":
                self.handle({"cmd": "language", "code": "next"})
            return
        self.dictation.on_hotkey(name, kind)

    def _on_tray_action(self, action: str) -> None:
        if action.startswith("profile:"):
            self.handle({"cmd": "profile", "name": action.split(":", 1)[1]})
        elif action.startswith("language:"):
            self.handle({"cmd": "language", "code": action.split(":", 1)[1]})
        else:
            self.handle({"cmd": action})

    def open_settings(self) -> None:
        try:
            if self._settings is None:
                self._settings = SettingsDialog(self.config, self.listener.capture_next, list_sources,
                                                backend=self.hotkey_backend)
                self._settings.saved.connect(self.apply_config)
            elif not self._settings.isVisible():
                # The dialog holds its own Config; refresh it so a reopen shows what
                # is actually in force rather than edits abandoned last time. Only
                # while it is off screen: a second `voice settings` or tray click on
                # an open dialog must raise the user's edits, not discard them.
                self._settings.reload_from_disk()
        except ValueError as exc:
            log.warning("cannot open settings: %s", exc)
            self._notifier.notify("Config error", str(exc), "critical")
            return
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
                    "version": __version__, "keyboard": self.listener.devices_ok(),
                    "hotkey_backend": self.hotkey_backend,
                    "language": self.config.get("general.language"),
                    "overlay": self.overlay.status() if self.overlay else "off"}
        if cmd == "profile":
            name = request.get("name", "")
            if name not in (self.config.get("stt.profiles", {}) or {}):
                return {"ok": False, "error": f"unknown profile '{name}'"}
            # Validated here, but mutated and saved on the Qt thread: writing the
            # config file from the IPC thread races the settings dialog.
            self._bridge.set_profile.emit(name)
            return {"ok": True, "profile": name}
        if cmd == "language":
            code = str(request.get("code", "")).strip().lower()
            if code == NEXT_LANGUAGE:
                # Deliberately not resolved here: see _set_language. The caller
                # is told so and reads the result back with `status`.
                self._bridge.set_language.emit(NEXT_LANGUAGE)
                return {"ok": True, "language": PENDING_LANGUAGE}
            if not is_language_code(code):
                return {"ok": False, "error": f"unknown language {code!r}"}
            # Validated here, written and applied on the Qt thread: same rule as
            # `profile`, because both touch the config file.
            self._bridge.set_language.emit(code)
            return {"ok": True, "language": code}
        if cmd == "reload":
            self._bridge.apply_config.emit()
            return {"ok": True}
        if cmd == "settings":
            self._bridge.open_settings.emit()
            return {"ok": True}
        if cmd == "quit":
            self._bridge.quit.emit()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def main() -> int:
    try:
        config = Config.load()
    except ValueError as exc:
        # Started from a .desktop entry there is no terminal to read a traceback in,
        # so say it once on stderr and once on the desktop, then give up cleanly.
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        Notifier().notify("Config error", str(exc), "critical")
        return 2
    errs = config.errors()
    if errs:
        log.warning("config problems: %s", "; ".join(errs))
    return Daemon(config).run()
