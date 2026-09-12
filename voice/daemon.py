"""Wires every module together and runs the Qt event loop."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from voice import APP_NAME, __version__
from voice.audio.capture import Recorder, capture_sources
from voice.config import Config, is_language_code
from voice.history import History
from voice.hotkey.evdev_listener import EvdevListener
from voice.hotkey.keyspec import KeySpec, Tracker, parse_keyspec
from voice.hotkey.portal_listener import STATE_BOUND, STATE_UNASSIGNED, PortalListener
from voice.inject.clipboard import Clipboard
from voice.inject.fallback import make_key_sender
from voice.inject.injector import (BLIND_PASTE, WINDOW_COMMAND_TIMEOUT_S, Injector,
                                   insertion_status, pill_policy, pill_settle_s,
                                   run_window_command)
from voice.inject.window import effective_window_command, terminal_chord_is_unreachable
from voice.ipc import (NEXT_LANGUAGE, PENDING_LANGUAGE, IPCError, Server, is_running,
                       send)
from voice.pipeline import Dictation, Services, State, detail_method
from voice.stt import make_transcriber
from voice.stt.base import TranscriptionError
from voice.ui.notify import Notifier
from voice.ui.overlay_client import (OverlayClient, cached_probe, default_launcher,
                                     pill_takes_focus)
#: How long the pill's fill takes to run to the end of its track. The helper's
#: own constant: this animation happens in another process, and the daemon has
#: to know when it has landed to sequence the paste around it.
from voice.ui.overlay_model import FINISH as PILL_FILL_S
from voice.ui.placement import (LEGACY_POSITIONS, POSITIONS, is_margin,
                                 normalise_position)
from voice.ui.settings import SettingsDialog
from voice.ui.tray import Tray

log = logging.getLogger(__name__)

HOTKEY_BACKENDS = ("evdev", "portal")

#: Every hotkey the daemon binds, in both listeners. `language_toggle` is
#: handled here rather than in the pipeline: it changes settings, not state.
HOTKEY_NAMES = ("dictate", "recall", "cancel", "language_toggle")


SEAT_TIMEOUT_S = 2


def profile_for_status(config: Config) -> tuple[str, str | None]:
    """The active profile and the language that selected it.

    The language is None whenever the active profile is not the one
    general.language maps to - no map at all, or a `voice profile` switch away
    from it - so the status line cannot claim a language chose a model it did
    not. It is a comparison, not a record of who set it: picking exactly the
    mapped profile by hand reads the same as the language having chosen it.
    """
    active = str(config.get("stt.active", "") or "")
    language = str(config.get("general.language", "en") or "en")
    mapped = config.profile_for_language(language)
    return active, (language if mapped and mapped == active else None)


def profile_hint(config: Config) -> str:
    """The tray tooltip's profile fragment: "local-swedish (for sv)"."""
    active, language = profile_for_status(config)
    return f"{active} (for {language})" if language else active


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

#: Insertion methods that leave the text on the clipboard rather than in the
#: window: `inject.mode = "clipboard"` (asked for), a paste that failed, and a
#: pill that would have taken the chord. All three end in an IDLE the pill used
#: to celebrate with a checkmark and "Inserted", while the user still had to
#: press Ctrl+V themselves.
CLIPBOARD_METHODS = ("clipboard", "clipboard-only", "clipboard-pill")
#: What the pill says instead. Short: it is drawn inside the 132 px well.
DONE_COPIED = "Copied · Ctrl+V"


#: What the pill says when the paste could not be confirmed to have landed.
#: Deliberately names no key. Where the focused window cannot be read, neither
#: chord can be recommended - a terminal pastes with Ctrl+Shift+V and a browser
#: with Ctrl+V, so naming either is wrong half the time, and the owner was duly
#: told to press Ctrl+Shift+V while pasting into a browser. What is true in
#: both cases is that the transcript is on the clipboard: say only that.
DONE_UNVERIFIED = "Copied"

#: What the pill says when the dictate key is pressed with nothing to toggle.
#: The press used to vanish - no sound, no notification, no change on screen -
#: so the owner pressed again, and the second press landed after the pipeline
#: had gone idle and started a recording instead of stopping one. `notice`
#: shows for NOTICE_TTL and then goes back to whatever was on screen, which is
#: exactly the shape this needs: an answer, not a state.
#: ERROR is deliberately absent: `_fail` enters and leaves it inside one call,
#: having already raised a "Dictation failed" notification, and "Still working"
#: on top of that contradicts what the owner was just told.
BUSY_NOTICES = {State.TRANSCRIBING: "Still working",
                State.INJECTING: "Still pasting"}


def busy_messages(state: State, policy: str) -> list[dict]:
    """What to tell the pill about a dictate press that could not be acted on.

    Pure, like `overlay_messages`, so the wording can be read and tested
    without a daemon. IDLE and RECORDING return nothing: they act on a press,
    so they never reach here, and a notice on either would be a lie.

    Nothing at all is said while INJECTING under the `hide` policy. The
    injector has taken a focus-stealing pill off screen precisely so the paste
    chord reaches the owner's window; a notice maps it again, the pill takes
    the keyboard back, the chord lands in it, and `restore_clipboard` then puts
    the old clipboard back over the transcript. Answering the press is worth a
    good deal, but not the text it was asking about.
    """
    if state is State.INJECTING and policy == "hide":
        return []
    text = BUSY_NOTICES.get(state)
    return [{"state": "notice", "text": text}] if text else []

#: The detail on the INJECTING that re-inserts an entry from the history.
#: There was no transcription and no pill, so there is no fill to finish and
#: nothing for the paste to wait for.
RECALL = "recall"

#: The detail on the TRANSCRIBING that re-runs the last kept recording.
RETRY = "retry"

#: How long the settings window's placement preview stays on screen. The
#: owner's own number: "show the position on the desktop for say 5 sec".
PREVIEW_SECONDS = 5.0
#: Something for the preview's waveform to show, so it reads as the real pill
#: rather than a flat capsule. One burst: the helper's model decays it.
PREVIEW_LEVELS = (0.2, 0.55, 0.35, 0.8, 0.45, 0.7, 0.3)


def _single_shot(seconds: float, done: Callable[[], None]):
    """A Qt timer that calls `done` once, on the Qt thread. Cancellable.

    Built here rather than with QTimer.singleShot so the preview can be ended
    early - a real dictation must not be interrupted five seconds later by a
    timer belonging to a pill that is no longer on screen.
    """
    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(done)
    timer.start(int(seconds * 1000))
    # The QTimer itself is kept by the closure: dropped, it is garbage collected
    # and never fires. `cancel` is the one thing the daemon asks of it.
    return type("_SingleShot", (), {"cancel": lambda self: timer.stop()})()


