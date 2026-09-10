"""The dictation state machine: record -> trim -> transcribe -> replace -> inject -> history."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import numpy as np

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
    trim: Callable[[np.ndarray], np.ndarray] = trim_silence


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
        # Owned by this pipeline rather than the module, so quitting can end it.
        self._worker = Worker()
        self._executor = executor or self._worker.submit

    def shutdown(self) -> None:
        self._worker.shutdown()

    # -- state ----------------------------------------------------------------
    @property
    def state(self) -> State:
        return self._state

    def _set(self, state: State, detail: str = "") -> None:
        self._state = state
        log.debug("state %s %s", state.value, detail)
        try:
            self.on_state(state, detail)
        except Exception:
            log.exception("on_state callback failed")

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
        self._executor(lambda: self._process(pcm))

    def toggle(self) -> None:
        if self._state == State.IDLE:
            self.start()
        elif self._state == State.RECORDING:
            self.stop()

    def cancel(self) -> None:
        with self._lock:
            if self._state != State.RECORDING:
                return
            if self._timer:
                self._timer.cancel()
            self._cancel_recorder()
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
            pcm = self.sv.history.take_audio()
            if pcm is None:
                return
            self._set(State.TRANSCRIBING, "retry")
        self._executor(lambda: self._process(pcm, trimmed=True))

    # -- worker ---------------------------------------------------------------
    def _process(self, pcm: np.ndarray, trimmed: bool = False) -> None:
        try:
            if not trimmed:
                # Only the most recent failed recording stays retryable: otherwise
                # "Retry last recording" could re-paste one from hours ago.
                self.sv.history.clear_audio()
            audio = pcm if trimmed else self.sv.trim(pcm)
            if duration_s(audio) * 1000 < MIN_SPEECH_MS:
                self._set(State.IDLE, "too short")
                return
            language = self.sv.config_getter("general.language", "en")
            try:
                result = self.sv.transcriber.transcribe(audio, language, self.sv.prompt_getter())
            except TranscriptionError as exc:
                self.sv.history.keep_audio(audio)
                self._fail(str(exc))
                return
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
            self._set(State.IDLE, f"{len(text)} chars via {res.method} in {entry.elapsed_s:.1f}s")
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
