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
    kwargs = []

    def __init__(self, name, device, compute_type, **kw):
        FakeModel.calls.append((name, device, compute_type))
        FakeModel.kwargs.append(kw)

    def transcribe(self, audio, **kw):
        FakeModel.last_kwargs = kw
        assert audio.dtype == np.float32
        return iter([FakeSegment(" Hello"), FakeSegment(" world.")]), FakeInfo()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeModel.calls = []
    FakeModel.kwargs = []
    # These cases are about what the transcriber does with a card it *can*
    # drive; the machine running the suite has no CUDA wheels, and without this
    # every one of them would take the "no runtime" path instead. The tests
    # that are about that path say so themselves.
    monkeypatch.setattr("voice.stt.local.bundled_cuda_runtime", lambda: True)


def test_a_configured_model_dir_is_where_the_model_is_downloaded():
    """Models are big, and ~/.cache is not where everyone keeps big things."""
    t = LocalTranscriber({"model": "medium", "model_dir": "/srv/models"},
                         model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert FakeModel.kwargs == [{"download_root": "/srv/models"}]


def test_no_model_dir_leaves_the_loader_on_its_own_default():
    """Passing download_root=None would be the same thing, but only by luck -
    say nothing and faster-whisper keeps using the Hugging Face cache."""
    t = LocalTranscriber({"model": "medium"},
                         model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert FakeModel.kwargs == [{}]


def test_a_blank_model_dir_is_not_a_directory():
    """An emptied setting means "the default", not a folder called ""."""
    t = LocalTranscriber({"model": "medium", "model_dir": "   "},
                         model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert FakeModel.kwargs == [{}]


def test_the_model_dir_survives_the_cpu_fallback():
    """The retry builds a second model; it must land in the same place."""
    class FailsOnGpu(FakeModel):
        def __init__(self, name, device, compute_type, **kw):
            super().__init__(name, device, compute_type, **kw)
            if device != "cpu":
                raise RuntimeError("no cublas here")

    t = LocalTranscriber({"model": "medium", "device": "cuda", "model_dir": "/srv/models"},
                         model_factory=FailsOnGpu, cuda_available=lambda: True)
    t.warmup()
    assert [c[1] for c in FakeModel.calls] == ["cuda", "cpu"]
    assert FakeModel.kwargs == [{"download_root": "/srv/models"}] * 2


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
    # Two builds, not one: the card failed while computing, so it was rebuilt on
    # the processor and tried once more. That failed too, and the instance stays
    # on the processor - so the second dictation builds nothing and just fails.
    assert [c[1] for c in FakeModel.calls] == ["cuda", "cpu"]


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


def test_a_failed_load_does_not_pin_the_instance_to_cpu():
    """`_load` reads `self._device` as its starting point.

    Recorded before the load succeeded, one failure sent every later attempt
    straight to CPU for the life of the process - so a GPU that came back
    healthy was never tried again, and nothing said why.
    """
    from voice.stt.local import LocalTranscriber

    broken = {"gpu": True}
    tried = []

    def factory(name, device, compute, **kw):
        tried.append(device)
        if device == "cuda" and broken["gpu"]:
            raise RuntimeError("libcublas.so.12 not found")
        return object()

    stt = LocalTranscriber({"model": "m", "device": "cuda", "compute_type": "float16"},
                           model_factory=factory, cuda_available=lambda: True)
    stt.warmup()
    assert tried == ["cuda", "cpu"]

    broken["gpu"], stt._model, tried[:] = False, None, []
    stt.warmup()
    assert tried == ["cuda"], "the GPU was never tried again after one failure"


def test_a_failure_that_is_not_the_gpu_is_reported_as_itself():
    """A download that failed is not a GPU problem.

    Blaming the card sent the owner a "Running on CPU" notification for a
    network error, and would have them debugging CUDA.
    """
    import pytest

    from voice.stt.base import TranscriptionError
    from voice.stt.local import LocalTranscriber

    def always_fails(name, device, compute, **kw):
        raise OSError("connection reset while downloading model")

    stt = LocalTranscriber({"model": "m", "device": "cuda", "compute_type": "float16"},
                           model_factory=always_fails, cuda_available=lambda: True)
    with pytest.raises(Exception) as caught:
        stt.warmup()
    assert "connection reset" in str(caught.value)
    assert stt.fallback_reason is None, \
        f"a download failure was blamed on the GPU: {stt.fallback_reason}"


def test_device_auto_gets_the_same_net_as_cuda():
    """`auto` is faster-whisper's own default and picks the GPU when there is one.

    Keyed on the exact string "cuda", both guards missed it and it reached the
    loader with no fallback behind it.
    """
    from voice.stt.local import LocalTranscriber

    tried = []

    def factory(name, device, compute, **kw):
        tried.append(device)
        if device == "auto":
            raise RuntimeError("libcublas.so.12 not found")
        return object()

    stt = LocalTranscriber({"model": "m", "device": "auto", "compute_type": "float16"},
                           model_factory=factory, cuda_available=lambda: True)
    stt.warmup()
    assert tried == ["auto", "cpu"], tried
    assert "GPU could not be used" in stt.fallback_reason


def test_auto_with_no_card_goes_straight_to_cpu():
    from voice.stt.local import LocalTranscriber

    tried = []
    stt = LocalTranscriber({"model": "m", "device": "auto", "compute_type": "float16"},
                           model_factory=lambda n, d, c, **kw: (tried.append(d), object())[1],
                           cuda_available=lambda: False)
    stt.warmup()
    assert tried == ["cpu"]


# -- a card the libraries cannot drive ----------------------------------------

def test_a_card_with_no_runtime_is_not_even_tried(monkeypatch):
    """The CPU-only package on a machine with an NVIDIA card.

    The model *constructs* on cuda perfectly well - cuBLAS is only needed when
    it first computes - so the 0.1.4 fallback, which watched the construction,
    never fired. The first dictation then died with "Library libcublas.so.12 is
    not found or cannot be loaded" and no fallback at all.
    """
    monkeypatch.setattr("voice.stt.local.bundled_cuda_runtime", lambda: False)
    t = LocalTranscriber({"model": "medium", "device": "cuda", "compute_type": "float16"},
                         model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert FakeModel.calls == [("medium", "cpu", "int8")], FakeModel.calls
    assert "cpu" in t.describe()
    assert t.fallback_reason and "runtime" in t.fallback_reason.lower()


def test_a_card_with_its_runtime_is_used(monkeypatch):
    monkeypatch.setattr("voice.stt.local.bundled_cuda_runtime", lambda: True)
    t = LocalTranscriber({"model": "medium", "device": "cuda", "compute_type": "float16"},
                         model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert FakeModel.calls == [("medium", "cuda", "float16")]
    assert t.fallback_reason is None


def test_a_gpu_that_fails_mid_transcription_is_retried_on_the_processor(monkeypatch):
    """The safety net behind the check above: whatever the reason, a dictation
    must not be lost to a card that cannot compute."""
    monkeypatch.setattr("voice.stt.local.bundled_cuda_runtime", lambda: True)

    class CublasOnGpu(FakeModel):
        def transcribe(self, audio, **kw):
            if FakeModel.calls[-1][1] != "cpu":
                raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
            return super().transcribe(audio, **kw)

    t = LocalTranscriber({"model": "medium", "device": "cuda", "compute_type": "float16"},
                         model_factory=CublasOnGpu, cuda_available=lambda: True)
    out = t.transcribe(np.zeros(16000, dtype=np.int16), language="en", prompt=None)
    assert out.text == "Hello world."
    assert [c[1] for c in FakeModel.calls] == ["cuda", "cpu"]
    assert t.fallback_reason and "cpu" in t.fallback_reason.lower()
    assert "cpu" in t.describe()


def test_a_processor_failure_is_not_retried_for_ever(monkeypatch):
    """Only a GPU failure earns a second go; a broken model must still fail."""
    monkeypatch.setattr("voice.stt.local.bundled_cuda_runtime", lambda: False)

    class AlwaysBroken(FakeModel):
        def transcribe(self, audio, **kw):
            raise RuntimeError("nothing works")

    t = LocalTranscriber({"model": "medium", "device": "cpu", "compute_type": "int8"},
                         model_factory=AlwaysBroken, cuda_available=lambda: False)
    with pytest.raises(TranscriptionError):
        t.transcribe(np.zeros(16000, dtype=np.int16), language="en", prompt=None)
    assert len(FakeModel.calls) == 1, FakeModel.calls
