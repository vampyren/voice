import numpy as np

from voice.audio.level import rms_level


def _pcm(values) -> bytes:
    return np.asarray(values, dtype=np.int16).tobytes()


def test_empty_chunk_is_silent():
    assert rms_level(b"") == 0.0


def test_silence_is_zero():
    assert rms_level(_pcm([0] * 1024)) == 0.0


def test_full_scale_square_is_one():
    # +/-32767 alternating: RMS is (near) full scale, the loudest a chunk can be.
    chunk = _pcm([32767, -32767] * 512)
    assert rms_level(chunk) == 1.0


def test_half_scale_square_lands_in_the_documented_middle_range():
    # -6 dBFS is already loud; the soft knee puts it high but below the ceiling.
    level = rms_level(_pcm([16384, -16384] * 512))
    assert 0.6 < level < 0.95


def test_quiet_speech_like_level_lands_in_the_talking_band():
    # ~ -26 dBFS RMS, a normal speaking voice at a desk mic.
    chunk = _pcm([1638, -1638] * 512)
    assert 0.2 < rms_level(chunk) < 0.45


def test_louder_chunk_gives_a_higher_level():
    quiet = rms_level(_pcm([800, -800] * 512))
    loud = rms_level(_pcm([8000, -8000] * 512))
    assert loud > quiet


def test_result_is_always_within_unit_range():
    for amp in (0, 1, 100, 5000, 20000, 32767):
        assert 0.0 <= rms_level(_pcm([amp, -amp] * 64)) <= 1.0


def test_odd_trailing_byte_is_ignored():
    chunk = _pcm([16384, -16384] * 512)
    assert rms_level(chunk + b"\x7f") == rms_level(chunk)


def test_a_single_odd_byte_is_silence_not_a_crash():
    assert rms_level(b"\x7f") == 0.0
