"""Wayland clipboard via wl-clipboard (wl-copy / wl-paste)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)
TIMEOUT = 2


class ClipboardError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    text: str | None


class Clipboard:
    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def available(self) -> bool:
        return shutil.which("wl-copy") is not None and shutil.which("wl-paste") is not None

    def snapshot(self) -> Snapshot:
        try:
            types = self._run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=TIMEOUT)
            if types.returncode != 0 or not any(t.startswith("text/plain") for t in types.stdout.split()):
                return Snapshot(None)
            content = self._run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=TIMEOUT)
            return Snapshot(content.stdout if content.returncode == 0 else None)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("clipboard snapshot failed: %s", exc)
            return Snapshot(None)

    def set_text(self, text: str) -> None:
        try:
            cp = self._run(["wl-copy", "--type", "text/plain;charset=utf-8"], input=text, text=True,
                           capture_output=True, timeout=TIMEOUT)
        except FileNotFoundError as exc:
            raise ClipboardError("wl-copy not found; install wl-clipboard") from exc
        except subprocess.SubprocessError as exc:
            raise ClipboardError(f"wl-copy failed: {exc}") from exc
        if cp.returncode != 0:
            raise ClipboardError(f"wl-copy exited {cp.returncode}: {cp.stderr}")

    def restore(self, snap: Snapshot) -> bool:
        if snap.text is None:
            return False
        try:
            self.set_text(snap.text)
        except ClipboardError as exc:
            log.warning("clipboard restore failed: %s", exc)
            return False
        return True
