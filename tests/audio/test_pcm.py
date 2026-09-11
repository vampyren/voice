import io
import wave

import numpy as np

from voice.audio.pcm import SAMPLE_RATE, duration_s, from_bytes, to_float32, to_wav_bytes


def test_to_float32_scales_int16():
    pcm = np.array([0, 16384, -32768, 32767], dtype=np.int16)
    f = to_float32(pcm)
    assert f.dtype == np.float32
    assert np.allclose(f, [0.0, 0.5, -1.0, 32767 / 32768])


def test_wav_bytes_is_valid_mono_16k_wav():
    pcm = (np.sin(np.linspace(0, 100, SAMPLE_RATE)) * 10000).astype(np.int16)
    data = to_wav_bytes(pcm)
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == SAMPLE_RATE
        assert w.getnframes() == SAMPLE_RATE


def test_from_bytes_and_duration():
    pcm = np.arange(8000, dtype=np.int16)
    assert np.array_equal(from_bytes(pcm.tobytes()), pcm)
    assert duration_s(pcm) == 0.5
    assert from_bytes(b"\x01").size == 0      # odd trailing byte dropped, no crash
