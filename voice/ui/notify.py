"""Desktop notifications through notify-send (libnotify)."""
from __future__ import annotations

import logging
import subprocess
from typing import Callable

from voice import APP_NAME

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, enabled: bool = True, run: Callable | None = None):
        self._enabled = enabled
        # Looked up when it is used, not bound here: a default argument would
        # capture subprocess.Popen at import time, where nothing can intercept
        # it - and the test suite must never reach the owner's desktop.
        self._run = run

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def notify(self, title: str, body: str, urgency: str = "normal") -> None:
        if not self._enabled:
            return
        argv = ["notify-send", "--app-name", APP_NAME, "--urgency", urgency, "--expire-time", "4000", title, body]
        try:
            run = self._run or subprocess.Popen
            run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("notify-send unavailable: %s", exc)
