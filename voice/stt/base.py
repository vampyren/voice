"""Transcriber protocol shared by local and cloud backends."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    audio_s: float
    elapsed_s: float
    backend: str


class TranscriptionError(RuntimeError):
    """Raised for any backend failure; message is user-presentable."""


class Transcriber(Protocol):
    name: str

    def transcribe(self, pcm: np.ndarray, language: str | None, prompt: str | None) -> Transcript: ...
    def warmup(self) -> None: ...
    def describe(self) -> str: ...
