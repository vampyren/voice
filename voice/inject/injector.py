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


class Injector:
    def __init__(self, clipboard: Clipboard, sender: KeySender, settings: dict,
                 modifiers_held: Callable[[], bool], window_class: Callable[[], str | None],
                 sleep: Callable[[float], None] = time.sleep):
        self._clip, self._sender, self._settings = clipboard, sender, settings
        self._modifiers_held, self._window_class, self._sleep = modifiers_held, window_class, sleep

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
