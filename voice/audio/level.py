"""Microphone loudness for the recording overlay: one number per captured chunk.

This is display-only - nothing in the transcription path reads it - so it
optimises for *looking* right rather than for metering accuracy.
"""
from __future__ import annotations

import numpy as np

from voice.audio.pcm import from_bytes

#: Soft-knee gain applied after the square root. A square root alone leaves
#: speech hugging the bottom of the scale (a normal voice sits near -26 dBFS,
#: i.e. 0.05 full scale, which maps to 0.22); the gain lifts the usual speaking
#: range of 0.06..0.4 full-scale RMS onto 0.31..0.79 of the bar height, so the
#: waveform moves visibly without pinning to the ceiling. It clips to 1.0 at
#: 0.64 full-scale RMS (-3.9 dBFS), which only a very loud chunk reaches.
GAIN = 1.25

FULL_SCALE = 32768.0


def rms_level(chunk: bytes) -> float:
    """RMS of a raw int16 mono chunk, normalised to 0..1 with a soft knee.

    An odd trailing byte (a chunk boundary splitting a sample) is ignored, and
    an empty or single-byte chunk is silence rather than an error.
    """
    samples = from_bytes(chunk)
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    return min(1.0, (rms / FULL_SCALE) ** 0.5 * GAIN)