def overlay_messages(state: State, detail: str, language: str,
                     finish_fill: bool = False, blind_hint: str = "") -> list[dict]:
    """The pill protocol for one pipeline transition, in order.

    Pure so the mapping can be read (and tested) without a daemon: the states
    the user must see are recording, transcribing, the checkmark and errors.

    `finish_fill` is the daemon saying that this insertion is going to take the
    pill off screen for its paste chord (see `pill_policy`); the fill is then
    told to run to the end of its track first, because unmapping the pill
    mid-sweep is what leaves the bar stopped in the middle.

    `blind_hint` is what to say instead of a bare checkmark when the paste
    could not be confirmed to have reached anything - see `BLIND_PASTE`. Empty
    means say nothing extra, because there is nothing useful to suggest.
    """
    if state is State.RECORDING:
        # The badge first, so the pill never appears showing the old language.
        return [{"language": language}, {"state": "recording"}]
    if state is State.TRANSCRIBING:
        return [{"state": "transcribing"}]
    if state is State.INJECTING:
        # No checkmark: that belongs to the IDLE that follows a successful
        # insertion, and saying `done` here too sent it twice per dictation.
        # The fill's run to the end is a different thing, and it starts here -
        # as early as the transcript exists - so that the clipboard work and
        # the wait for the modifiers are paid out of it rather than added to
        # it. A recall never had a transcription, so it has nothing to finish.
        return [{"finish": True}] if finish_fill and detail != RECALL else []
    if state is State.ERROR:
        return [{"state": "error", "text": detail or "dictation failed"}]
    if state is State.IDLE:
        if detail in NOTHING_TO_SHOW:
            return [{"state": "hidden"}]
        if detail and detail != AFTER_ERROR:
            # Insertion finished; the helper hides itself. What it says depends
            # on whether anything was actually inserted.
            method = detail_method(detail)
            if method in CLIPBOARD_METHODS:
                return [{"state": "done", "text": DONE_COPIED}]
            if method == BLIND_PASTE and blind_hint:
                # The chord went out and nothing can say where it landed. A
                # bare checkmark here is the thing that made a dictation look
                # lost: it reads as "inserted" whether or not anything was.
                return [{"state": "done", "text": blind_hint}]
            return [{"state": "done"}]
    return []


class FocusedWindow:
    """Asks the desktop what has the keyboard, and stops asking if it will not say.

    One timeout disables it for the rest of the session. The command runs
    between the pill being unmapped and the chord being sent, so one that hangs
    costs *every* dictation its full timeout - and one that is secretly
    interactive would be worse still. KWin's `queryWindowInfo` is the live
    example: it may be a window *picker* rather than a query, in which case it
    would put a crosshair grab in front of every paste. Losing the terminal
    chord is much the lesser harm, and the pill says so out loud rather than
    pretending the paste landed.

    A command that answers "nothing is focused" is not a failure and keeps
    being asked; only a command that does not answer at all is given up on.
    """

    def __init__(self, command: Callable[[], str],
                 run: Callable[..., str | None] = run_window_command):
        self._command, self._run = command, run
        #: The command that stopped answering, or None. Keyed to the string
        #: rather than a bare flag: the give-up message tells the owner to set
        #: a working `inject.active_window_command`, and a flag that outlived
        #: the command it was about short-circuited the replacement too - every
        #: paste stayed blind until a restart nothing told them to perform.
        self._gave_up_on: str | None = None
        #: The focused window as it was when this dictation began. Replayed at
        #: paste time rather than re-read, because by then the pill has it.
        self._captured: str | None = None
        self._reader: threading.Thread | None = None

    @property
    def usable(self) -> bool:
        """False while the configured command is one that stopped answering."""
        return self._gave_up_on is None or self._gave_up_on != self._command()

    def live(self) -> str | None:
        """Read the focused window right now.

        The better answer wherever it can be trusted - it follows the owner if
        they move to another window while the transcription runs. It cannot be
        trusted while a focus-stealing pill is on screen, which is what
        `capture` exists for.
        """
        cmd = self._command()
        if not cmd or cmd == self._gave_up_on:
            return None
        return self._run(cmd, on_timeout=lambda: self._give_up(cmd))

    def capture(self) -> None:
        """Start reading the focused window for this dictation's paste to use.

        Called as a dictation begins, before the pill is told to appear: that
        is the last moment the desktop names the window being dictated INTO,
        because a pill with no layer-shell surface takes the keyboard when it
        maps and anything asked later names the pill.

        Started on a thread rather than run here. This runs on the hotkey
        listener thread with the pipeline lock held, and the command can take
        the better part of a second: done inline it delayed the recording pill
        by that much and, worse, held up the release of a push-to-talk key, so
        a short tap recorded until the command came back.
        """
        self._captured, cmd = None, self._command()
        if not cmd or cmd == self._gave_up_on:
            self._reader = None
            return
        self._reader = threading.Thread(target=self._read, args=(cmd,),
                                        name="focused-window", daemon=True)
        self._reader.start()

    def _read(self, cmd: str) -> None:
        self._captured = self._run(cmd, on_timeout=lambda: self._give_up(cmd))

    def __call__(self) -> str | None:
        """The window this dictation began in, or None if nobody would say.

        Waits for the reader if it is somehow still going; by paste time it has
        had the whole recording and transcription to finish in.
        """
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(WINDOW_COMMAND_TIMEOUT_S + 0.2)
        return self._captured

    def _give_up(self, cmd: str) -> None:
        self._gave_up_on = cmd
        log.warning("%r did not answer in time and will not be asked again unless "
                    "inject.active_window_command changes. Pastes are now sent "
                    "without knowing the focused window, and the pill says so. If "
                    "this is KWin's queryWindowInfo it is an interactive window "
                    "picker rather than a query: set inject.active_window_command "
                    "to something that answers on its own.", cmd)


def window_class_getter(config: Config) -> Callable[[], str | None]:
    """Ask the desktop which window has the keyboard, if it will say.

    Read fresh on every dictation rather than captured once: the owner can set
    `inject.active_window_command` in the settings window and the next paste
    should use it, and `effective_window_command` is cheap - a dict lookup and
    at most one `shutil.which`.
    """
    return FocusedWindow(lambda: effective_window_command(config.get, os.environ))


#: How long the daemon waits for `{"state": "hidden"}` to reach the helper
#: before the injector starts its own settle. The helper unmaps the window on
#: its next frame, ~33 ms later.
OVERLAY_HIDE_FLUSH_S = 0.5

#: How long the daemon waits for the helper to say what its fill still needs
#: (see `voice.ui.overlay`). Short on purpose: the answer is normally already
#: waiting by the time this is asked - the insertion has done its clipboard
#: work since - and a helper that never answers has to cost the paste a blink,
#: not a pause, before the daemon falls back to its own estimate.
FILL_ACK_S = 0.1


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
    """Hops work from another thread onto the Qt thread.

    Everything reachable from `handle` that mutates state, touches the config file
    or drives Tray's QMenu/QAction must travel through one of these signals. The
    last two carry answers back from the settings window's background refreshes
    (`_refresh_settings_inputs`), which are the other threads that reach Qt.
    """

    open_settings = Signal()
    apply_config = Signal()
    set_profile = Signal(str)
    set_language = Signal(str)
    quit = Signal()
    triggers_refreshed = Signal()
    refresh_sources = Signal()
    sources_listed = Signal(object)
    layer_shell_probed = Signal(object)
    preview_pill = Signal(str, int, int)


