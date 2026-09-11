"""Small PCM helpers: 16 kHz mono int16 is the app's only audio format."""
from __future__ import annotations

import io
import wave

import numpy as np

SAMPLE_RATE = 16000


def to_float32(pcm: np.ndarray) -> np.ndarray:
    return (pcm.astype(np.float32) / 32768.0).astype(np.float32)


def to_wav_bytes(pcm: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.ascontiguousarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


def from_bytes(raw: bytes) -> np.ndarray:
    usable = len(raw) - (len(raw) % 2)
    return np.frombuffer(raw[:usable], dtype=np.int16)


def duration_s(pcm: np.ndarray, rate: int = SAMPLE_RATE) -> float:
    return float(pcm.size) / rate
