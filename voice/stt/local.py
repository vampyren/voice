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

_ALIASES = {
    "large-v3-turbo": "Systran/faster-whisper-large-v3-turbo",
    "turbo": "Systran/faster-whisper-large-v3-turbo",
}


def resolve_model_name(name: str) -> str:
    return _ALIASES.get(name, name)


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
        self._device = profile.get("device", "cuda")
        self._compute = profile.get("compute_type", "float16")
        self.fallback_reason: str | None = None

    def warmup(self) -> None:
        with self._lock:
            if self._model is None:
                self._model = self._load()

    def _load(self):
        device, compute = self._device, self._compute
        if device == "cuda" and not self._cuda():
            self.fallback_reason = "CUDA not available; using CPU int8 (slower)"
            log.warning(self.fallback_reason)
            device, compute = "cpu", "int8"
        self._device, self._compute = device, compute
        log.info("loading %s on %s/%s", resolve_model_name(self._profile["model"]), device, compute)
        return self._factory(resolve_model_name(self._profile["model"]), device, compute)

    def describe(self) -> str:
        return f"local {self._profile.get('model')} ({self._device}/{self._compute})"

    def transcribe(self, pcm: np.ndarray, language: str | None, prompt: str | None) -> Transcript:
        self.warmup()
        lang = None if language in (None, "", "auto") else language
        start = time.time()
        try:
            segments, info = self._model.transcribe(
                to_float32(pcm),
                language=lang,
                initial_prompt=prompt or None,
                beam_size=int(self._profile.get("beam_size", 5)),
                vad_filter=False,
                condition_on_previous_text=False,
            )
            text = "".join(s.text for s in segments).strip()
        except Exception as exc:
            raise TranscriptionError(f"local transcription failed: {exc}") from exc
        return Transcript(text, getattr(info, "language", lang), duration_s(pcm), time.time() - start, self.name)