class Daemon:
    def __init__(self, config: Config, *, listener=None, recorder=None, clipboard=None, sender=None,
                 notifier=None, tray=None, preview_timer=None, clock=None):
        self.config = config
        #: The only clock the daemon reads. Injected so the paste's sequencing
        #: around the pill's animations is testable without waiting for them.
        self._clock = clock or time.monotonic
        #: What ends a pill preview after PREVIEW_SECONDS. A Qt single shot in
        #: the daemon; tests hand in a clock they can fire themselves.
        self._preview_timer_factory = preview_timer or _single_shot
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
        #: What the running listener was built from; None until build().
        self._hotkey_settings: tuple | None = None
        #: The "no key assigned" notification is worth sending once, not per reload.
        self._shortcut_hint_shown = False
        #: The terminal-paste warning last given. Re-checked on every reload -
        #: clearing `inject.terminal_classes` in the settings window puts the
        #: owner straight into the silent-discard state - but only said again
        #: when the answer has actually changed.
        self._terminal_warning = ""
        #: The language the pill was last told about; see _send_overlay.
        self._overlay_language = str(config.get("general.language", "en") or "en")
        #: The [ui] settings the running helper was started with; see _make_overlay.
        self._overlay_settings: tuple | None = None
        #: The last microphone listing: the settings window opens on it instead
        #: of waiting for `pw-dump`, and the record-start guard reads it rather
        #: than shelling out between the hotkey and `recorder.start()`. None is
        #: "nobody has managed to ask yet", which is not the same answer as an
        #: empty list and must never stop a recording - see `_capture_sources`.
        #: Only ever written on the Qt thread.
        self._source_cache: list | None = None
        #: What the focus-stealing pill costs the paste, if anything; set by
        #: _make_injector, reported by `status`. See pill_policy().
        self._pill_policy = "none"
        #: When the pill's fill will have reached the end of its track, once
        #: one has been asked for; None when nothing is owed. Written when the
        #: message goes out, read once by the injector before it hides the pill.
        self._fill_lands_at: float | None = None
        #: True from the moment the injector takes the pill off screen for the
        #: paste chord until the next state reaches the pill. While it is set,
        #: nothing may put the pill back: doing so hands a focus-stealing
        #: window the keyboard mid-paste, the chord lands in it, and
        #: `restore_clipboard` then overwrites the transcript.
        self._pill_hidden_for_paste = False
        #: Built once and kept. It was built inside `_make_injector`, which
        #: `apply_config` calls on every reload, so a command already known to
        #: hang was re-armed by every language switch, profile switch and
        #: settings save - and the next paste paid its full timeout again.
        self._focused_window = window_class_getter(config)
        #: Serialises everything that reaches the pill. `_send_overlay` used to
        #: have one caller, serialised by `_set`; the busy notice added a second
        #: on the hotkey listener thread, racing the worker. It guards the flag
        #: above - the check and the send have to be one step, or the notice
        #: slips in behind the hide and remaps a focus-stealing pill mid-paste -
        #: and the `_overlay_language`/`_fill_lands_at` read-modify-writes below,
        #: which two threads could otherwise interleave into a wrong badge.
        self._overlay_lock = threading.Lock()
        #: One background refresh of each kind at a time; see _refresh_off_thread.
        self._refreshers: dict[str, threading.Thread] = {}
        #: Whether this desktop can put the pill where the config says; None
        #: until the helper probe has answered. See _on_layer_shell_probed.
        self._layer_shell: bool | None = None
        #: The countdown that ends the pill preview, while one is on screen.
        #: Not None is exactly "a preview is showing"; see _preview_pill.
        self._preview: object | None = None
        self._bridge = _Bridge()

    # -- construction -------------------------------------------------------
    def build(self) -> None:
        self.tracker = Tracker(hotkey_specs(self.config))
        self.history = History()
        self.listener, self.hotkey_backend = self._make_listener()
        self._hotkey_settings = self._hotkey_snapshot()
        self._sender = self._sender or make_key_sender()
        self.injector = self._make_injector()
        self._active_profile = self._profile_snapshot()
        services = Services(recorder=self._recorder, transcriber=self._make_transcriber(),
                            injector=self.injector, history=self.history, notify=self._notifier.notify,
                            config_getter=self.config.get, prompt_getter=self._prompt,
                            hotwords_getter=self._hotwords, sources=self._capture_sources)
        self.dictation = Dictation(services)
        self.tray = self._tray or Tray(self._on_tray_action)
        self.overlay = self._make_overlay()
        self.dictation.on_state = self._on_dictation_state
        self.dictation.on_busy = self._on_dictation_busy
        self.tray.set_profiles(list(self.config.get("stt.profiles", {}) or {}), self.config.get("stt.active"))
        self.tray.set_languages(self.config.languages(), self.config.get("general.language"))
        self.tray.set_profile_hint(profile_hint(self.config))
        self._bridge.open_settings.connect(self.open_settings)
        self._bridge.apply_config.connect(self.apply_config)
        self._bridge.set_profile.connect(self._set_profile)
        self._bridge.set_language.connect(self._set_language)
        self._bridge.quit.connect(self._quit)
        self._bridge.triggers_refreshed.connect(self._on_triggers_refreshed)
        self._bridge.refresh_sources.connect(self._refresh_sources)
        self._bridge.sources_listed.connect(self._on_sources_listed)
        self._bridge.layer_shell_probed.connect(self._on_layer_shell_probed)
        self._bridge.preview_pill.connect(self._preview_pill)
        self._server = Server(self.handle)

    def _make_injector(self) -> Injector:
        """The injector, told what the recording pill is going to do to it.

        Built here rather than inline so `build()` and `apply_config()` cannot
        drift: the pill policy is re-read on every reload, so changing it in the
        settings window takes effect without restarting the daemon.
        """
        self._pill_policy = pill_policy(self.config, pill_takes_focus(self.config))
        return Injector(self._clipboard, self._sender, self.config.get("inject", {}) or {},
                        self.listener.modifiers_held,
                        # A pill that cannot steal the keyboard leaves the live
                        # answer trustworthy, and live is better: it follows the
                        # owner if they change window mid-dictation.
                        self._focused_window.live if self._pill_policy == "none"
                        else self._focused_window,
                        pill_policy=self._pill_policy, hide_pill=self._hide_pill_for_paste,
                        fill_wait=self._pill_fill_wait, settle_s=pill_settle_s(self.config))

    def _warn_if_terminals_can_never_be_pasted_into(self, notify: bool = True) -> None:
        """Say once, at startup, that a terminal will swallow every paste.

        The injector cannot find this out for itself. The compositor accepts
        the chord whatever window has focus, so a paste into a terminal that
        ignores Ctrl+V is indistinguishable, from here, from one that worked -
        which is how a dictation came to look lost when it had been sitting on
        the clipboard the whole time. One line at startup, and the same line
        from `voice doctor`, is the only warning that can honestly be given.
        """
        settings = self.config.get("inject", {}) or {}
        if str(settings.get("mode", "paste")).strip().lower() != "paste":
            # Cleared, not just skipped: switching to clipboard mode and back
            # otherwise left the old warning stored, so the identical warning
            # was suppressed as "already said" when the condition came back.
            self._terminal_warning = ""
            return                     # not pasting at all: no chord to be wrong
        if self._pill_policy == "clipboard":
            # `inject.mode` is "paste", but the pill takes focus here and
            # `inject.pill_focus = clipboard` has already decided not to send a
            # chord at all. Warning about the chord being wrong for terminals
            # would be a daily notification about something that never happens.
            # Cleared for the same reason as the branch above: switching to
            # this policy and back must not leave the warning suppressed.
            self._terminal_warning = ""
            return
        configured = effective_window_command(self.config.get, os.environ)
        if configured and not self._focused_window.usable:
            # A different fault entirely from "this desktop will not say": the
            # command exists and names the window perfectly well, it simply
            # stopped answering. Saying the desktop is mute would point the
            # owner at the wrong fix, and contradicts what `voice doctor` says.
            self._say_terminal_warning(
                f"{configured} stopped answering, so every dictation is now pasted "
                f"without knowing the focused window. The transcript is left on the "
                f"clipboard and the pill says so.", notify)
            return
        unreachable, why = terminal_chord_is_unreachable(
            configured,
            str(settings.get("paste_chord", "ctrl+v")),
            str(settings.get("terminal_chord", "ctrl+shift+v")),
            settings.get("terminal_classes", []))
        if not unreachable:
            self._terminal_warning = ""
            return
        self._say_terminal_warning(why, notify)

    def _say_terminal_warning(self, why: str, notify: bool) -> None:
        """Say `why` once, and again only if the answer has actually changed."""
        if why == self._terminal_warning:
            return                     # already said, and nothing has changed
        self._terminal_warning = why
        log.warning("%s", why)
        if not notify:
            # A reload happens every time the settings window saves. The log
            # line and `voice doctor` carry it from there; a notification on
            # every save would be nagging rather than news.
            return
        # At startup it is worth a notification: the journal is not a channel
        # the owner reads, and this is permanent, silent data loss - every
        # terminal paste discarded. Same treatment as "No keyboard access",
        # which sits directly above the call site. `general.notifications =
        # false` still suppresses it, and `voice doctor` remains the channel
        # that cannot be switched off.
        self._notifier.notify("Dictation cannot paste into a terminal", why, "normal")

    def _blind_hint(self) -> str:
        """What to offer when a paste could not be confirmed to have landed."""
        return DONE_UNVERIFIED

    def _pill_fill_wait(self) -> float:
        """Seconds still owed to the pill's fill before it may be unmapped.

        The injector calls this immediately before `_hide_pill_for_paste`, and
        everything the insertion has already done - the clipboard snapshot, the
        copy, waiting for the hotkey modifiers to clear - has been running
        while the fill did, so only the remainder is left to wait for. Read
        once: a second insertion must not inherit a deadline from the first.

        The helper is asked first, because only it knows two things this side
        cannot see: whether anything is animating at all (nothing is, under
        reduced motion or for a pill that was not transcribing) and when the
        fill actually started - which is not when the message was queued, since
        it queues behind up to thirty level messages a second. A helper that
        does not answer leaves the old estimate in place.
        """
        with self._overlay_lock:
            lands_at, self._fill_lands_at = self._fill_lands_at, None
        if lands_at is None:
            return 0.0
        told = self._what_the_pill_says()
        if told is not None:
            return max(0.0, min(PILL_FILL_S, told))
        # Never longer than the animation itself, whatever a clock has done.
        return max(0.0, min(PILL_FILL_S, lands_at - self._clock()))

    def _what_the_pill_says(self) -> float | None:
        """The helper's own answer to the `finish` just sent, or None.

        None covers every way of not being told: an older helper with nothing
        to say, one that is wedged, or a client with no such question. The
        caller then falls back to the estimate, exactly as before.
        """
        ask = getattr(self.overlay, "finish_wait", None) if self.overlay is not None else None
        if ask is None:
            return None
        try:
            answer = ask(FILL_ACK_S)
            return None if answer is None else float(answer)
        except Exception:
            log.debug("the pill did not say how far its fill had got", exc_info=True)
            return None

    def _hide_pill_for_paste(self) -> None:
        """Take the pill off screen so the paste chord reaches the user's window.

        Called by the injector on the dictation worker, immediately before the
        chord, and only when the pill is a focus-stealing window. The helper
        unmaps on its next frame, so the flush below and the injector's own
        settle are both needed - the flush only proves the line was written.
        The pill comes straight back: the IDLE that follows a successful
        insertion sends `done`, which is the checkmark.
        """
        if self.overlay is None:
            return
        with self._overlay_lock:
            self._pill_hidden_for_paste = True
            self.overlay.send({"state": "hidden"})
        # Outside the lock: this waits on the helper, and a notice that is
        # going to be refused anyway must not queue behind half a second of it.
        self.overlay.flush(OVERLAY_HIDE_FLUSH_S)

    def _make_overlay(self) -> OverlayClient:
        """The recording pill's supervisor. Disabled means: never spawn anything."""
        enabled = bool(self.config.get("ui.overlay", True))
        position, margin_x, margin_y = self.config.overlay_placement()
        # The helper is started with --lang, so that is the badge it already
        # shows: what we track here is the last language it was *told*. A
        # respawn reads it again rather than the one baked in at build time.
        # Under the lock like every other writer of it - this runs on the Qt
        # thread and can interleave with the worker inside `_send_overlay`.
        with self._overlay_lock:
            self._overlay_language = str(self.config.get("general.language", "en") or "en")
        self._overlay_settings = self._overlay_snapshot()
        verbose = log.isEnabledFor(logging.DEBUG)
        allow_fallback = bool(self.config.get("ui.overlay_allow_fallback", False))
        return OverlayClient(enabled, launcher=lambda: default_launcher(
            position=position, margin_x=margin_x, margin_y=margin_y,
            lang=self._overlay_language, verbose=verbose, allow_fallback=allow_fallback))

    def _preview_pill(self, position: str, margin_x: int, margin_y: int) -> None:
        """Qt thread: show the real pill at `position` for a few seconds.

        The helper is told its placement on its command line - a layer surface
        has anchors, and they are set when the surface is created - so showing
        a placement means running a helper at it. The live one is stopped, a
        preview one takes its place, and `_end_pill_preview` puts the first
        back. Nothing here touches the pipeline: no recording is started, no
        history entry is written, and the dictation state machine never hears
        about it. It is a picture of a pill, not a dictation.
        """
        self._end_pill_preview()               # a second nudge replaces the first
        with self._overlay_lock:
            if self._pill_hidden_for_paste:
                # A dictation is between the pill being unmapped and its chord.
                # The preview is a focus-stealing window like any other: mapping
                # one here takes the keyboard, the chord lands in the preview,
                # and `restore_clipboard` overwrites the transcript. The
                # placement can be previewed a second later.
                log.info("not previewing the pill: a paste is in flight")
                return
        if self.overlay is not None:
            self.overlay.stop()
        language = str(self.config.get("general.language", "en") or "en")
        self.overlay = self._make_preview_overlay(position, margin_x, margin_y, language)
        self.overlay.start()
        # Through the guarded sender like every other route that can map the
        # pill - see `test_only_the_guarded_sender_can_put_the_pill_back_on_screen`.
        # The levels carry no state and cannot map anything, so they go direct.
        self._send_overlay([{"language": language}, {"state": "recording"}],
                           language, refuse_while_hidden=True)
        for level in PREVIEW_LEVELS:
            self.overlay.send({"level": level})
        self._preview = self._preview_timer_factory(PREVIEW_SECONDS, self._end_pill_preview)

    def _make_preview_overlay(self, position: str, margin_x: int, margin_y: int,
                              language: str) -> OverlayClient:
        """A helper at one placement, for the preview only.

        Deliberately not `_make_overlay`: that one reads the config and records
        what it started the helper with, and a preview must leave both alone -
        the placement being tried has not been saved, and may never be.
        """
        verbose = log.isEnabledFor(logging.DEBUG)
        allow_fallback = bool(self.config.get("ui.overlay_allow_fallback", False))
        with self._overlay_lock:
            self._overlay_language = language
        return OverlayClient(True, launcher=lambda: default_launcher(
            position=position, margin_x=margin_x, margin_y=margin_y, lang=language,
            verbose=verbose, allow_fallback=allow_fallback))

    def _end_pill_preview(self) -> None:
        """Take the preview off the screen and give the real pill its helper back.

        Safe to call when no preview is showing, which is what makes it usable
        as both the timer's callback and the "a real dictation started" guard.
        """
        timer, self._preview = self._preview, None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if cancel is not None:
            cancel()                           # a dictation ended it early
        if self.overlay is not None:
            self.overlay.send({"state": "hidden"})
            self.overlay.flush(OVERLAY_HIDE_FLUSH_S)
            self.overlay.stop()
        self.overlay = self._make_overlay()
        self.overlay.start()

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

        # Before anything is told to appear: the pill takes the keyboard when it
        # maps, so this is the last moment the desktop still names the window
        # the owner is dictating into. A recall has no recording, but it starts
        # from idle with nothing on screen, so the same moment serves.
        # RETRY as well as RECORDING: a retry happens minutes after the
        # recording it re-runs, very possibly in another window, and replaying
        # the old class chose that window's chord while reporting the paste as
        # verified - so the restore wiped a transcript the new window had
        # discarded. A recall has no recording, but starts from idle with
        # nothing on screen, so the same moment serves.
        if (state is State.RECORDING
                or (state is State.TRANSCRIBING and detail == RETRY)
                or (state is State.INJECTING and detail == RECALL)):
            self._focused_window.capture()
        # A preview showing while a real recording starts would be taken for the
        # recording itself - and it is a helper at the wrong placement, on a
        # client the pipeline's messages are not meant for.
        if self._preview is not None and state is not State.IDLE:
            self._end_pill_preview()
        self.tray.state_changed.emit(state.value, detail)
        if self.overlay is None:
            return
        language = str(self.config.get("general.language", "en") or "en")
        # Only the `hide` policy unmaps the pill for the chord; every other
        # case leaves it on screen, where the fill finishes into the checkmark
        # on its own and costs the paste nothing. See pill_policy().
        messages = overlay_messages(state, detail, language,
                                    finish_fill=self._pill_policy == "hide",
                                    blind_hint=self._blind_hint())
        self._send_overlay(messages, language)

    def _on_dictation_busy(self, state: State) -> None:
        """Answer a dictate press the pipeline was in no position to act on.

        Called on the hotkey listener thread. The pill's client swallows its
        own failures, and the pipeline nets anything this raises, so a missing
        or wedged helper costs the press its answer and nothing else.

        `_pill_hidden_for_paste` is read rather than the state that came with
        the press, because they are not the same thing: `toggle()` samples the
        state under its lock and reports it after releasing, so a press made
        during TRANSCRIBING can arrive here once the injector has already
        unmapped the pill for the chord. The pill's actual situation is the
        only safe thing to test - answering the press is not worth the
        transcript it was asking about.
        """
        if self.overlay is None:
            return
        language = str(self.config.get("general.language", "en") or "en")
        self._send_overlay(busy_messages(state, self._pill_policy), language,
                           refuse_while_hidden=True)

    def _send_overlay(self, messages: list[dict], language: str,
                      refuse_while_hidden: bool = False) -> None:
        """Send one transition's messages, badge first when it has changed.

        The pill draws the badge from the last language it was told about, and
        the language can change from anywhere - the toggle, the tray, the
        settings dialog - between two states. So every state message is
        preceded by the language whenever it has moved since the last one sent.

        `refuse_while_hidden` marks the messages that are only an answer to the
        owner, never a state: they are dropped outright while the injector has
        the pill off screen for a paste chord. The test and the send happen
        under one lock, because they were two steps and the gap between them
        was enough - `_hide_pill_for_paste` blocks for up to half a second on
        its flush - for a notice to arrive after the hide, remap a
        focus-stealing pill, take the chord into it, and have
        `restore_clipboard` overwrite the transcript 150 ms later.

        Anything that is a state transition clears the flag on its way past:
        it is about to repaint the pill, so nothing is hidden on the
        insertion's behalf any more.
        """
        with self._overlay_lock:
            if refuse_while_hidden:
                if self._pill_hidden_for_paste:
                    return
            else:
                self._pill_hidden_for_paste = False
            self._send_overlay_locked(messages, language)

    def _send_overlay_locked(self, messages: list[dict], language: str) -> None:
        """The body of `_send_overlay`. Caller holds `_overlay_lock`."""
        for message in messages:
            if "language" in message:
                self._overlay_language = str(message["language"])
            elif "state" in message and language != self._overlay_language:
                self.overlay.send({"language": language})
                self._overlay_language = language
            if message.get("finish"):
                # Armed where it is sent, so the deadline and the message can
                # never disagree about whether a fill is on its way. Two things
                # are armed: the wait for the helper's own answer (which is the
                # truth - it knows whether anything is animating and when it
                # really started), and, for a helper that never answers, the
                # estimate this has always used.
                self._fill_lands_at = self._clock() + PILL_FILL_S
                expect = getattr(self.overlay, "expect_finish", None)
                if expect is not None:
                    expect()
            self.overlay.send(message)

    def _sync_overlay_language(self) -> None:
        """Tell a pill that is already on screen about a language changed elsewhere."""
        if self.overlay is None:
            return
        language = str(self.config.get("general.language", "en") or "en")
        # Under the same lock as every other writer of `_overlay_language`:
        # this runs on the Qt thread, from the tray and the settings dialog,
        # and can otherwise interleave with a pipeline transition into a badge
        # that disagrees with what the helper was last told.
        with self._overlay_lock:
            if language != self._overlay_language:
                self.overlay.send({"language": language})
                self._overlay_language = language

    def _hotkey_snapshot(self) -> tuple:
        """What the listener was built from. The portal binds its shortcuts once,
        when the session is created, so a change here needs a new listener - the
        configured backend rather than the resolved one, to avoid re-probing the
        keyboards and the seat on every reload.

        Only the triggers the *running* backend actually binds: the evdev
        listener reads `hotkeys.<name>` through the tracker, which apply_config
        updates in place, so rebuilding it for an edited portal trigger would
        drop its /dev/input descriptors for a setting it never looks at.
        """
        backend = str(self.config.get("hotkeys.backend", "auto") or "auto").strip().lower()
        triggers = portal_shortcuts(self.config) if self.hotkey_backend == "portal" else {}
        return (backend, triggers)

    def rebind_hotkeys(self) -> None:
        """Bind again now, to what the file and the desktop hold at this moment.

        The settings window calls this after it has written the desktop's own
        shortcut store (see `voice.hotkey.desktop_shortcuts`): the key that
        moved is the *desktop's*, so `_hotkey_snapshot` sees nothing at all, and
        without a rebind the listener keeps the old binding until a restart.

        Done here rather than armed for the next apply_config, because there
        may not be one - "Change…" hands the key over and writes it with no
        save behind it - and a flag left armed fires during some later,
        unrelated reload instead, tearing the listener down (and possibly
        re-showing the desktop's permission dialog) for a request that belonged
        to a different minute. The file is re-read first: the window saves the
        trigger before it asks for this, and the listener has to bind what is
        actually written down.

        On the save path this still costs exactly one rebind: the window asks
        before `saved`, and the apply_config that follows finds the bindings
        already current and leaves them alone.
        """
        if self._hotkey_settings is None:
            return                      # nothing is built yet; build() binds it
        if not self._reload_config():
            return
        before = self.listener
        self._rebind_hotkeys_if_needed(force=True)
        if self.listener is not before:
            # The injector holds the listener's `modifiers_held`, so it must not
            # be left pointing at the one that has just been stopped.
            self.injector = self._make_injector()
            self.dictation.set_injector(self.injector)

    def _rebind_hotkeys_if_needed(self, force: bool = False) -> None:
        """Rebuild the listener when the backend or a portal trigger changed.

        `force` is a caller that knows something no config snapshot can see -
        the desktop's own store moved under us. The desktop may show its
        permission dialog again; that is the price of applying a new trigger
        without restarting the daemon.
        """
        if self._hotkey_settings is None:
            return
        if not force and self._hotkey_snapshot() == self._hotkey_settings:
            return
        log.info("hotkey bindings changed; rebuilding the listener")
        try:
            self.listener.stop()
        except Exception:
            log.exception("failed to stop the listener while rebinding")
        try:
            listener, backend = self._make_listener()
        except Exception as exc:
            # The old session is already closed, so there are no hotkeys either
            # way; say so rather than leaving a stopped listener behind in
            # silence. `_hotkey_settings` is deliberately left alone: the next
            # reload tries again, which is how a fixed trigger recovers without
            # restarting the daemon. Everything after this call still applies.
            log.exception("failed to build the new hotkey listener")
            self._notifier.notify("Hotkeys are off",
                                  f"{exc}. Fix the setting and run `voice reload`, or restart "
                                  f"{APP_NAME}.", "critical")
            return
        previous_backend, self.hotkey_backend = self.hotkey_backend, backend
        self.listener = listener
        self._hotkey_settings = self._hotkey_snapshot()
        try:
            self.listener.start()
        except Exception:
            log.exception("failed to start the rebuilt listener")
        # After start(), never before: the replacement window asks the listener
        # what the desktop holds as it is built, and a listener that has not
        # started yet can only answer "nothing". The portal's own answer comes
        # later still, and reaches the window through _on_hotkeys_ready.
        if backend != previous_backend:
            self._rebuild_settings_dialog()

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

    def _on_hotkeys_ready(self, state: str) -> None:
        """Called from the portal listener thread once the desktop has answered.

        `state` is one of the listener's STATE_* words. "unassigned" is the one
        that used to pass for success: the desktop registered our shortcut and
        attached no key to it, so nothing we do makes a press arrive - only the
        user, in their keyboard settings, can.

        This is also the moment a settings window that is already open learns
        what the desktop holds: it may have been built (or rebuilt for a new
        backend) while the portal was still opening its session, and nothing
        else would have told it afterwards - the fields then read "waiting for
        an answer" until the window was closed and reopened. Queued through the
        bridge, because this runs on the listener thread.
        """
        self._bridge.triggers_refreshed.emit()
        if state == STATE_BOUND:
            # A key is attached again (the user assigned one, or the desktop told
            # us so with ShortcutsChanged). The one-shot latch must not outlive
            # the problem it reported: losing the key a second time is news.
            self._shortcut_hint_shown = False
            return
        if state == STATE_UNASSIGNED:
            # Once per daemon run: a reload rebuilds the listener, and a repeated
            # critical notification for a state the user is already looking at is
            # noise they cannot switch off.
            if self._shortcut_hint_shown:
                return
            self._shortcut_hint_shown = True
            self._notifier.notify("Dictation shortcut is not assigned",
                                  "Open Keyboard Settings and set a key for voice.", "critical")
            return
        self._notifier.notify("Shortcut not registered",
                              "The desktop did not bind the global shortcut. Check its shortcut settings, "
                              "or run install.sh so the portal can resolve this app.", "critical")

    def _shortcut_status(self) -> dict:
        """The portal listener's effective triggers, for `status` and `doctor`.

        Empty on evdev, which has no such thing, and on any listener a test
        substituted - the two accessors are portal-only. Read from the listener
        at every call, never cached here: the desktop owns the key and can move
        it at any moment, and a copy taken at build time is how `voice status`
        came to report "no key assigned" for the rest of the run.
        """
        if self.hotkey_backend != "portal":
            return {}
        state = getattr(self.listener, "shortcut_state", None)
        triggers = getattr(self.listener, "effective_triggers", None)
        if state is None or triggers is None:
            return {}
        return {"shortcut_state": state(), "shortcut_triggers": triggers()}

    def effective_triggers(self) -> dict[str, str]:
        """The trigger the desktop currently holds per shortcut id ("" = none).

        The settings window shows it beside the `hotkeys.portal_*` fields, which
        are a first-run preference and on GNOME are never applied at all - so
        the only truthful thing to show there is what the desktop actually has.
        Empty on evdev and on a listener that cannot be asked.
        """
        return dict(self._shortcut_status().get("shortcut_triggers") or {})

    def _refresh_shortcut_triggers(self) -> None:
        """Ask the desktop again what it holds.

        `ShortcutsChanged` keeps the listener current where the portal sends it;
        this covers a signal emitted while the daemon was still starting, and a
        backend that never sends one. Failure is not fatal: the listener keeps
        the triggers it already has.
        """
        refresh = getattr(self.listener, "refresh_triggers", None)
        if refresh is None:                    # evdev, or a test's fake listener
            return
        try:
            refresh()
        except Exception:
            log.exception("could not re-read the desktop's shortcut assignment")

    def _refresh_off_thread(self, name: str, work, done) -> None:
        """Run `work()` on a throwaway thread and hand its answer to `done`.

        `done` is a bridge signal's `emit`, so Qt queues the answer and the slot
        runs back on the Qt thread with a widget it is allowed to touch. One
        thread of each `name` at a time: clicking the tray twice in a second
        must not start two `pw-dump`s or two D-Bus round trips.
        """
        running = self._refreshers.get(name)
        if running is not None and running.is_alive():
            return

        def run() -> None:
            try:
                done(work())
            except Exception:
                log.exception("background %s refresh failed", name)

        thread = threading.Thread(target=run, name=f"voice-{name}", daemon=True)
        self._refreshers[name] = thread
        thread.start()

    def _refresh_settings_inputs(self) -> None:
        """Re-read the two slow facts the settings window shows, off the Qt thread.

        Both of these used to run on the Qt thread *before* the window was shown,
        which is the whole of "why is opening the setting so slow?": asking the
        portal what the desktop holds is a synchronous D-Bus round trip bounded
        by the listener's own 2 s timeout, and listing microphones is `pw-dump`
        with a 5 s one. The window opens on what is already known - the listener
        caches the triggers and keeps them current from `ShortcutsChanged`, and
        the last listing is kept here - and both slots below correct it in place
        when the real answer arrives.
        """
        self._refresh_off_thread("triggers", self._refresh_shortcut_triggers,
                                 lambda _: self._bridge.triggers_refreshed.emit())
        self._refresh_sources()
        # Whether the pill can be placed at all. The probe spawns two
        # interpreters on a cold cache, which is not something the Qt thread may
        # do while a window is opening - and the answer cannot change under a
        # running daemon, so it is asked once and remembered.
        self._refresh_off_thread("layer_shell", lambda: cached_probe().layer_shell,
                                 self._bridge.layer_shell_probed.emit)

    def _on_triggers_refreshed(self) -> None:
        """Qt thread: the desktop has answered. Update the window, if any is up.

        The dialog may have been closed, or thrown away and rebuilt for another
        backend, between the question and the answer; `self._settings` is always
        the one on screen now, and None is simply nobody to tell.
        """
        if self._settings is not None:
            self._settings.refresh_effective_triggers()

    def _on_layer_shell_probed(self, available) -> None:
        """Qt thread: the helper probe has answered.

        Kept here as well as pushed, so a window opened later starts out saying
        the right thing instead of waiting for its own probe.
        """
        self._layer_shell = bool(available)
        if self._settings is not None:
            self._settings.set_layer_shell(self._layer_shell)

    def _capture_sources(self) -> list | None:
        """What PipeWire last said it had, plus a fresh ask for the next time.

        Read, never asked. This is called from `Dictation.start()`, which is on
        the listener thread with the pipeline's lock held, between the hotkey and
        `recorder.start()` - and `pw-dump` is a subprocess bounded at five
        seconds. Measured at 11-18 ms here (~70 ms on the reviewer's machine) of
        latency before every dictation, which is a clipped first syllable; a
        PipeWire that was wedged rather than merely slow would have held the lock
        for the whole five seconds, blocking stop(), cancel() and toggle() too.

        The guard it feeds is unchanged: `[]` is still "this machine has no
        capture device" and None is still "could not be asked", which may never
        stop a recording. Asking here for a fresh listing is what keeps the
        cache honest without putting it in the way - a microphone unplugged
        mid-session is noticed by the next dictation, including one that was
        just refused for want of one.
        """
        self._bridge.refresh_sources.emit()
        return self._source_cache

    def _refresh_sources(self) -> None:
        """Ask `pw-dump` for the capture devices, off whatever thread is here."""
        self._refresh_off_thread("sources", capture_sources,
                                 self._bridge.sources_listed.emit)

    def _on_sources_listed(self, sources) -> None:
        """Qt thread: `pw-dump` has answered - with a listing, or with nothing.

        None is kept as None: the guard reads this, and flattening "could not
        ask" into "no microphone" would refuse to record on a machine whose
        capture works perfectly well.
        """
        self._source_cache = None if sources is None else list(sources)
        if self._settings is not None:
            self._settings.set_sources(self._source_cache or [])

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

    def _hotwords(self) -> str | None:
        """The vocabulary for the next dictation: the words the user listed plus
        the spellings their replacements aim at.

        Runs on the worker just before transcribe(), like _prompt(), and must not
        raise there for the same reason: a hand-edited dictionary costs the
        vocabulary, never the recording.
        """
        try:
            return self.config.hotwords() or None
        except Exception:
            log.debug("no vocabulary available", exc_info=True)
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
        # Before the first hotkey, so the no-microphone guard has an answer to
        # read rather than the "nobody asked yet" it starts out with.
        self._refresh_sources()
        # The portal listener answers asynchronously and reports through
        # _on_hotkeys_ready; only the evdev listener knows its state by now.
        if self.hotkey_backend != "portal" and self.listener.devices_ok() is False:
            self._notifier.notify("No keyboard access", "Run the installer's udev step or add yourself to the input group.", "critical")
        self._warn_if_terminals_can_never_be_pasted_into()
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

    def _reload_config(self) -> bool:
        """Re-read the file. Qt thread only. False means nothing was applied."""
        try:
            self.config.reload()
        except ValueError as exc:
            # Unparseable TOML: the CLI has already printed "ok", so the only way
            # the user learns nothing was applied is this notification.
            log.warning("config reload failed, keeping previous settings: %s", exc)
            self._notifier.notify("Config error, keeping previous settings", str(exc), "critical")
            return False
        return True

    def apply_config(self) -> None:
        """Re-reads config. Runs on the Qt thread only (see _Bridge.apply_config)."""
        if not self._reload_config():
            return
        self.tracker.set_specs(hotkey_specs(self.config))
        self._notifier.set_enabled(bool(self.config.get("general.notifications", True)))
        self._rebind_hotkeys_if_needed()      # before the injector: it holds the listener
        self._refresh_settings_inputs()       # and after it: the new listener is the one to ask
        current = self._profile_snapshot()
        if current != self._active_profile:
            self._active_profile = current
            self.dictation.set_transcriber(self._make_transcriber())
            self._start_warmup()
        self.injector = self._make_injector()
        self.dictation.set_injector(self.injector)
        # The settings window can put the owner straight into the silent-discard
        # state - clearing inject.terminal_classes, or the window command - so
        # this is re-evaluated on every reload and not only at startup. It says
        # nothing unless the answer has changed, and never notifies here; see
        # `_terminal_warning`.
        self._warn_if_terminals_can_never_be_pasted_into(notify=False)
        self.tray.set_profiles(list(self.config.get("stt.profiles", {}) or {}), self.config.get("stt.active"))
        self.tray.set_languages(self.config.languages(), self.config.get("general.language"))
        self.tray.set_profile_hint(profile_hint(self.config))
        self._rebuild_overlay_if_needed()
        self._sync_overlay_language()

    def _set_profile(self, name: str) -> None:
        """Qt thread: persist the profile switch, then apply it.

        The file is re-read first: this Config is a whole-document snapshot, so
        saving it back would otherwise revert every edit made since it loaded -
        a hand edit, or the settings dialog's own save.
        """
        if not self._reload_config():
            return
        if name not in (self.config.get("stt.profiles", {}) or {}):
            # Validated on the IPC thread against the document we had then.
            log.warning("profile %r is no longer in the config; not switching", name)
            self._notifier.notify("Profile is gone",
                                  f"'{name}' is no longer defined in config.toml", "critical")
            return
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
        if not self._reload_config():       # never write back a stale document
            return
        previous = str(self.config.get("general.language", "en") or "en")
        code = self._next_language() if code == NEXT_LANGUAGE else code
        if not code or code == previous:
            # Nowhere to go (a cycle with one entry, or the language asked for
            # is already in force). Saving and flashing "EN → EN" is worse than
            # doing nothing at all.
            return
        if not is_language_code(code):
            # A code named over IPC was checked there, but one resolved from
            # general.languages was not: nothing validates that list on the way
            # in, so a hand-edited entry would be written to general.language
            # and every later load would fail its own validation.
            log.warning("general.languages entry %r is not a language code; not switching", code)
            self._notifier.notify("Unknown language in the cycle",
                                  f"'{code}' in general.languages is not \"auto\" or a "
                                  "two-letter code", "critical")
            return
        try:
            self.config.set("general.language", code)
            self._apply_language_profile(code)
            self.config.save()
        except Exception as exc:
            log.exception("could not save the language switch")
            self._notifier.notify("Could not save settings", str(exc), "critical")
            return
        self.apply_config()             # sends the new badge (_sync_overlay_language)
        if self.overlay is not None:
            # Through `_send_overlay`, not straight at the helper: this runs on
            # the Qt thread while the injector runs on the worker, so a toggle
            # during a paste would otherwise map a focus-stealing pill, take
            # the chord into it, and let `restore_clipboard` overwrite the
            # transcript. A notice is an answer to the owner, never a state.
            self._send_overlay([{"state": "notice", "swaps_language": True,
                                 "text": f"{previous.upper()} \u2192 {code.upper()}"}],
                               str(self.config.get("general.language", "en") or "en"),
                               refuse_while_hidden=True)

    def _apply_language_profile(self, code: str) -> None:
        """Point stt.active at the profile `code` maps to, in this same document.

        Called between the language write and the save so the pair travels as one
        write and one apply_config: the transcriber is rebuilt once, not twice.
        A map naming a profile that is gone must not be written - that config is
        one the daemon itself rejects - so it is reported and the model stays.
        """
        name = self.config.profile_for_language(code)
        if not name or name == self.config.get("stt.active"):
            return
        if name not in (self.config.get("stt.profiles", {}) or {}):
            log.warning("general.language_profiles.%s names unknown profile %r", code, name)
            self._notifier.notify("Language profile missing",
                                  f"'{name}' for {code} is not defined in config.toml", "normal")
            return
        self.config.set("stt.active", name)

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

    def _rebuild_settings_dialog(self) -> None:
        """Throw the cached dialog away after a backend change.

        The backend decides the Hotkeys tab's whole widget set - trigger fields
        and the desktop's keys on the portal, a capture button on evdev - and
        that is decided once, when the dialog is built. Reusing it after a
        `hotkeys.backend` change showed the departed backend's fields. A window
        the user is looking at is replaced rather than merely dropped: saving
        the change from that very window is the commonest way to make it.
        """
        dialog, self._settings = self._settings, None
        if dialog is None:
            return
        visible = dialog.isVisible()
        dialog.close()
        dialog.deleteLater()
        if visible:
            self.open_settings()

    def open_settings(self) -> None:
        try:
            if self._settings is None:
                # Late-bound, both of them: this dialog outlives the listener
                # it was built beside, and a bound `self.listener.capture_next`
                # kept calling the stopped one - "Press a key..." for ever.
                self._settings = SettingsDialog(self.config,
                                                lambda cb: self.listener.capture_next(cb),
                                                lambda: list(self._source_cache or []),
                                                backend=self.hotkey_backend,
                                                triggers=self.effective_triggers,
                                                preview_pill=self._ask_for_preview)
                self._settings.saved.connect(self.apply_config)
                # Emitted before `saved`, so the rebind it asks for happens in
                # the same apply_config as the rest of the save.
                self._settings.shortcuts_rebound.connect(self.rebind_hotkeys)
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
        self._settings.set_layer_shell(self._layer_shell)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()
        # Only now: the window is already on screen, and the two slow reads
        # correct it in place a moment later. See _refresh_settings_inputs.
        self._refresh_settings_inputs()

    def _ask_for_preview(self, position: str, margin_x: int, margin_y: int) -> dict:
        """What the settings window calls when the pill is dropped or nudged.

        Straight through `handle`, exactly as the tray does: one path into the
        preview, validated in one place, whether it is asked for from the window
        or over the socket.
        """
        return self.handle({"cmd": "preview_pill", "position": position,
                            "margin_x": int(margin_x), "margin_y": int(margin_y)})

    def _quit(self) -> None:
        app = QApplication.instance()
        if app:
            app.quit()

    def _handle_preview_pill(self, request: dict) -> dict:
        """`preview_pill`: show the real pill at a placement for a few seconds.

        Validated here, on whichever thread asked, and applied on the Qt thread
        - the same rule as `profile` and `language`, because it starts and stops
        helper processes. Refused rather than queued when there is no pill to
        show or a dictation is using it: a preview must never be mistaken for
        the recording, and must never take the recording's pill off screen.
        """
        position = request.get("position")
        if not isinstance(position, str) or position.strip().lower() not in (
                set(POSITIONS) | set(LEGACY_POSITIONS)):
            return {"ok": False, "error": f"unknown pill placement {position!r}"}
        margins = []
        for key in ("margin_x", "margin_y"):
            value = request.get(key)
            if not is_margin(value):
                return {"ok": False, "error": f"{key} must be a whole number of pixels, "
                                              f"got {value!r}"}
            margins.append(int(value))
        if not self.config.get("ui.overlay", True) or self.overlay is None:
            return {"ok": False, "error": "the recording pill is off (ui.overlay = false)"}
        if self.dictation.state is not State.IDLE:
            return {"ok": False, "error": "not while a dictation is running"}
        self._bridge.preview_pill.emit(normalise_position(position), margins[0], margins[1])
        return {"ok": True, "seconds": PREVIEW_SECONDS}

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
            profile, for_language = profile_for_status(self.config)
            return {"ok": True, "state": d.state.value, "profile": profile,
                    "profile_language": for_language,
                    "backend": d.sv.transcriber.describe(), "last_error": d.last_error,
                    "version": __version__, "keyboard": self.listener.devices_ok(),
                    "hotkey_backend": self.hotkey_backend, **self._shortcut_status(),
                    "language": self.config.get("general.language"),
                    "overlay": self.overlay.status() if self.overlay else "off",
                    "insertion": insertion_status(self.config, self._pill_policy),
                    # Read here rather than by the caller: only this process is
                    # certain to be inside the graphical session the answer
                    # depends on. `voice doctor` may not be.
                    "window_command": effective_window_command(self.config.get, os.environ),
                    # Not the same question as "is one configured": a command
                    # that stopped answering leaves every paste blind while the
                    # configured string still looks perfectly healthy.
                    "window_command_usable": self._focused_window.usable}
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
        if cmd == "preview_pill":
            return self._handle_preview_pill(request)
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
