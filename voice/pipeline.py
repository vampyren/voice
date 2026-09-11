"""The dictation state machine: record -> trim -> transcribe -> replace -> inject -> history."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import numpy as np

from voice.audio.capture import capture_sources
from voice.audio.pcm import SAMPLE_RATE, duration_s
from voice.audio.vad import MIN_SPEECH_MS, trim_silence
from voice.history import Entry, History
from voice.stt.base import TranscriptionError
from voice.text import apply_replacements, normalize_text

log = logging.getLogger(__name__)

#: Said once when the recording pill has the keyboard and the paste was
#: therefore not attempted. It has to name the pill: "could not paste" would
#: send the owner off debugging the portal instead.
PILL_FOCUS_NOTICE = ("The pill takes focus on this desktop, so the text is on "
                     "the clipboard - press Ctrl+V.")

#: How long a transcription may run before the attempt is abandoned, when
#: `[stt] timeout_seconds` says nothing. Nothing used to bound one at all: the
#: owner's daemon sat in `transcribing` for many minutes with the tray amber,
#: and only a restart cleared it.
#:
#: Five minutes is deliberately generous. The longest recording the daemon
#: will take is `audio.max_seconds`, 120 s by default, and the slow case is
#: the local backend on a CPU: large-v3-turbo under CTranslate2 int8 runs at
#: roughly real time on a few cores, a beam of 5 and a cold model load costing
#: tens of seconds on top. 300 s is therefore about 2.5x the worst honest case
#: for a full-length recording, and ten times what the owner's 29 s recording
#: should ever have needed - long enough that a slow machine is never cut off
#: mid-sentence, short enough that a wedged one comes back on its own.
DEFAULT_STT_TIMEOUT = 300.0

#: The backstop: how long past its own limit a state may live before the
#: pipeline gives up on it. Recording is bounded by `audio.max_seconds`, which
#: has its own timer; this is what catches that timer never firing.
STUCK_GRACE = 30.0

#: The bound on an insertion. Every step of one - the clipboard, waiting for
#: the hotkey modifiers to clear, the paste chord and the pill's settle - is
#: sub-second by design, so a minute means something is wedged.
INJECT_BOUND = 60.0

#: Said when PipeWire has no capture device at all. `pw-record` does not fail
#: in that case: with nothing to link to it sits there producing no audio (24
#: bytes in four seconds, measured), and with only a monitor to fall back on
#: it records silence - either way it exits with 0 and says nothing, which is
#: how a dictation with no microphone became a transcription of nothing.
NO_MICROPHONE = "No microphone found"
NO_MICROPHONE_BODY = ("PipeWire has no capture device. Connect a microphone and "
                      "try again - nothing was recorded.")


class State(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    INJECTING = "injecting"
    ERROR = "error"


@dataclass
class Services:
    recorder: Any
    transcriber: Any
    injector: Any
    history: History
    notify: Callable[..., None]
    config_getter: Callable[..., Any]
    prompt_getter: Callable[[], str | None] = lambda: None
    #: The user's vocabulary, read fresh for each dictation so an edit in the
    #: settings window applies to the next one without a restart.
    hotwords_getter: Callable[[], str | None] = lambda: None
    trim: Callable[[np.ndarray], np.ndarray] = trim_silence
    #: Every capture source PipeWire knows about, or None when it could not be
    #: asked at all. The two answers are not the same: see `_no_microphone`.
    sources: Callable[[], list | None] = capture_sources


class Worker:
    """One daemon thread running submitted jobs in order.

    Deliberately not a ThreadPoolExecutor: its worker threads are *not* daemon
    threads, and concurrent.futures joins them from an atexit hook - so a
    transcription or a paste still in flight held the whole process open after
    quit, with the IPC socket already unlinked and nothing left to reach it
    with. Started on first use, because most daemons never transcribe anything.
    """

    def __init__(self, name: str = "dictation"):
        self._name = name
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def submit(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, args=(self._jobs,),
                                                name=self._name, daemon=True)
                self._thread.start()
            self._jobs.put(fn)

    def _run(self, jobs: queue.Queue) -> None:
        while True:
            fn = jobs.get()
            if fn is None:                    # shutdown()
                return
            try:
                fn()
            except Exception:                 # the pipeline nets its own errors
                log.exception("dictation job failed")

    def shutdown(self) -> None:
        """Stop the worker, dropping whatever is queued behind the running job.

        Quitting must not wait on a transcription nobody will ever see; the job
        already running finishes into the void, on a thread that cannot hold
        the interpreter open.
        """
        self.abandon()

    def abandon(self) -> None:
        """Give up on the thread in flight; the next submit starts a fresh one.

        A local transcription cannot be interrupted - it is inside
        faster-whisper, on this thread, and killing it would take the model
        (and quite possibly the process) with it. Abandoning one therefore
        means abandoning its thread: it is left to finish into the void, it
        exits when it does because the sentinel is already in its queue, and
        everything after it - a retry, the next dictation - runs on a new
        thread instead of queueing behind a result nobody wants.
        """
        with self._lock:
            thread, self._thread = self._thread, None
            jobs, self._jobs = self._jobs, queue.Queue()
        if thread is None:
            return
        while True:
            try:
                jobs.get_nowait()
            except queue.Empty:
                break
        jobs.put(None)


#: The IDLE detail a finished dictation carries: how much text, how it reached
#: the window, and how long it took. The daemon reads the method back out of it
#: to decide what the pill says, so the two halves live together here.
_METHOD_RE = re.compile(r"\bvia\s+(\S+)\s+in\b")


def idle_detail(chars: int, method: str, elapsed_s: float) -> str:
    return f"{chars} chars via {method} in {elapsed_s:.1f}s"


def detail_method(detail: str) -> str:
    """The insertion method named in an IDLE detail, or "" if it names none."""
    found = _METHOD_RE.search(detail or "")
    return found.group(1) if found else ""


class Dictation:
    def __init__(self, services: Services, executor: Callable | None = None, timer_factory=threading.Timer):
        self.sv = services
        self._timer_factory = timer_factory
        self._timer = None
        self._lock = threading.RLock()
        self._state = State.IDLE
        self.on_state: Callable[[State, str], None] = lambda s, d: None
        self.last_error: str | None = None
        #: The focus-stealing-pill explanation is worth saying once per daemon
        #: run, not once per dictation: it describes the desktop, and nothing
        #: about it changes between two sentences.
        self._pill_notice_shown = False
        #: The transcription attempt in flight. Abandoning one moves the number
        #: on, which is how a result that lands afterwards is recognised as
        #: belonging to nobody and dropped.
        self._attempt = 0
        #: The audio of the attempt in flight, so whatever abandons it can hand
        #: it back to history for "Retry last recording". Set under the lock by
        #: whichever command started the attempt, so there is no window in which
        #: a cancel finds nothing to keep; the worker swaps in the trimmed form
        #: once it has one, and `_audio_trimmed` says which of the two it is.
        self._audio: np.ndarray | None = None
        self._audio_trimmed = False
        #: The backstop timer for the state on screen, and its generation: a
        #: guard that fires after its state has moved on must do nothing.
        self._guard = None
        self._guard_gen = 0
        # Owned by this pipeline rather than the module, so quitting can end it.
        self._worker = Worker()
        self._executor = executor or self._worker.submit

    def shutdown(self) -> None:
        self._disarm_guard()
        self._worker.shutdown()

    # -- state ----------------------------------------------------------------
    @property
    def state(self) -> State:
        return self._state

    def _set(self, state: State, detail: str = "") -> None:
        self._state = state
        self._arm_guard(state)
        log.debug("state %s %s", state.value, detail)
        try:
            self.on_state(state, detail)
        except Exception:
            log.exception("on_state callback failed")

    # -- the backstop ---------------------------------------------------------
    #: No state may be terminal. Every non-idle state is entered with a timer
    #: behind it, and a state that outlives its bound goes back to idle with a
    #: notification, whatever wedged it. This is what would have saved the
    #: owner's daemon regardless of where it actually got stuck.

    def _guard_bound(self, state: State) -> float | None:
        """How long `state` may last, or None for a state that may wait."""
        if state is State.TRANSCRIBING:
            return self._stt_timeout()
        if state is State.RECORDING:
            # The recording has its own timer at audio.max_seconds; this is the
            # one that catches that timer not firing.
            return self._max_seconds() + STUCK_GRACE
        if state is State.INJECTING:
            return INJECT_BOUND
        # IDLE is the pipeline waiting for its owner, which it may do for ever,
        # and ERROR is a step on the way back to IDLE rather than a state to
        # sit in - `_fail` leaves it in the same breath it enters it.
        return None

    def _stt_timeout(self) -> float:
        """`[stt] timeout_seconds`, or the default when it says nothing usable."""
        try:
            value = float(self.sv.config_getter("stt.timeout_seconds", DEFAULT_STT_TIMEOUT))
        except (TypeError, ValueError):
            return DEFAULT_STT_TIMEOUT
        return value if value > 0 else DEFAULT_STT_TIMEOUT

    def _max_seconds(self) -> float:
        try:
            value = float(self.sv.config_getter("audio.max_seconds", 120))
        except (TypeError, ValueError):
            return 120.0
        return value if value > 0 else 120.0

    def _disarm_guard(self) -> None:
        """Drop the guard in flight; one that fires after this does nothing.

        The generation moves on rather than relying on the cancel: a
        `threading.Timer` already inside its callback cannot be cancelled, and
        that is precisely the race this has to survive.
        """
        with self._lock:
            timer, self._guard = self._guard, None
            self._guard_gen += 1
            if timer is None:
                return
        try:
            timer.cancel()
        except Exception:
            log.exception("could not cancel the state guard")

    def _arm_guard(self, state: State) -> None:
        with self._lock:
            self._disarm_guard()
            generation, bound = self._guard_gen, self._guard_bound(state)
            if bound is None:
                return
            try:
                timer = self._timer_factory(bound, lambda: self._on_guard(state, generation, bound))
                timer.start()
            except Exception:
                # A dictation without its backstop still works; one that cannot
                # leave start() does not. The failure is worth a line, not a stop.
                log.exception("could not arm the %s guard", state.value)
                return
            self._guard = timer

    def _on_guard(self, state: State, generation: int, bound: float) -> None:
        """`state` has outlived `bound`. Whatever it was doing, stop waiting."""
        with self._lock:
            if generation != self._guard_gen or self._state is not state:
                return                      # the pipeline moved on without it
            self._guard = None
            log.warning("stuck in %s for %.0fs; returning to idle", state.value, bound)
            if state is State.TRANSCRIBING:
                self._drop_attempt()
                # The message leads with the two words that matter: the pill
                # has room for about twenty characters of it, the notification
                # carries the rest.
                self._fail(f"transcription timed out after {bound:.0f}s and was "
                           'abandoned - the recording is kept, use "Retry last '
                           'recording" to try it again.')
                return
            if state is State.RECORDING:
                if self._timer:
                    self._timer.cancel()
                self._cancel_recorder()
            self._fail(f"stuck in {state.value} for more than {bound:.0f}s; back to idle")

    # -- the transcription in flight ------------------------------------------
    def _next_attempt(self, audio: np.ndarray, trimmed: bool) -> int:
        """Number the attempt that is about to start. Caller holds the lock.

        The recording comes with it. It used to be handed over inside the
        worker instead, after the VAD pass, which left a window between here
        and there in which a cancel found `self._audio` at None: the recording
        just made was dropped, and whatever history was still holding - a
        recording from hours ago - became what "Retry last recording" would
        re-run. The VAD pass itself stays in the worker; it is far too slow to
        run here, on the listener thread, with the lock held.
        """
        self._attempt += 1
        self._audio, self._audio_trimmed = audio, trimmed
        return self._attempt

    def _current(self, attempt: int) -> bool:
        """Is `attempt` still the one the pipeline is waiting for?"""
        with self._lock:
            return attempt == self._attempt and self._state is State.TRANSCRIBING

    def _claim(self, attempt: int) -> bool:
        """Take delivery of `attempt`, once: False means it was abandoned.

        The number moves on as the result is claimed, so nothing can deliver
        the same attempt twice, and the guard is dropped here rather than at
        the next transition - the transcription is over, and the few lines of
        text handling between here and INJECTING are not a state to bound.
        """
        with self._lock:
            if attempt != self._attempt or self._state is not State.TRANSCRIBING:
                return False
            self._attempt += 1
            self._audio, self._audio_trimmed = None, False
            self._disarm_guard()
            return True

    def _drop_attempt(self) -> None:
        """Stop waiting on the transcription in flight. Caller holds the lock.

        The local backend cannot be interrupted, so this is all abandoning one
        can mean: the attempt number moves on (a result that lands later is
        recognised as nobody's and dropped), the worker thread is left to
        finish into the void with a fresh one behind it, and the audio goes
        back to history so "Retry last recording" can have another go at it.
        """
        self._attempt += 1
        audio, trimmed = self._audio, self._audio_trimmed
        self._audio, self._audio_trimmed = None, False
        if audio is not None:
            self.sv.history.keep_audio(audio, trimmed=trimmed)
        self._worker.abandon()

    def set_transcriber(self, t) -> None:
        self.sv.transcriber = t

    def set_injector(self, i) -> None:
        self.sv.injector = i

    # -- hotkey entry point ---------------------------------------------------
    def on_hotkey(self, name: str, kind: str) -> None:
        # Called on the evdev listener thread: nothing may escape from here, or the
        # listener dies and every hotkey stops working for the rest of the session.
        try:
            if name == "dictate":
                mode = self.sv.config_getter("hotkeys.dictate_mode", "hold")
                if mode == "toggle":
                    if kind == "press":
                        self.toggle()
                elif kind == "press":
                    self.start()
                else:
                    self.stop()
            elif name == "cancel" and kind == "press":
                self.cancel()
            elif name == "recall" and kind == "press":
                self.recall()
        except Exception as exc:
            log.exception("hotkey %s/%s failed", name, kind)
            with self._lock:
                if self._state == State.RECORDING:
                    # Failing part-way through stop()/cancel() must not leave
                    # pw-record running for the rest of the session.
                    self._cancel_recorder()
            self._fail(f"unexpected error: {exc}")

    # -- commands -------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._state != State.IDLE:
                return
            # Config is read and validated *before* the recorder starts: a bad value
            # here used to raise after pw-record was already running, killing the
            # listener thread and orphaning the process.
            try:
                device = self.sv.config_getter("audio.device", "") or None
                seconds = int(self.sv.config_getter("audio.max_seconds", 120))
                if seconds <= 0:
                    raise ValueError(f"audio.max_seconds must be a positive number, got {seconds!r}")
            except Exception as exc:
                self._fail(f"bad audio settings: {exc}")
                return
            if self._no_microphone():
                # Not a failed dictation - nothing was attempted. Said plainly
                # and left at idle, because the alternative is what the owner
                # hit: pw-record happily "recording" nothing at all.
                self._notify(NO_MICROPHONE, NO_MICROPHONE_BODY, "critical")
                return
            try:
                self.sv.recorder.start(device)
            except Exception as exc:
                self._fail(f"cannot record: {exc}")
                return
            try:
                self._timer = self._timer_factory(seconds, self._on_max_seconds)
                self._timer.start()
            except Exception as exc:
                self._cancel_recorder()
                self._fail(f"cannot start recording: {exc}")
                return
            self._set(State.RECORDING)

    def _no_microphone(self) -> bool:
        """True only when PipeWire is sure it has no capture device.

        An empty list and "could not ask" are different answers and only the
        first one may stop a recording: refusing because `pw-dump` is missing
        would break dictation on a machine that captures perfectly well.
        """
        try:
            sources = self.sv.sources()
        except Exception:
            log.exception("could not ask PipeWire for its capture sources")
            return False
        return sources is not None and not sources

    def _notify(self, title: str, body: str, urgency: str = "normal") -> None:
        """Notify without letting a broken notifier reach the caller."""
        try:
            self.sv.notify(title, body, urgency)
        except Exception:
            log.exception("notify failed: %s", title)

    def _cancel_recorder(self) -> None:
        try:
            self.sv.recorder.cancel()
        except Exception:
            log.exception("failed to cancel the recorder")

    def _on_max_seconds(self) -> None:
        log.info("max recording length reached")
        self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._state != State.RECORDING:
                return
            if self._timer:
                self._timer.cancel()
            pcm = self.sv.recorder.stop()
            if self.sv.recorder.error:
                self.sv.notify("Microphone problem", self.sv.recorder.error, "critical")
            self._set(State.TRANSCRIBING)
            attempt = self._next_attempt(pcm, trimmed=False)
        self._executor(lambda: self._process(pcm, attempt))

    def toggle(self) -> None:
        if self._state == State.IDLE:
            self.start()
        elif self._state == State.RECORDING:
            self.stop()

    def cancel(self) -> None:
        """Abandon whatever is in flight: the recording, or the conversion.

        Cancelling during TRANSCRIBING is the owner's way out of a conversion
        that is wedged, and unlike a cancelled recording it does not throw the
        audio away - the recording is finished and perfectly good, so it stays
        in history for "Retry last recording".
        """
        with self._lock:
            if self._state == State.RECORDING:
                if self._timer:
                    self._timer.cancel()
                self._cancel_recorder()
            elif self._state == State.TRANSCRIBING:
                self._drop_attempt()
            else:
                return
            self._set(State.IDLE, "cancelled")

    def recall(self) -> None:
        last = self.sv.history.last()
        if last is None:
            return
        with self._lock:
            if self._state != State.IDLE:
                return
            # Reserve INJECTING here, under the lock, so a start() racing this
            # call sees a busy state immediately instead of a stale IDLE - the
            # worker below must not set INJECTING again.
            self._set(State.INJECTING, "recall")
        self._executor(lambda: self._inject(last.text, last, already_injecting=True))

    def retry(self) -> None:
        with self._lock:
            if self._state != State.IDLE:
                return
            trimmed = self.sv.history.audio_trimmed
            pcm = self.sv.history.take_audio()
            if pcm is None:
                return
            self._set(State.TRANSCRIBING, "retry")
            attempt = self._next_attempt(pcm, trimmed=trimmed)
        self._executor(lambda: self._process(pcm, attempt, trimmed=trimmed))

    # -- worker ---------------------------------------------------------------
    def _process(self, pcm: np.ndarray, attempt: int, trimmed: bool = False) -> None:
        """Transcribe one recording, if anybody is still waiting for it.

        `attempt` is the number this job was given when the state went to
        TRANSCRIBING. It is checked on the way in - the job may have sat in the
        queue while a watchdog or a cancel gave up on it - and again before the
        transcript is used, because a local transcription cannot be stopped and
        may well arrive long after the pipeline stopped waiting for it.
        """
        claimed = False
        try:
            if not self._current(attempt):
                log.info("dropping a transcription nobody is waiting for")
                return
            if not trimmed:
                # Only the most recent failed recording stays retryable: otherwise
                # "Retry last recording" could re-paste one from hours ago.
                self.sv.history.clear_audio()
            audio = pcm if trimmed else self.sv.trim(pcm)
            if duration_s(audio) * 1000 < MIN_SPEECH_MS:
                self._set(State.IDLE, "too short")
                return
            language = self.sv.config_getter("general.language", "en")
            with self._lock:
                if not self._current(attempt):
                    log.info("dropping a transcription nobody is waiting for")
                    return
                # What an abandoned attempt leaves behind for the retry. The
                # untrimmed form has been there since stop()/retry(); this is
                # the upgrade to the trimmed one, not its first appearance.
                self._audio, self._audio_trimmed = audio, True
            try:
                result = self.sv.transcriber.transcribe(audio, language, self.sv.prompt_getter(),
                                                        hotwords=self.sv.hotwords_getter())
            except TranscriptionError as exc:
                if not self._claim(attempt):
                    log.info("a backend error from an abandoned attempt: %s", exc)
                    return
                self.sv.history.keep_audio(audio)
                self._fail(str(exc))
                return
            if not self._claim(attempt):
                log.info("discarding a transcript that arrived after its attempt "
                         "was abandoned")
                return
            claimed = True
            text = normalize_text(result.text)
            if not text:
                self._set(State.IDLE, "empty")
                return
            text = apply_replacements(text, self.sv.config_getter("dictionary.replacements", []) or [])
            entry = Entry(text, time.time(), result.backend, result.audio_s, result.elapsed_s)
            self.sv.history.add(entry)
            self._inject(text, entry)
        except Exception as exc:  # never let the worker die silently
            log.exception("pipeline failure")
            if not claimed and not self._claim(attempt):
                return                  # an abandoned attempt: it has been reported
            self._fail(f"unexpected error: {exc}")

    def _inject(self, text: str, entry: Entry, already_injecting: bool = False) -> None:
        if not already_injecting:
            self._set(State.INJECTING)
        try:
            try:
                res = self.sv.injector.inject(text)
            except Exception as exc:
                self._fail(f"could not paste: {exc}")
                return
            if res.method == "clipboard-only":
                self.sv.notify("Text copied", "Could not paste automatically. Paste with Ctrl+V.", "normal")
            elif res.method == "clipboard":
                # inject.mode = "clipboard": the user asked for this, so it reads
                # as a result rather than as the paste failure above.
                self.sv.notify("Copied", "Press Ctrl+V to paste.", "normal")
            elif res.method == "clipboard-pill" and not self._pill_notice_shown:
                # Nothing is broken: this desktop cannot give the pill a surface
                # that refuses focus, so pasting into it would be pasting into
                # the wrong window. Said once - see _pill_notice_shown.
                self._pill_notice_shown = True
                self.sv.notify("Text copied", PILL_FOCUS_NOTICE, "normal")
            self.last_error = None
            self._set(State.IDLE, idle_detail(len(text), res.method, entry.elapsed_s))
        except Exception as exc:  # never leave the daemon stuck in INJECTING - mirrors _process's net
            log.exception("inject failure")
            self._fail(f"unexpected error: {exc}")

    def _fail(self, message: str) -> None:
        self.last_error = message
        self._set(State.ERROR, message)
        try:
            self.sv.notify("Dictation failed", message, "critical")
        except Exception:
            # A broken notifier must not prevent recovery back to IDLE.
            log.exception("notify failed while reporting a dictation failure")
        self._set(State.IDLE, "after error")
