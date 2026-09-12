"""faster-whisper backend (CTranslate2), CUDA with CPU int8 fallback."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import numpy as np

from voice.audio.pcm import duration_s, to_float32
from voice.stt.base import Transcript, TranscriptionError

log = logging.getLogger(__name__)


def _is_cpu(device: str) -> bool:
    """Is this device definitely not a GPU?

    Everything else is treated as one, `"auto"` included - that is
    faster-whisper's own default, it selects the GPU whenever a card is
    counted, and keying on the exact string `"cuda"` let it reach the loader
    with no fallback behind it at all.
    """
    return str(device).strip().lower() in ("cpu", "")


def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception as exc:  # missing libs count as unavailable
        log.info("cuda probe failed: %s", exc)
        return False


def _default_factory(name: str, device: str, compute_type: str, **kw):
    from faster_whisper import WhisperModel  # lazy: slow import
    return WhisperModel(name, device=device, compute_type=compute_type, **kw)


class LocalTranscriber:
    name = "local"

    def __init__(self, profile: dict, model_factory: Callable | None = None,
                 cuda_available: Callable[[], bool] | None = None):
        self._profile = profile
        self._factory = model_factory or _default_factory
        self._cuda = cuda_available or _cuda_available
        self._model = None
        self._lock = threading.Lock()
        #: What the owner asked for, which never changes, and what is actually
        #: in use, which `describe()` reports. Kept apart: a fallback used to
        #: write back over the request, so one failed load pinned every later
        #: attempt to CPU even once the GPU was healthy again.
        self._wanted = profile.get("device", "cuda")
        self._wanted_compute = profile.get("compute_type", "float16")
        self._device, self._compute = self._wanted, self._wanted_compute
        self.fallback_reason: str | None = None

    def warmup(self) -> None:
        with self._lock:
            if self._model is None:
                self._model = self._load()

    def _load(self):
        device, compute = self._wanted, self._wanted_compute
        if not _is_cpu(device) and not self._cuda():
            self.fallback_reason = "CUDA not available; using CPU int8 (slower)"
            log.warning(self.fallback_reason)
            return self._build("cpu", "int8")
        if _is_cpu(device):
            return self._build(device, compute)

        try:
            return self._build(device, compute)
        except Exception as exc:
            # A card is present and the libraries to drive it are not - exactly
            # the CPU-only build on a machine with an NVIDIA driver:
            # `get_cuda_device_count()` counts the card, then CTranslate2
            # cannot dlopen libcublas 12 because this package deliberately does
            # not ship it. Counting devices can never see that; only loading
            # can. Failing here would cost a dictation and a model download.
            try:
                model = self._build("cpu", "int8")
            except Exception:
                # CPU failed the same way, so the GPU was never the problem -
                # a bad model name, no network, a full disk. Blaming the card
                # would send the owner off debugging CUDA for a download.
                raise exc from None
            self.fallback_reason = (f"the GPU could not be used ({exc}); "
                                    f"using CPU int8 (slower)")
            log.warning(self.fallback_reason)
            return model

    def _build(self, device: str, compute: str):
        # The configured name goes through untouched: faster-whisper resolves its
        # own short names ("large-v3-turbo", "small"), and anything else is a
        # Hugging Face repository id, which is how KBLab/kb-whisper-* works. We
        # once rewrote "large-v3-turbo" ourselves, to a repository that does not
        # exist - the shipped default could not load a model at all.
        name = self._profile["model"]
        log.info("loading %s on %s/%s", name, device, compute)
        model = self._factory(name, device, compute)
        # Only once it has actually loaded. Recorded before, a failed attempt
        # pinned the instance to whatever it fell back to for the life of the
        # process: `_load` reads `self._device` as its starting point, so the
        # next attempt never tried the GPU again even once it was healthy.
        self._device, self._compute = device, compute
        return model

    def describe(self) -> str:
        return f"local {self._profile.get('model')} ({self._device}/{self._compute})"

    def transcribe(self, pcm: np.ndarray, language: str | None, prompt: str | None,
                   hotwords: str | None = None) -> Transcript:
        """`prompt` steers the style; `hotwords` is the user's vocabulary.

        Measured on this project's corpus, biasing the decoder with the word list
        took domain-term recall from 3 of 8 to 7 of 8 and left all 207 LibriSpeech
        words untouched, while the same vocabulary written as a prose
        initial_prompt reached the same recall and doubled the errors on ordinary
        English. They are separate settings for that reason.
        """
        try:
            self.warmup()
        except Exception as exc:
            raise TranscriptionError(f"model load failed: {exc}") from exc
        lang = None if language in (None, "", "auto") else language
        start = time.time()
        try:
            segments, info = self._model.transcribe(
                to_float32(pcm),
                language=lang,
                initial_prompt=prompt or None,
                beam_size=int(self._profile.get("beam_size", 5)),
                # Only when there is something to bias towards: any value builds a
                # <|startofprev|> context the decoder would otherwise not have.
                **({"hotwords": hotwords.strip()} if hotwords and hotwords.strip() else {}),
                vad_filter=False,
                condition_on_previous_text=False,
            )
            text = "".join(s.text for s in segments).strip()
        except Exception as exc:
            raise TranscriptionError(f"local transcription failed: {exc}") from exc
        return Transcript(text, getattr(info, "language", lang), duration_s(pcm), time.time() - start, self.name)
