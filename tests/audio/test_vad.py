import numpy as np
import pytest

from voice.audio.pcm import SAMPLE_RATE
from voice.audio.vad import trim_silence


def test_no_speech_returns_empty():
    pcm = np.zeros(SAMPLE_RATE, dtype=np.int16)
    out = trim_silence(pcm, timestamps_fn=lambda a: [])
    assert out.size == 0 and out.dtype == np.int16


def test_spans_are_padded_clipped_and_concatenated():
    pcm = np.arange(SAMPLE_RATE * 2, dtype=np.int16)      # 2 s ramp so we can check indices
    spans = [{"start": 1600, "end": 3200}, {"start": 30000, "end": 32000}]
    out = trim_silence(pcm, pad_ms=100, timestamps_fn=lambda a: spans)
    pad = SAMPLE_RATE // 10
    expected = np.concatenate([pcm[1600 - pad:3200 + pad], pcm[30000 - pad:SAMPLE_RATE * 2]])
    assert np.array_equal(out, expected)


def test_padding_does_not_go_negative():
    pcm = np.arange(8000, dtype=np.int16)
    out = trim_silence(pcm, pad_ms=500, timestamps_fn=lambda a: [{"start": 100, "end": 400}])
    assert np.array_equal(out, pcm[0:400 + 8000])


def test_timestamps_fn_receives_float32(monkeypatch):
    seen = {}

    def fn(a):
        seen["dtype"] = a.dtype
        return []

    trim_silence(np.zeros(1600, dtype=np.int16), timestamps_fn=fn)
    assert seen["dtype"] == np.float32


@pytest.mark.boundary
def test_real_silero_on_silence_is_empty():
    out = trim_silence(np.zeros(SAMPLE_RATE * 2, dtype=np.int16))
    assert out.size == 0
