"""Copy the text, wait for the hotkey modifiers to clear, paste, restore the clipboard."""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from voice.inject.clipboard import Clipboard
from voice.inject.keys import KeySender, KeySendError, parse_chord

log = logging.getLogger(__name__)
MODIFIER_WAIT_S = 1.5
POLL_S = 0.02
SETTLE_S = 0.15

#: What to do when the recording pill is on screen and can only be an ordinary
#: window - GTK 4 dropped the "do not focus me" hints, so without a layer-shell
#: surface the pill has the keyboard when the chord is sent and the paste lands
#: in it. `none` is every other case: a layer-shell pill never takes focus, and
#: neither does a pill that is switched off.
#:
#: `hide` takes the pill off screen, waits for the compositor to hand focus back
#: to whatever had it, and only then sends the chord - the owner keeps both the
#: pill and automatic pasting. `clipboard` refuses to paste at all, which is the
#: honest answer if hiding does not work here: sending the chord anyway can lose
#: the transcript outright, because it goes to the pill and `restore_clipboard`
#: then puts the old contents back. `paste` is the escape hatch: send it and
#: hope, for a compositor that hands the chord on regardless.
#: The three a user may write in `inject.pill_focus`; "none" is the daemon's
#: own answer for "the pill is not in the way here", never a setting.
PILL_FOCUS_CHOICES = ("hide", "clipboard", "paste")
PILL_POLICIES = ("none",) + PILL_FOCUS_CHOICES

#: How long to let the compositor return focus after the pill is unmapped.
#: Configurable (`inject.pill_settle_ms`) because it is a guess about someone
#: else's window manager, not a fact.
PILL_SETTLE_S = 0.15


@dataclass(frozen=True)
class InjectResult:
    method: str
    chord: str
    restored: bool


def run_window_command(cmd: str, run: Callable = subprocess.run) -> str | None:
    if not cmd:
        return None
    try:
        cp = run(cmd, shell=True, capture_output=True, text=True, timeout=1)
        return cp.stdout.strip() or None if cp.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def pill_policy(config, takes_focus: bool) -> str:
    """One of `PILL_POLICIES` for the situation the daemon is in.

    "none" whenever the pill is not in the way, and whenever the user is not
    pasting in the first place - `inject.mode = "clipboard"` is their own
    choice and there is no chord to protect. Otherwise `inject.pill_focus`
    decides, and anything unrecognised in a hand-edited file gets the fix
    rather than the failure.
    """
    if not takes_focus or config.get("inject.mode", "paste") != "paste":
        return "none"
    choice = str(config.get("inject.pill_focus", "hide") or "hide").strip().lower()
    return choice if choice in PILL_FOCUS_CHOICES else "hide"


def pill_settle_s(config) -> float:
    """`inject.pill_settle_ms` in seconds; the default for anything unusable."""
    try:
        millis = float(config.get("inject.pill_settle_ms", PILL_SETTLE_S * 1000))
    except (TypeError, ValueError):
        return PILL_SETTLE_S
    return millis / 1000.0 if millis >= 0 else PILL_SETTLE_S


def insertion_status(config, policy: str) -> str:
    """How the text reaches the window, for `voice status` - in one line.

    Silent about the pill when the pill is not involved: a line about
    layer-shell on a KDE machine, where all of this works, is noise.
    """
    mode = str(config.get("inject.mode", "paste") or "paste")
    if policy == "none":
        return mode
    if policy == "clipboard":
        return "clipboard - press Ctrl+V (the pill takes focus on this desktop)"
    if policy == "paste":
        return f"{mode} (forced: the pill takes focus here and may swallow the chord)"
    return f"{mode} (the pill takes focus here, so it is hidden for the chord)"


