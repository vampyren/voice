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


def loaded_with():
    """What each build was given, minus the offline flag.

    Every load tries the disk first, so `local_files_only` is on almost every
    call and says nothing about the case under test. The tests that are about
    it look at `FakeModel.kwargs` directly.
    """
    return [{k: v for k, v in kw.items() if k != "local_files_only"}
            for kw in FakeModel.kwargs]


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
    assert loaded_with() == [{"download_root": "/srv/models"}]


def test_no_model_dir_leaves_the_loader_on_its_own_default():
    """Passing download_root=None would be the same thing, but only by luck -
    say nothing and faster-whisper keeps using the Hugging Face cache."""
    t = LocalTranscriber({"model": "medium"},
                         model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert loaded_with() == [{}]


def test_a_blank_model_dir_is_not_a_directory():
    """An emptied setting means "the default", not a folder called ""."""
    t = LocalTranscriber({"model": "medium", "model_dir": "   "},
                         model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert loaded_with() == [{}]


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
    import os

    assert [c[1] for c in FakeModel.calls] == ["cuda", "cpu"]
    # The CPU build also gets its thread count; the card does not.
    assert loaded_with() == [{"download_root": "/srv/models"},
                             {"download_root": "/srv/models",
                              "cpu_threads": len(os.sched_getaffinity(0))}]


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


def test_the_thread_count_respects_an_affinity_mask(monkeypatch):
    """`os.cpu_count()` reports the machine, not what this process may use.

    Started under `taskset`, or inside a container with a CPU limit, asking for
    every core on the box means threads fighting over the few that are allowed.
    """
    monkeypatch.setattr("voice.stt.local.os.cpu_count", lambda: 32)
    monkeypatch.setattr("voice.stt.local.os.sched_getaffinity", lambda pid: set(range(8)))
    t = LocalTranscriber({"model": "medium", "device": "cpu", "compute_type": "int8"},
                         model_factory=FakeModel, cuda_available=lambda: False)
    t.warmup()
    assert loaded_with() == [{"cpu_threads": 8}]


def test_the_processor_gets_every_core_by_default():
    """faster-whisper uses four threads unless told otherwise - "Number of
    threads to use when running on CPU (4 by default)" - so a 16-core machine
    transcribed on a quarter of itself."""
    import os

    t = LocalTranscriber({"model": "medium", "device": "cpu", "compute_type": "int8"},
                         model_factory=FakeModel, cuda_available=lambda: False)
    t.warmup()
    assert loaded_with() == [{"cpu_threads": len(os.sched_getaffinity(0))}]


def test_a_thread_count_can_be_set_by_hand():
    t = LocalTranscriber({"model": "medium", "device": "cpu", "compute_type": "int8",
                          "cpu_threads": 3},
                         model_factory=FakeModel, cuda_available=lambda: False)
    t.warmup()
    assert loaded_with() == [{"cpu_threads": 3}]


def test_the_card_is_not_given_a_thread_count():
    """It is a CPU setting; passing it alongside a GPU load says nothing."""
    t = LocalTranscriber({"model": "medium", "device": "cuda", "compute_type": "float16"},
                         model_factory=FakeModel, cuda_available=lambda: True)
    t.warmup()
    assert loaded_with() == [{}]


# -- it runs locally, so it should not need the network -----------------------

def test_a_model_already_on_disk_is_loaded_without_asking_the_internet(monkeypatch, tmp_path):
    """Switching language logged an HTTP request to huggingface every time.

    Nothing was downloaded - it was the hub checking whether the cached copy
    was current - but it makes a local transcriber need the network to change
    language, and it reads like the audio is being sent somewhere.
    """
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    (tmp_path / "hub" / "models--Systran--faster-whisper-medium").mkdir(parents=True)
    t = LocalTranscriber({"model": "medium", "device": "cpu", "compute_type": "int8"},
                         model_factory=FakeModel, cuda_available=lambda: False)
    t.warmup()
    assert FakeModel.kwargs[0].get("local_files_only") is True


def test_a_model_that_is_not_there_yet_is_still_downloaded(monkeypatch, tmp_path):
    """Loading offline is for a model already here; the first use of a language
    has to be able to fetch it, and must not waste an attempt finding out."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    t = LocalTranscriber({"model": "medium", "device": "cpu", "compute_type": "int8"},
                         model_factory=FakeModel, cuda_available=lambda: False)
    t.warmup()
    assert len(FakeModel.kwargs) == 1, "it tried twice"
    assert "local_files_only" not in FakeModel.kwargs[0]
    assert t.fallback_reason is None, "reaching for the network is not a fallback"


def test_a_real_failure_is_still_reported():
    class Broken(FakeModel):
        def __init__(self, name, device, compute_type, **kw):
            super().__init__(name, device, compute_type, **kw)
            raise OSError("no such model anywhere")

    t = LocalTranscriber({"model": "nope", "device": "cpu", "compute_type": "int8"},
                         model_factory=Broken, cuda_available=lambda: False)
    with pytest.raises(TranscriptionError, match="no such model"):
        t.transcribe(np.zeros(1600, dtype=np.int16), None, None)


def test_asking_for_an_update_loads_with_the_network_once(monkeypatch, tmp_path):
    """The only thing that should ever reach out: the owner pressing a button.

    After that it is back to loading off the disk - an update check is a thing
    you ask for, not something that happens behind every language switch.
    """
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    (tmp_path / "hub" / "models--Systran--faster-whisper-medium").mkdir(parents=True)
    t = LocalTranscriber({"model": "medium", "device": "cpu", "compute_type": "int8"},
                         model_factory=FakeModel, cuda_available=lambda: False)
    t.warmup()
    assert FakeModel.kwargs[0].get("local_files_only") is True

    t.refresh()
    t.warmup()
    assert "local_files_only" not in FakeModel.kwargs[1], "it stayed offline"

    t._model = None                # the flag is not sticky
    t.warmup()
    assert FakeModel.kwargs[2].get("local_files_only") is True


def test_a_refresh_that_fails_leaves_the_old_model_alone(monkeypatch, tmp_path):
    """A check with no network must not cost the model that was working."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    (tmp_path / "hub" / "models--Systran--faster-whisper-medium").mkdir(parents=True)
    calls = []

    def factory(name, device, compute, **kw):
        calls.append(kw)
        if not kw.get("local_files_only"):
            raise OSError("no network")
        return FakeModel(name, device, compute, **kw)

    t = LocalTranscriber({"model": "medium", "device": "cpu", "compute_type": "int8"},
                         model_factory=factory, cuda_available=lambda: False)
    t.warmup()
    working = t._model
    t.refresh()
    with pytest.raises(Exception):
        t.warmup()
    t.warmup()                      # and the next one is offline again, and works
    assert t._model is not None and t._model is not working
