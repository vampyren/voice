"""Daemon-side supervisor for the recording pill helper process.

`voice.ui.overlay` is a separate process (GTK4 through PyGObject) that the
daemon drives with one JSON object per line on its stdin. This module owns
everything the daemon needs to do that safely:

* find an interpreter that actually has PyGObject and spawn the helper there;
* write events without ever blocking the caller - levels arrive on the audio
  reader thread, and a stalled helper must never cost us a recording;
* throttle levels to the helper's ~30 fps redraw rate;
* read the one thing the helper answers - how long its progress fill still
  needs - without ever depending on it answering;
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

from voice.ui.placement import (DEFAULT_MARGIN_X, DEFAULT_MARGIN_Y, DEFAULT_POSITION,
                                clamp_margin, normalise_position)

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
#: pycairo is checked as well as `gi`: the helper draws the pill through
#: voice.ui.overlay_draw, which does `import cairo`, and the two are separate
#: distro packages - PyGObject does not pull pycairo in. Without this line an
#: interpreter with gi and no pycairo probes healthy, `voice doctor` says so,
#: and every spawn dies on the import instead.
_PROBE_SCRIPT = (
    "import gi\n"
    "import cairo\n"
    "gi.require_version('Gtk', '4.0')\n"
    "found = ['gtk4']\n"
    "try:\n"
    "    gi.require_version('Gtk4LayerShell', '1.0')\n"
    "except Exception:\n"
    "    pass\n"
    "else:\n"
    "    token = 'layer-shell'\n"
    "    try:\n"
    # Importing Gtk opens the display, which is what is_supported() reads.
    "        from gi.repository import Gtk, Gtk4LayerShell\n"
    "        if Gtk4LayerShell.is_supported() is False:\n"
    "            token = 'layer-shell-unsupported'\n"
    "    except Exception:\n"
    "        pass\n"
    "    found.append(token)\n"
    "print(' '.join(found))\n"
)

#: The typelib is installed but the compositor has no zwlr_layer_shell_v1, so
#: no window can refuse focus here. For the pill that is the same as absent.
LAYER_SHELL_UNSUPPORTED = "layer-shell-unsupported"

PROBE_TIMEOUT_S = 10

#: The helper's exit code for "--require-layer-shell was given and there is no
#: layer-shell here". Nothing about that is retryable.
NO_LAYER_SHELL_EXIT = 2

#: What `voice status` shows for an overlay that gave up for that reason.
NO_LAYER_SHELL_STATUS = "disabled: no layer-shell"

#: Queue item meaning "the helper is gone; bring it back before the next line".
_RESPAWN = object()

#: The one answer the helper gives (see `voice.ui.overlay`): how many seconds
#: its progress fill still needs, 0 for "nothing is animating".
FINISH_ACK = "finish"


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

    @property
    def layer_shell_unsupported(self) -> bool:
        """Installed, and useless: this compositor cannot make a layer surface."""
        return LAYER_SHELL_UNSUPPORTED in self.features


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
    return HelperProbe(None, (), "no interpreter with PyGObject, GTK 4 and pycairo "
                                f"({SYSTEM_PYTHON}, {sys.executable}); "
                                "install python-gobject / gtk4 / pycairo")


def helper_command() -> list[str] | None:
    return probe_helper().command


def pill_takes_focus(config, probe: Callable[[], object] | None = None) -> bool:
    """True when the pill on screen can only be a window that steals the keyboard.

    GTK 4 dropped the "do not focus me" hints, so only a layer-shell surface can
    refuse focus - and gtk4-layer-shell being installed is not enough, the
    compositor has to implement it, which GNOME does not. Three things have to
    line up for the pill to be in the paste's way: it is switched on, this
    desktop cannot give it a layer surface, and the user allowed the fallback
    window anyway. Without that last one the helper exits instead of showing
    one, so there is no pill to work around - and the config alone answers
    that, which is why the probe (two interpreters, up to ten seconds each) is
    only ever run for the people it can actually affect.
    """
    if not config.get("ui.overlay", True):
        return False
    if not config.get("ui.overlay_allow_fallback", False):
        return False
    # Resolved here rather than as a default argument: a default would bind the
    # module's own `cached_probe` once, at import, and no test could substitute it.
    found = (probe or cached_probe)()
    return getattr(found, "command", None) is not None and not getattr(found, "layer_shell", False)


#: probe_helper() spawns interpreters and waits up to PROBE_TIMEOUT_S each. What
#: it measures - which packages are installed - cannot change under a running
#: daemon, so it is answered once and then remembered: a helper that dies mid
#: dictation is respawned without paying for the probe again.
_PROBE_CACHE: list[HelperProbe] = []
_PROBE_CACHE_LOCK = threading.Lock()


def cached_probe() -> HelperProbe:
    """`probe_helper()`, run at most once in this process."""
    with _PROBE_CACHE_LOCK:
        if not _PROBE_CACHE:
            _PROBE_CACHE.append(probe_helper())
        return _PROBE_CACHE[0]


def reset_probe_cache() -> None:
    """Forget the remembered probe. For tests, which stub the interpreters."""
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE.clear()


def default_launcher(position: str = DEFAULT_POSITION, lang: str = "en", verbose: bool = False,
                     allow_fallback: bool = False, margin_x: object = DEFAULT_MARGIN_X,
                     margin_y: object = DEFAULT_MARGIN_Y, popen: Callable = subprocess.Popen):
    """Spawn the helper, or return None when this machine cannot run it.

    The current environment is passed through unchanged (WAYLAND_DISPLAY,
    XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS - the helper needs all of them)
    with the repository prepended to PYTHONPATH so the system interpreter can
    import `voice.ui.overlay`.
    """
    probe = cached_probe()
    if probe.command is None:
        log.warning("recording overlay disabled: %s", probe.reason)
        return None
    env = dict(os.environ)
    root = str(repo_root())
    env["PYTHONPATH"] = os.pathsep.join([root] + [p for p in [env.get("PYTHONPATH")] if p])
    cmd = list(probe.command)
    # Normalised here as well as in the config: this is the command line that
    # ends up in the daemon's log, and an owner reading it back should see one
    # of the nine, not whatever their file happens to hold.
    cmd += ["--position", normalise_position(position),
            "--margin-x", str(clamp_margin(margin_x, DEFAULT_MARGIN_X)),
            "--margin-y", str(clamp_margin(margin_y, DEFAULT_MARGIN_Y))]
    if lang:
        cmd += ["--lang", lang]
    if not allow_fallback:
        # GTK 4 dropped the accept-focus / focus-on-map hints, so a fallback
        # toplevel *is* focused when it maps - and the paste chord would then go
        # to the pill instead of the user's window. The helper exits 2 instead.
        cmd.append("--require-layer-shell")
    if verbose:
        cmd.append("--verbose")                 # the helper then logs every state it shows
    log.info("recording overlay: %s", " ".join(cmd))
    # stderr is inherited on purpose: the helper's own log lines (layer-shell
    # present or not, window mapped or not) belong in the daemon's log.
    # stdout is a pipe, not DEVNULL: the helper answers a `finish` with how long
    # its fill really needs, and the paste waits for that instead of guessing.
    return popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)


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
        #: Set by stop(), and never cleared: what actually ends the writer thread.
        #: A queued sentinel can be dropped by a full queue, and then the pipe
        #: would stay open on a thread parked in queue.get() forever.
        self._halt = threading.Event()
        self._status = "not started" if self.enabled else "off"
        self._restarts = 0
        self._pending_level: dict | None = None
        self._last_level: float | None = None
        #: Bumped on every spawn, so a write that fails after the process it was
        #: aimed at has been replaced cannot condemn its replacement.
        self._generation = 0
        self._respawn_queued = False
        #: The helper's last answer to a `finish`, as (seconds it needs, when it
        #: said so), and the wait that ends when it arrives. Armed by
        #: `expect_finish`, so an answer nobody is waiting for is dropped rather
        #: than kept for whatever asks next.
        self._finish_reply: tuple[float, float] | None = None
        self._finish_expected = False
        self._finish_said = threading.Event()

    # -- lifecycle ----------------------------------------------------------
    def status(self) -> str:
        """One line for `voice status`, polling the helper as a side effect.

        Polling here is the point: nothing else notices that the helper exited
        (and, on a first exit, restarts it) until the next message goes out.
        """
        with self._lock:
            if self.enabled and not self._stopped and self._proc is not None:
                # `voice status` is a user command on its own thread, so unlike
                # send() it can afford the spawn itself.
                self._alive(spawn_here=True)
            return self._status

    def start(self) -> None:
        """Spawn the helper. Safe to call twice; never raises."""
        with self._lock:
            if not self.enabled or self._stopped or self._proc is not None:
                return
            self._spawn()

    def stop(self) -> None:
        """Close the helper's stdin - its cue to exit - and reap it."""
        with self._lock:
            self._stopped = True                  # no new messages from here on
            self._pending_level = None
            if not self._dead:
                self._status = "stopped"
        self.flush(DRAIN_S)                       # let the writer drain first: it
        with self._lock:                          # needs self._proc to write at all
            proc, self._proc = self._proc, None
            writer = self._writer
            # The event, not the sentinel, is what ends the pump; draining first
            # then guarantees the wake-up fits, and keeps _inflight honest for
            # anyone who calls flush() after us.
            # This is the only place _halt is set, and the _put below is the only
            # thing that wakes the pump to notice it: the pump reads the flag when
            # an item arrives, never while parked in queue.get(). Drain first so
            # the wake-up always fits, and keep both halves together - a _halt set
            # without one leaves the writer parked and the helper's stdin open.
            self._halt.set()
            self._drain()
            if writer is not None and writer.is_alive():
                self._put(None)               # a wake-up for a parked queue.get()
        if writer is not None:
            writer.join(timeout=DRAIN_S)
            if writer.is_alive():
                # The writer is parked in write()/flush() on a pipe the helper
                # stopped reading, holding the BufferedWriter's lock. Closing
                # stdin would queue behind that lock and never return, so the
                # child goes first: losing the read end EPIPEs the write.
                self._unblock(proc)
                writer.join(timeout=STOP_WAIT_S)
        finished = writer is None or not writer.is_alive()
        with self._lock:
            if finished:
                self._writer = None           # the pump is gone; forget it
                self._drain()                 # nothing is left to write it away
        # Never close stdin under a live writer thread - that is the deadlock.
        self._close(proc, close_stdin=finished)

    def _drain(self) -> None:
        """Caller holds the lock. Drop what is queued, keeping _inflight true."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            self._inflight = max(0, self._inflight - 1)
        if self._inflight == 0:
            self._idle.set()

    def _unblock(self, proc) -> None:
        """Signal the helper until it is gone, and reap it.

        Also what releases a writer parked on the helper's full pipe: losing the
        read end EPIPEs the write.
        """
        if proc is None:
            return
        log.debug("overlay helper has not exited; signalling it")
        for signal_it in (getattr(proc, "terminate", None), getattr(proc, "kill", None)):
            if signal_it is None:
                continue
            try:
                signal_it()
                proc.wait(timeout=STOP_WAIT_S)
                return
            except subprocess.TimeoutExpired:
                continue                      # SIGTERM ignored; escalate to kill
            except Exception:
                # A terminate() that raises (a PID already gone, an EPERM) says
                # nothing about the child being dead, so escalate rather than
                # walk away: giving up here leaves the writer parked forever.
                log.debug("signalling the overlay helper failed", exc_info=True)
                continue

    def _close(self, proc, close_stdin: bool = True) -> None:
        if proc is None:
            return
        try:
            if close_stdin and proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            log.debug("closing the overlay helper's stdin failed", exc_info=True)
        try:
            proc.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            log.warning("overlay helper did not exit; terminating it")
            # Signal *and* reap: a bare terminate() leaves a zombie behind for
            # the daemon's lifetime, and a SIGTERM it ignores leaves it running.
            self._unblock(proc)
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
            self._status = "disabled: no helper"
            return False
        self._proc = proc
        self._generation += 1
        self._dead = False
        self._status = "running"
        stdout = getattr(proc, "stdout", None)
        if stdout is not None:
            # One reader per helper, ending when its pipe does - which is the
            # only thing that ever ends it. Nothing closes that pipe from
            # another thread: `close()` takes the buffer's own lock, which a
            # parked `readline` is holding, so closing it under this thread
            # would block until the helper wrote a line or died - the same
            # deadlock the writer side above is built to avoid. Losing the
            # helper (it exits, or is signalled) is an EOF, and the thread
            # ends on it.
            threading.Thread(target=self._listen, args=(stdout, self._generation),
                             name="overlay-reader", daemon=True).start()
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
        if not self._put(line):
            log.debug("overlay queue full; dropping %s", message)

    def _put(self, item) -> bool:
        """Caller holds the lock. Hand one item to the writer thread."""
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            return False
        self._inflight += 1
        self._idle.clear()
        return True

    def _alive(self, spawn_here: bool = False) -> bool:
        """Caller holds the lock. Restarts a helper that exited, once.

        `spawn_here` decides *where*: send() runs on the audio reader thread, so
        it only asks the writer thread for the restart and returns.
        """
        if self._dead or self._proc is None:
            return False
        if self._proc.poll() is None:
            return True
        if self._respawn_queued:
            return True                     # the writer is already bringing it back
        code = self._proc.returncode
        if code == NO_LAYER_SHELL_EXIT:
            # The helper refused to show a window that would take the focus.
            # Restarting it would only reproduce that, so the pill stays off.
            self.enabled = False
            self._let_go()
            self._die(NO_LAYER_SHELL_STATUS,
                      "overlay disabled: no layer-shell; install gtk4-layer-shell or set "
                      "ui.overlay_allow_fallback = true")
            return False
        if self._restarts >= 1:
            self._let_go()
            self._die(f"disabled: helper exited ({code})",
                      f"recording overlay: helper exited again ({code}); it stays off")
            return False
        if spawn_here:
            self._restarts += 1
            log.warning("recording overlay helper exited (%s); restarting it once", code)
            proc, self._proc = self._proc, None
            self._last_level = None
            self._close(proc)
            return self._spawn()
        if not self._queue_respawn():
            return False                    # nothing was spent; the next send retries
        log.warning("recording overlay helper exited (%s); restarting it once", code)
        return True

    def _let_go(self) -> None:
        """Caller holds the lock. Drop a helper we have given up on for good.

        Without this its stdin fd and its process entry are held until the
        daemon exits, which is days. Only ever reached for a helper whose
        `poll()` already returned, so the close cannot park behind a writer
        still holding the buffer lock: the dead child's read end is gone and
        any parked write has been released with EPIPE.
        """
        proc, self._proc = self._proc, None
        self._close(proc)

    def _queue_respawn(self) -> bool:
        """Caller holds the lock. Ask the writer thread to bring the helper back.

        Queued rather than done here so the message that noticed the death is
        written to the replacement, in order, and so no caller ever pays for a
        spawn: `send` runs on the pw-record reader thread.
        """
        if self._respawn_queued:
            return True
        if not self._put(_RESPAWN):
            # The budget is deliberately untouched: no replacement was asked for,
            # so the next send() gets to ask again once the queue has room.
            log.debug("overlay queue full; not restarting the helper yet")
            return False
        self._respawn_queued = True
        self._restarts += 1
        return True

    def _respawn(self) -> None:
        """Writer thread: replace the helper the queue told us to give up on."""
        with self._lock:
            self._respawn_queued = False
            if self._dead or self._stopped or not self.enabled:
                return
            proc, self._proc = self._proc, None
            self._last_level = None
        self._close(proc)                   # bounded, and never under the lock
        with self._lock:
            if not (self._dead or self._stopped) and self._proc is None:
                self._spawn()

    def _die(self, status: str, message: str) -> None:
        """Give up on the helper, saying so exactly once."""
        with self._lock:
            if self._dead:
                return
            self._dead = True
            self._status = status
        log.warning("%s", message)

    # -- what the helper answers --------------------------------------------
    def expect_finish(self) -> None:
        """Arm the wait for the next `finish` answer. Called before the send.

        Armed rather than assumed, so an answer that arrives with nobody
        waiting - a late one, from a request already given up on - is dropped
        instead of being handed to whatever asks next.
        """
        with self._lock:
            self._finish_reply = None
            self._finish_expected = True
            self._finish_said.clear()

    def finish_wait(self, timeout: float) -> float | None:
        """Seconds the helper's fill still needs, or None if it did not say.

        None is the whole point of the timeout: an older helper, or one that is
        wedged, must cost the paste a short wait and then leave the caller to
        its own estimate rather than hanging the insertion on a decoration.
        """
        with self._lock:
            if not self._finish_expected:
                return None
            if self._dead or self._stopped or self._proc is None:
                return None
        if not self._finish_said.wait(max(0.0, timeout)):
            return None
        with self._lock:
            reply = self._finish_reply
        if reply is None:
            return None
        seconds, said_at = reply
        # Only the remainder: everything the insertion has done since the helper
        # answered has been running while the fill did.
        return max(0.0, seconds - (self._clock() - said_at))

    def _listen(self, stream, generation: int) -> None:
        """Reader thread: one line at a time until the helper's pipe closes."""
        try:
            for line in iter(stream.readline, b""):
                if not line:
                    break
                self._answered(line, generation)
        except Exception:
            # A closed pipe, a helper that was replaced, a half-written line:
            # none of it is worth more than a debug line. The pill is decoration.
            log.debug("overlay reader stopped", exc_info=True)

    def _answered(self, line, generation: int) -> None:
        """One line from the helper. Anything we do not understand is dropped."""
        try:
            text = line.decode() if isinstance(line, bytes) else str(line)
            message = json.loads(text)
        except Exception:
            log.debug("overlay: ignoring what the helper said (%r)", line[:120])
            return
        if not isinstance(message, dict) or message.get("ack") != FINISH_ACK:
            return
        try:
            seconds = max(0.0, float(message.get("seconds", 0.0)))
        except (TypeError, ValueError):
            log.debug("overlay: helper answered a bad duration %r", message.get("seconds"))
            return
        with self._lock:
            if generation != self._generation or not self._finish_expected:
                return              # a different helper, or nobody is waiting
            self._finish_reply = (seconds, self._clock())
            self._finish_said.set()

    # -- writer thread ------------------------------------------------------
    def _pump(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if self._halt.is_set():
                    return                  # stop() sets it once, and never clears it
                if item is None:
                    continue                # defensive: a wake-up is not a message
                if item is _RESPAWN:
                    self._respawn()
                else:
                    self._write(item)
            finally:
                self._queue.task_done()
                with self._lock:
                    self._inflight = max(0, self._inflight - 1)
                    if self._inflight == 0:
                        self._idle.set()

    def _write(self, line: bytes) -> None:
        with self._lock:
            proc, generation = self._proc, self._generation
        stdin = getattr(proc, "stdin", None) if proc is not None else None
        if stdin is None:
            return
        try:
            stdin.write(line)
            stdin.flush()
        except Exception as exc:
            self._write_failed(generation, exc)

    def _write_failed(self, generation: int, exc: Exception) -> None:
        """Writer thread: the helper stopped taking writes. That is it dying.

        Treated exactly like an exit, restart-once included - anything else
        would leave a helper that broke mid-message off for the session.
        """
        with self._lock:
            if generation != self._generation:
                # The process was swapped while this write was in flight (a
                # respawn, or an earlier failure): closing the old stdin is what
                # raised, and it says nothing about the helper running now.
                log.debug("overlay write to a replaced helper failed (%s)", exc)
                return
            if self._dead or self._stopped:
                return
            if self._respawn_queued:
                # _alive() already saw this helper go and asked for its
                # replacement; this failed write is that same death reaching us
                # a second time, and it must not cost a second restart.
                log.debug("overlay write to a helper already being replaced failed (%s)", exc)
                return
            if self._restarts >= 1:
                self._die("disabled: helper not writable",
                          f"recording overlay: cannot write to the helper ({exc}); it stays off")
                return
            self._restarts += 1
            # Claimed before the lock is dropped, so a send() racing the respawn
            # below sees a replacement coming instead of counting the death again.
            self._respawn_queued = True
        log.warning("recording overlay: the helper stopped reading (%s); restarting it once", exc)
        self._respawn()

    def flush(self, timeout: float = 1.0) -> bool:
        """Wait for queued messages to reach the helper. For stop() and tests."""
        return self._idle.wait(timeout)


def _is_level(message: dict) -> bool:
    return "level" in message and "state" not in message and "language" not in message
