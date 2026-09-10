"""Daemon-side supervisor for the recording pill helper process.

`voice.ui.overlay` is a separate process (GTK4 through PyGObject) that the
daemon drives with one JSON object per line on its stdin. This module owns
everything the daemon needs to do that safely:

* find an interpreter that actually has PyGObject and spawn the helper there;
* write events without ever blocking the caller - levels arrive on the audio
  reader thread, and a stalled helper must never cost us a recording;
* throttle levels to the helper's ~30 fps redraw rate;
* survive the helper dying: log it once, restart it once, then stay quiet.

Nothing here raises to its caller. The overlay is decoration; dictation is not.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

#: One frame at the helper's redraw rate: at most one level per window.
FRAME_S = 1.0 / 30.0

#: Queued-but-unwritten messages. Deep enough for a stall of a few frames,
#: shallow enough that a hung helper cannot grow the daemon's memory.
QUEUE_MAX = 64

#: How long stop() gives the helper to drain and then to exit.
DRAIN_S = 0.2
STOP_WAIT_S = 1.0

#: The system interpreter, which is where PyGObject normally lives; the
#: project's virtualenv has no `gi` (it is a distro package, not a wheel).
SYSTEM_PYTHON = "/usr/bin/python3"

#: Printed by the probe below, one token per available piece.
_PROBE_SCRIPT = (
    "import gi\n"
    "gi.require_version('Gtk', '4.0')\n"
    "found = ['gtk4']\n"
    "try:\n"
    "    gi.require_version('Gtk4LayerShell', '1.0')\n"
    "    found.append('layer-shell')\n"
    "except Exception:\n"
    "    pass\n"
    "print(' '.join(found))\n"
)

PROBE_TIMEOUT_S = 10

#: Set to 1 to run the pill without gtk4-layer-shell, accepting that it takes
#: keyboard focus when it appears (see `plain_window_allowed`).
ALLOW_PLAIN_ENV = "VOICE_OVERLAY_ALLOW_PLAIN_WINDOW"


def repo_root() -> Path:
    """The directory holding the `voice` package, for the helper's PYTHONPATH."""
    return Path(__file__).resolve().parents[2]


def _probe(python: str) -> tuple[str, ...]:
    """Which GTK pieces `python` can import, as tokens (`gtk4`, `layer-shell`)."""
    try:
        done = subprocess.run([python, "-c", _PROBE_SCRIPT], capture_output=True,
                              text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("overlay probe of %s failed: %s", python, exc)
        return ()
    if done.returncode != 0:
        log.debug("overlay probe of %s: %s", python, done.stderr.strip()[-200:])
        return ()
    return tuple(done.stdout.split())


@dataclass(frozen=True)
class HelperProbe:
    """How (and whether) the overlay helper can be started on this machine."""

    command: list[str] | None
    features: tuple[str, ...] = ()
    reason: str = ""

    @property
    def layer_shell(self) -> bool:
        return "layer-shell" in self.features


def probe_helper() -> HelperProbe:
    """The command that runs the helper here, or why nothing can.

    The system interpreter first (`python3 -m voice.ui.overlay` with the repo on
    PYTHONPATH), then the installed `voice-overlay` console script for installs
    whose own interpreter does have PyGObject.
    """
    features = _probe(SYSTEM_PYTHON)
    if features:
        return HelperProbe([SYSTEM_PYTHON, "-m", "voice.ui.overlay"], features)
    script = shutil.which("voice-overlay")
    if script:
        features = _probe(sys.executable)
        if features:
            return HelperProbe([script], features)
    return HelperProbe(None, (), "no interpreter with PyGObject and GTK 4 "
                                f"({SYSTEM_PYTHON}, {sys.executable}); "
                                "install python-gobject / gtk4")


def helper_command() -> list[str] | None:
    return probe_helper().command


def default_launcher(position: str = "bottom", lang: str = "en",
                     popen: Callable = subprocess.Popen):
    """Spawn the helper, or return None when this machine cannot run it.

    The current environment is passed through unchanged (WAYLAND_DISPLAY,
    XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS - the helper needs all of them)
    with the repository prepended to PYTHONPATH so the system interpreter can
    import `voice.ui.overlay`.
    """
    probe = probe_helper()
    if probe.command is None:
        log.warning("recording overlay disabled: %s", probe.reason)
        return None
    strict = not plain_window_allowed()
    if strict and not probe.layer_shell:
        # GTK 4 dropped the accept-focus / focus-on-map hints, so a plain
        # toplevel *is* focused when it maps - and the paste chord would then
        # go to the pill instead of the user's window. No pill beats no
        # dictation. Set the env var above to see it anyway.
        log.warning("recording overlay disabled: gtk4-layer-shell is missing, and a plain "
                    "window would take keyboard focus and swallow the paste. Install it "
                    "(Arch: gtk4-layer-shell, Debian/Ubuntu: gir1.2-gtk4layershell-1.0) "
                    "or set %s=1 to accept that.", ALLOW_PLAIN_ENV)
        return None
    env = dict(os.environ)
    root = str(repo_root())
    env["PYTHONPATH"] = os.pathsep.join([root] + [p for p in [env.get("PYTHONPATH")] if p])
    cmd = list(probe.command)
    cmd += ["--position", position if position in ("bottom", "top") else "bottom"]
    if lang:
        cmd += ["--lang", lang]
    if strict:
        cmd.append("--require-layer-shell")     # in case the helper sees less than we did
    log.info("recording overlay: %s", " ".join(cmd))
    # stderr is inherited on purpose: the helper's own log lines (layer-shell
    # present or not, window mapped or not) belong in the daemon's log.
    return popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env=env)


