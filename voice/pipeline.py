"""The dictation state machine: record -> trim -> transcribe -> replace -> inject -> history."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dictation")


def _thread_executor(fn: Callable[[], None]) -> None:
    _pool.submit(fn)


class Dictation:
    def __init__(self, services: Services, executor: Callable = _thread_executor, timer_factory=threading.Timer):
        self.sv = services
        self._executor = executor
        self._timer_factory = timer_factory
        self._timer = None
        self._lock = threading.RLock()
        self._state = State.IDLE
        self.on_state: Callable[[State, str], None] = lambda s, d: None
        self.last_error: str | None = None

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

    # -- commands -------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._state != State.IDLE:
                return
            device = self.sv.config_getter("audio.device", "") or None
            try:
                self.sv.recorder.start(device)
            except Exception as exc:
                self._fail(f"cannot record: {exc}")
                return
            seconds = int(self.sv.config_getter("audio.max_seconds", 120))
            self._timer = self._timer_factory(seconds, self._on_max_seconds)
            self._timer.start()
            self._set(State.RECORDING)

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
            self.sv.recorder.cancel()
            self._set(State.IDLE, "cancelled")

    def recall(self) -> None:
        last = self.sv.history.last()
        if last is None or self._state != State.IDLE:
            return
        self._executor(lambda: self._inject(last.text, last))

    def retry(self) -> None:
        pcm = self.sv.history.take_audio()
        if pcm is None or self._state != State.IDLE:
            return
        with self._lock:
            self._set(State.TRANSCRIBING, "retry")
        self._executor(lambda: self._process(pcm, trimmed=True))

    # -- worker ---------------------------------------------------------------
    def _process(self, pcm: np.ndarray, trimmed: bool = False) -> None:
        try:
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

    def _inject(self, text: str, entry: Entry) -> None:
        self._set(State.INJECTING)
        try:
            res = self.sv.injector.inject(text)
        except Exception as exc:
            self._fail(f"could not paste: {exc}")
            return
        if res.method == "clipboard-only":
            self.sv.notify("Text copied", "Could not paste automatically. Paste with Ctrl+V.", "normal")
        self._set(State.IDLE, f"{len(text)} chars via {res.method} in {entry.elapsed_s:.1f}s")

    def _fail(self, message: str) -> None:
        self.last_error = message
        self._set(State.ERROR, message)
        self.sv.notify("Dictation failed", message, "critical")
        self._set(State.IDLE, "after error")