class Injector:
    def __init__(self, clipboard: Clipboard, sender: KeySender, settings: dict,
                 modifiers_held: Callable[[], bool], window_class: Callable[[], str | None],
                 sleep: Callable[[float], None] = time.sleep, pill_policy: str = "none",
                 hide_pill: Callable[[], None] | None = None,
                 fill_wait: Callable[[], float] | None = None,
                 settle_s: float = PILL_SETTLE_S):
        self._clip, self._sender, self._settings = clipboard, sender, settings
        self._modifiers_held, self._window_class, self._sleep = modifiers_held, window_class, sleep
        #: Resolved by the daemon, which is the only thing that knows whether the
        #: helper it started got a layer-shell surface. See PILL_POLICIES.
        self._pill_policy = pill_policy if pill_policy in PILL_POLICIES else "none"
        self._hide_pill, self._settle_s = hide_pill, settle_s
        #: Asks the daemon how much of the pill's progress fill is still to
        #: run, in seconds. Only the `hide` policy has anything to wait for.
        self._fill_wait = fill_wait

    def _chord(self) -> str:
        cls = (self._window_class() or "").lower()
        terminals = [str(t).lower() for t in self._settings.get("terminal_classes", [])]
        return self._settings.get("terminal_chord", "ctrl+shift+v") if cls and cls in terminals \
            else self._settings.get("paste_chord", "ctrl+v")

    def inject(self, text: str) -> InjectResult:
        if self._settings.get("mode", "paste") == "clipboard":
            # Deliberate clipboard-only: the chord would go into the void here,
            # and restoring 150 ms later would take the transcript with it. Copy,
            # and leave it there for the user's own Ctrl+V.
            self._clip.set_text(text)
            return InjectResult("clipboard", "", False)
        if self._pill_policy == "clipboard":
            # The pill has the keyboard and we are not going to take it off
            # screen, so the chord would land in the pill - and with
            # restore_clipboard on, the transcript would be gone 150 ms later.
            # Copy, take no snapshot, restore nothing, and let the caller say so.
            self._clip.set_text(text)
            return InjectResult("clipboard-pill", "", False)
        snap = self._clip.snapshot()
        self._clip.set_text(text)
        waited = 0.0
        while self._modifiers_held() and waited < MODIFIER_WAIT_S:
            self._sleep(POLL_S)
            waited += POLL_S
        chord = self._chord()
        try:
            codes = parse_chord(chord)
        except ValueError as exc:
            # A hand-edited chord must degrade to clipboard-only like any other
            # paste failure; raising here skipped the restore and lost the text.
            log.warning("invalid paste chord %r, text left on clipboard: %s", chord, exc)
            return InjectResult("clipboard-only", chord, False)
        self._yield_focus()
        try:
            self._sender.send_chord(codes)
        except KeySendError as exc:
            log.warning("paste failed, text left on clipboard: %s", exc)
            return InjectResult("clipboard-only", chord, False)
        self._sleep(SETTLE_S)
        restored = False
        if self._settings.get("restore_clipboard", True):
            restored = self._clip.restore(snap)
        return InjectResult(self._sender.name, chord, restored)

    def _yield_focus(self) -> None:
        """Give the keyboard back before the chord, if the pill is holding it.

        Last thing before `send_chord`, so a chord that never gets sent - an
        unparseable one - does not blink the pill for nothing. The pill is
        decoration: a helper that has already died must not cost us the text,
        so a failure here is logged and the paste goes ahead regardless.
        """
        if self._pill_policy != "hide" or self._hide_pill is None:
            return
        self._await_fill()
        try:
            self._hide_pill()
        except Exception:
            log.warning("could not take the pill off screen before pasting", exc_info=True)
            return
        self._sleep(self._settle_s)

    def _await_fill(self) -> None:
        """Let the pill's progress fill reach the end of its track first.

        Taking the pill off screen mid-sweep is what leaves the bar stopped in
        the middle, so the wait belongs here, before the hide - the settle
        below cannot cover it, because what the settle waits for (the surface
        going away, the compositor handing focus back) only starts once the
        pill is unmapped. The daemon set this running when the transcript
        landed, so everything the insertion has done since comes off it, and
        it answers 0 when there is nothing on screen to finish.
        """
        if self._fill_wait is None:
            return
        try:
            remaining = float(self._fill_wait())
        except Exception:                       # a broken pill costs no time
            log.debug("could not ask how far the pill's fill had got", exc_info=True)
            return
        if remaining > 0:
            self._sleep(remaining)