class OverlayClient:
    """Supervises the helper process and feeds it the protocol.

    Thread-safe: `send` is called from the audio reader thread (levels), the
    Qt thread (language switches) and the dictation worker (states). Writes
    happen on a private thread, so none of those can block on a full pipe.
    """

    def __init__(self, enabled: bool, launcher: Callable[[], subprocess.Popen] | None = None,
                 clock: Callable[[], float] | None = None):
        self.enabled = bool(enabled)
        self._launcher = launcher or default_launcher
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._proc = None
        self._queue: queue.Queue = queue.Queue(QUEUE_MAX)
        self._writer: threading.Thread | None = None
        self._inflight = 0
        self._idle = threading.Event()
        self._idle.set()
        self._dead = False
        self._stopped = False
        self._restarts = 0
        self._pending_level: dict | None = None
        self._last_level: float | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Spawn the helper. Safe to call twice; never raises."""
        with self._lock:
            if not self.enabled or self._stopped or self._proc is not None:
                return
            self._spawn()

    def restart(self) -> None:
        """Explicitly bring the helper back after it was given up on."""
        with self._lock:
            self._close(self._proc)
            self._proc = None
            self._dead = False
            self._stopped = False
            self._restarts = 0
            self._pending_level = None
            if self.enabled:
                self._spawn()

    def stop(self) -> None:
        """Close the helper's stdin - its cue to exit - and reap it."""
        with self._lock:
            self._stopped = True                  # no new messages from here on
            self._pending_level = None
        self.flush(DRAIN_S)                       # let the writer drain first: it
        with self._lock:                          # needs self._proc to write at all
            proc, self._proc = self._proc, None
        try:
            self._queue.put_nowait(None)          # release the writer thread
        except queue.Full:
            pass
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.join(timeout=DRAIN_S)
        self._close(proc)

    def _close(self, proc) -> None:
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            log.debug("closing the overlay helper's stdin failed", exc_info=True)
        try:
            proc.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            log.warning("overlay helper did not exit; terminating it")
            try:
                proc.terminate()
            except Exception:
                log.debug("terminating the overlay helper failed", exc_info=True)
        except Exception:
            log.debug("waiting for the overlay helper failed", exc_info=True)

    def _spawn(self) -> bool:
        """Caller holds the lock. Returns whether a helper is now running."""
        try:
            proc = self._launcher()
        except Exception as exc:
            proc = None
            log.warning("recording overlay disabled: cannot start the helper (%s)", exc)
        if proc is None:
            # Either the launcher said "not on this machine" (and logged why) or
            # it blew up above. Either way there is nothing to retry.
            self.enabled = False
            self._dead = True
            return False
        self._proc = proc
        self._dead = False
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._pump, name="overlay-writer", daemon=True)
            self._writer.start()
        return True

    # -- protocol -----------------------------------------------------------
    def send(self, message: dict) -> None:
        """Queue one protocol message. Never raises, never blocks."""
        try:
            self._send(message)
        except Exception:                       # a broken overlay costs nothing
            log.debug("overlay send failed", exc_info=True)

    def _send(self, message: dict) -> None:
        if not self.enabled or self._stopped:
            return
        now = self._clock()
        with self._lock:
            if not self._alive():
                return
            if _is_level(message):
                self._pending_level = message
                if not self._window_open(now):
                    return                      # dropped; the newest one waits
                self._flush_level(now)
                return
            self._flush_level(now)              # keep levels ahead of states
            self._pending_level = None
            self._enqueue(message)

    def _window_open(self, now: float) -> bool:
        return self._last_level is None or (now - self._last_level) >= FRAME_S

    def _flush_level(self, now: float) -> None:
        if self._pending_level is None or not self._window_open(now):
            return
        level, self._pending_level = self._pending_level, None
        self._last_level = now
        self._enqueue(level)

    def _enqueue(self, message: dict) -> None:
        line = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            log.debug("overlay queue full; dropping %s", message)
            return
        self._inflight += 1
        self._idle.clear()

    def _alive(self) -> bool:
        """Caller holds the lock. Restarts a helper that exited, once."""
        if self._dead or self._proc is None:
            return False
        if self._proc.poll() is None:
            return True
        code = self._proc.returncode
        if self._restarts >= 1:
            self._die(f"helper exited again ({code}); overlay stays off")
            return False
        self._restarts += 1
        log.warning("recording overlay helper exited (%s); restarting it once", code)
        self._close(self._proc)
        self._proc = None
        self._last_level = None
        return self._spawn()

    def _die(self, reason: str) -> None:
        with self._lock:
            if self._dead:
                return
            self._dead = True
        log.warning("recording overlay: %s", reason)

    # -- writer thread ------------------------------------------------------
    def _pump(self) -> None:
        while True:
            line = self._queue.get()
            try:
                if line is None:
                    return
                self._write(line)
            finally:
                self._queue.task_done()
                with self._lock:
                    self._inflight = max(0, self._inflight - 1)
                    if self._inflight == 0:
                        self._idle.set()

    def _write(self, line: bytes) -> None:
        with self._lock:
            proc = self._proc
        stdin = getattr(proc, "stdin", None) if proc is not None else None
        if stdin is None:
            return
        try:
            stdin.write(line)
            stdin.flush()
        except Exception as exc:
            self._die(f"cannot write to the helper ({exc})")

    def flush(self, timeout: float = 1.0) -> bool:
        """Wait for queued messages to reach the helper. For stop() and tests."""
        return self._idle.wait(timeout)


def plain_window_allowed() -> bool:
    """Whether the user has accepted a pill that takes focus (see the env var)."""
    return os.environ.get(ALLOW_PLAIN_ENV, "").strip().lower() in ("1", "true", "yes")


def _is_level(message: dict) -> bool:
    return "level" in message and "state" not in message and "language" not in message
