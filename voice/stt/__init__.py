"""Backend registry."""
from __future__ import annotations

from voice.stt.base import Transcriber, Transcript, TranscriptionError  # re-export


def make_transcriber(profile: dict, secret: str | None) -> Transcriber:
    backend = profile.get("backend")
    if backend == "local":
        from voice.stt.local import LocalTranscriber
        return LocalTranscriber(profile)
    if backend == "openai_compatible":
        from voice.stt.openai_compat import OpenAICompatTranscriber
        return OpenAICompatTranscriber(profile, secret)
    raise ValueError(f"unknown stt backend {backend!r}")
