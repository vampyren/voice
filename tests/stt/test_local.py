import numpy as np
import pytest

from voice.stt.base import Transcript, TranscriptionError
from voice.stt.local import LocalTranscriber


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


#: Every model name this project ships: the profiles in DEFAULT_CONFIG and the
#: Swedish models the README tells people to paste in.
SHIPPED_MODEL_NAMES = ["large-v3-turbo", "medium", "small",
                       "KBLab/kb-whisper-large", "KBLab/kb-whisper-medium"]


@pytest.mark.parametrize("name", SHIPPED_MODEL_NAMES)
def test_the_configured_model_name_reaches_the_loader_unchanged(name):
    """We used to rewrite "large-v3-turbo" to a Hugging Face repository that does
    not exist, so a fresh install could not load a model at all. faster-whisper
    resolves its own short names; ours must be passed through untouched."""
    t = LocalTranscriber({"model": name}, model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert [call[0] for call in FakeModel.calls] == [name]


def test_transcribe_joins_segments_and_reports_metadata():
    t = LocalTranscriber({"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16", "beam_size": 3},
                         model_factory=FakeModel, cuda_available=lambda: True)
    pcm = np.zeros(16000, dtype=np.int16)
    out = t.transcribe(pcm, language="en", prompt="CachyOS")
    assert isinstance(out, Transcript)
    assert out.text == "Hello world."
    assert out.language == "en" and out.audio_s == 1.0 and out.backend == "local"
    assert FakeModel.calls == [("large-v3-turbo", "cuda", "float16")]
    assert FakeModel.last_kwargs["beam_size"] == 3
    assert FakeModel.last_kwargs["initial_prompt"] == "CachyOS"
    assert FakeModel.last_kwargs["language"] == "en"


def test_the_vocabulary_is_sent_as_hotwords_beside_the_style_prompt():
    """Two separate things: hotwords bias the decoder towards the user's words,
    initial_prompt steers the style. Measured, a prose prompt doing the
    vocabulary's job doubled the errors on ordinary English."""
    t = LocalTranscriber({"model": "small"}, model_factory=FakeModel, cuda_available=lambda: True)
    t.transcribe(np.zeros(1600, dtype=np.int16), "en", "Dictating notes.",
                 hotwords="CachyOS, OBSBOT")
    assert FakeModel.last_kwargs["hotwords"] == "CachyOS, OBSBOT"
    assert FakeModel.last_kwargs["initial_prompt"] == "Dictating notes."


@pytest.mark.parametrize("hotwords", [None, "", "   "])
def test_an_empty_vocabulary_sends_no_hotwords_at_all(hotwords):
    """faster-whisper builds a <|startofprev|> context whenever hotwords is set;
    an empty list must leave the decoder exactly as it was."""
    t = LocalTranscriber({"model": "small"}, model_factory=FakeModel, cuda_available=lambda: True)
    t.transcribe(np.zeros(1600, dtype=np.int16), "en", None, hotwords=hotwords)
    assert "hotwords" not in FakeModel.last_kwargs


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


@pytest.mark.boundary
def test_the_shipped_default_model_name_really_loads():
    """The name in DEFAULT_CONFIG must name a model faster-whisper can fetch.

    The alias this replaced pointed at a repository that does not exist, and no
    test with a fake factory could have seen that: only a real load can. Uses the
    Hugging Face cache when the weights are already there and downloads them
    otherwise; only a hub it cannot reach at all is a skip, because a name that
    does not resolve is exactly the failure this test exists for - and Hugging
    Face reports that one as an OSError too.
    """
    import tomlkit
    from huggingface_hub.errors import LocalEntryNotFoundError

    from voice.config import DEFAULT_CONFIG
    from voice.stt.local import _default_factory

    name = str(tomlkit.parse(DEFAULT_CONFIG)["stt"]["profiles"]["local"]["model"])
    try:
        model = _default_factory(name, "cpu", "int8", local_files_only=True)
    except Exception:                      # not in the cache: fetch it for real
        try:
            model = _default_factory(name, "cpu", "int8")
        except LocalEntryNotFoundError as exc:
            pytest.skip(f"{name} is not cached and the hub is unreachable: {exc}")
    assert model.model.is_multilingual is True


# -- a card that is present but unusable -------------------------------------
#: The CPU-only package on a machine with an NVIDIA driver: CTranslate2 counts
#: the card, then cannot dlopen libcublas 12 because the build deliberately
#: does not ship it. Counting devices cannot see that; only loading can. Before
#: this, the owner's first dictation failed after a 1.6 GB model download.

def test_a_gpu_that_cannot_actually_load_falls_back_to_cpu():
    tried = []

    def factory(name, device, compute, **kw):
        tried.append((device, compute))
        if device == "cuda":
            raise RuntimeError("Library libcublas.so.12 is not found")
        return object()

    from voice.stt.local import LocalTranscriber

    stt = LocalTranscriber({"model": "large-v3-turbo", "device": "cuda",
                            "compute_type": "float16"},
                           model_factory=factory, cuda_available=lambda: True)
    stt.warmup()

    assert tried == [("cuda", "float16"), ("cpu", "int8")], tried
    assert "GPU could not be used" in stt.fallback_reason
    assert stt.describe().endswith("(cpu/int8)")


def test_a_gpu_that_works_is_not_second_guessed():
    from voice.stt.local import LocalTranscriber

    tried = []

    def factory(name, device, compute, **kw):
        tried.append(device)
        return object()

    stt = LocalTranscriber({"model": "large-v3-turbo", "device": "cuda",
                            "compute_type": "float16"},
                           model_factory=factory, cuda_available=lambda: True)
    stt.warmup()

    assert tried == ["cuda"], "a working GPU must not be retried on CPU"
    assert stt.fallback_reason is None


def test_a_cpu_profile_never_tries_the_gpu():
    from voice.stt.local import LocalTranscriber

    tried = []
    stt = LocalTranscriber({"model": "small", "device": "cpu", "compute_type": "int8"},
                           model_factory=lambda n, d, c, **kw: tried.append(d),
                           cuda_available=lambda: True)
    stt.warmup()
    assert tried == ["cpu"]
