"""Paste chords as evdev keycodes, and the KeySender protocol."""
from __future__ import annotations

from typing import Protocol

from evdev import ecodes

_NAMED = {
    "ctrl": ecodes.KEY_LEFTCTRL, "control": ecodes.KEY_LEFTCTRL,
    "shift": ecodes.KEY_LEFTSHIFT,
    "alt": ecodes.KEY_LEFTALT,
    "super": ecodes.KEY_LEFTMETA, "meta": ecodes.KEY_LEFTMETA, "win": ecodes.KEY_LEFTMETA,
    "insert": ecodes.KEY_INSERT, "enter": ecodes.KEY_ENTER, "return": ecodes.KEY_ENTER,
    "tab": ecodes.KEY_TAB, "space": ecodes.KEY_SPACE, "esc": ecodes.KEY_ESC,
}


class KeySendError(RuntimeError):
    pass


class KeySender(Protocol):
    name: str

    def send_chord(self, keycodes: list[int]) -> None: ...
    def available(self) -> bool: ...


def parse_chord(text: str) -> list[int]:
    codes: list[int] = []
    for raw in text.split("+"):
        part = raw.strip().lower()
        if not part:
            continue
        if part in _NAMED:
            codes.append(_NAMED[part])
        elif part.upper().startswith("KEY_") and part.upper() in ecodes.ecodes:
            codes.append(ecodes.ecodes[part.upper()])
        elif len(part) == 1 and f"KEY_{part.upper()}" in ecodes.ecodes:
            codes.append(ecodes.ecodes[f"KEY_{part.upper()}"])
        else:
            raise ValueError(f"unknown key {part!r} in chord {text!r}")
    return codes
