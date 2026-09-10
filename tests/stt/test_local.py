import numpy as np
import pytest

from voice.stt.base import Transcript, TranscriptionError
from voice.stt.local import LocalTranscriber, resolve_model_name


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeInfo:
    language = "en"


class FakeModel:
    calls = []

    def __init__(self, name, device, compute_type, **kw):
        FakeModel.calls.append((name, device, compute_type))

    def transcribe(self, audio, **kw):
        FakeModel.last_kwargs = kw
        assert audio.dtype == np.float32
        return iter([FakeSegment(" Hello"), FakeSegment(" world.")]), FakeInfo()


@pytest.fixture(autouse=True)
def _reset():
    FakeModel.calls = []


def test_resolve_model_name_aliases():
    assert resolve_model_name("large-v3-turbo") == "Systran/faster-whisper-large-v3-turbo"
    assert resolve_model_name("KBLab/kb-whisper-large") == "KBLab/kb-whisper-large"
    assert resolve_model_name("small") == "small"


def test_transcribe_joins_segments_and_reports_metadata():
    t = LocalTranscriber({"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16", "beam_size": 3},
                         model_factory=FakeModel, cuda_available=lambda: True)
    pcm = np.zeros(16000, dtype=np.int16)
    out = t.transcribe(pcm, language="en", prompt="CachyOS")
    assert isinstance(out, Transcript)
    assert out.text == "Hello world."
    assert out.language == "en" and out.audio_s == 1.0 and out.backend == "local"
    assert FakeModel.calls == [("Systran/faster-whisper-large-v3-turbo", "cuda", "float16")]
    assert FakeModel.last_kwargs["beam_size"] == 3
    assert FakeModel.last_kwargs["initial_prompt"] == "CachyOS"
    assert FakeModel.last_kwargs["language"] == "en"


def test_auto_language_passes_none():
    t = LocalTranscriber({"model": "small"}, model_factory=FakeModel, cuda_available=lambda: True)
    t.transcribe(np.zeros(1600, dtype=np.int16), language="auto", prompt="")
    assert FakeModel.last_kwargs["language"] is None
    assert FakeModel.last_kwargs["initial_prompt"] is None


def test_cpu_fallback_when_cuda_missing():
    t = LocalTranscriber({"model": "small", "device": "cuda", "compute_type": "float16"},
                         model_factory=FakeModel, cuda_available=lambda: False)
    t.warmup()
    assert FakeModel.calls == [("small", "cpu", "int8")]
    assert "CUDA" in t.fallback_reason
    assert "cpu" in t.describe()


def test_model_loaded_once_and_errors_wrapped():
    class Boom(FakeModel):
        def transcribe(self, audio, **kw):
            raise RuntimeError("cublas exploded")

    t = LocalTranscriber({"model": "small"}, model_factory=Boom, cuda_available=lambda: True)
    with pytest.raises(TranscriptionError, match="cublas"):
        t.transcribe(np.zeros(1600, dtype=np.int16), None, None)
    with pytest.raises(TranscriptionError):
        t.transcribe(np.zeros(1600, dtype=np.int16), None, None)
    assert len(FakeModel.calls) == 1


def test_model_load_failure_is_wrapped():
    class BoomFactory:
        def __init__(self, name, device, compute_type, **kw):
            raise RuntimeError("404")

    t = LocalTranscriber({"model": "small"}, model_factory=BoomFactory, cuda_available=lambda: True)
    with pytest.raises(TranscriptionError, match="404"):
        t.transcribe(np.zeros(1600, dtype=np.int16), None, None)


@pytest.mark.gpu
def test_real_cuda_transcribes_fixture_under_one_second():
    import time
    from pathlib import Path
    import wave
    fixture = Path(__file__).parent / "fixtures" / "hello.wav"   # owner records: "hello world, testing one two three"
    with wave.open(str(fixture)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    t = LocalTranscriber({"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16"})
    t.warmup()
    start = time.time()
    out = t.transcribe(pcm, "en", None)
    assert time.time() - start < 1.0
    assert "hello" in out.text.lower()
    assert t.fallback_reason is None
