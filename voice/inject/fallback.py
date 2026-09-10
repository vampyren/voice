"""Non-portal chord senders (wlroots wtype, uinput ydotool) and the chooser."""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable

from evdev import ecodes

from voice.inject.keys import KeySendError, KeySender
from voice.inject.portal import PortalKeySender, portal_available

log = logging.getLogger(__name__)
_XKB = {ecodes.KEY_LEFTCTRL: "ctrl", ecodes.KEY_LEFTSHIFT: "shift", ecodes.KEY_LEFTALT: "alt",
        ecodes.KEY_LEFTMETA: "logo", ecodes.KEY_INSERT: "Insert", ecodes.KEY_ENTER: "Return",
        ecodes.KEY_TAB: "Tab", ecodes.KEY_SPACE: "space", ecodes.KEY_ESC: "Escape"}
_MODS = {ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_LEFTALT, ecodes.KEY_LEFTMETA}


def keycode_to_xkb_name(code: int) -> str:
    if code in _XKB:
        return _XKB[code]
    name = ecodes.KEY.get(code, "")
    name = name[0] if isinstance(name, list) else name
    return name.removeprefix("KEY_").lower()


class _Cmd:
    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def _exec(self, argv: list[str]) -> None:
        try:
            cp = self._run(argv, capture_output=True, text=True, timeout=3)
        except FileNotFoundError as exc:
            raise KeySendError(f"{argv[0]} not installed") from exc
        except subprocess.SubprocessError as exc:
            raise KeySendError(f"{argv[0]} failed: {exc}") from exc
        if cp.returncode != 0:
            raise KeySendError(f"{argv[0]} exited {cp.returncode}: {cp.stderr.strip()}")


class WtypeKeySender(_Cmd):
    name = "wtype"

    def available(self) -> bool:
        return shutil.which("wtype") is not None

    def send_chord(self, keycodes: list[int]) -> None:
        mods = [c for c in keycodes if c in _MODS]
        keys = [c for c in keycodes if c not in _MODS]
        argv = ["wtype"]
        for m in mods:
            argv += ["-M", keycode_to_xkb_name(m)]
        for k in keys:
            argv += ["-k", keycode_to_xkb_name(k)]
        for m in reversed(mods):
            argv += ["-m", keycode_to_xkb_name(m)]
        self._exec(argv)


class YdotoolKeySender(_Cmd):
    name = "ydotool"

    def available(self) -> bool:
        return shutil.which("ydotool") is not None

    def send_chord(self, keycodes: list[int]) -> None:
        seq = [f"{c}:1" for c in keycodes] + [f"{c}:0" for c in reversed(keycodes)]
        self._exec(["ydotool", "key", *seq])


class ClipboardOnlySender:
    name = "clipboard-only"

    def available(self) -> bool:
        return True

    def send_chord(self, keycodes: list[int]) -> None:
        raise KeySendError("no key sender available; text left on clipboard")


def make_key_sender(preferred: str = "auto", run: Callable = subprocess.run) -> KeySender:
    candidates = {"portal": lambda: PortalKeySender(), "wtype": lambda: WtypeKeySender(run),
                  "ydotool": lambda: YdotoolKeySender(run)}
    order = [preferred] if preferred in candidates else ["portal", "wtype", "ydotool"]
    for name in order:
        ok = portal_available() if name == "portal" else shutil.which(name) is not None
        if ok:
            log.info("key sender: %s", name)
            return candidates[name]()
    log.warning("no key sender available; falling back to clipboard-only")
    return ClipboardOnlySender()
