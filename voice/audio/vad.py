"""Trim leading/trailing/inner silence using the Silero VAD bundled with faster-whisper."""
from __future__ import annotations

from typing import Callable

import numpy as np

from voice.audio.pcm import SAMPLE_RATE, to_float32

MIN_SPEECH_MS = 300
TimestampsFn = Callable[[np.ndarray], list[dict]]


def _silero(audio: np.ndarray) -> list[dict]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps  # heavy import, lazy

    return get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=0))


def trim_silence(pcm: np.ndarray, pad_ms: int = 200, timestamps_fn: TimestampsFn | None = None) -> np.ndarray:
    fn = timestamps_fn or _silero
    spans = fn(to_float32(pcm))
    if not spans:
        return np.zeros(0, dtype=np.int16)
    pad = int(SAMPLE_RATE * pad_ms / 1000)
    pieces = []
    for span in spans:
        start = max(0, int(span["start"]) - pad)
        end = min(pcm.size, int(span["end"]) + pad)
        pieces.append(pcm[start:end])
    return np.concatenate(pieces).astype(np.int16)
