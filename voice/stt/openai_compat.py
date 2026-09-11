"""POST /audio/transcriptions backend: OpenAI, Groq, Mistral, Together, OpenRouter, any."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
import numpy as np

from voice.audio.pcm import duration_s, to_wav_bytes
from voice.stt.base import Transcript, TranscriptionError

TIMEOUT_S = 30.0
_NO_PROMPT_HOSTS = ("openrouter.ai",)


def build_request(profile: dict, language: str | None, prompt: str | None) -> tuple[str, dict]:
    base = str(profile["base_url"]).rstrip("/")
    host = urlparse(base).hostname or ""
    fields = {"model": profile["model"], "response_format": "json"}
    if language and language != "auto":
        fields["language"] = language
    if prompt and not any(h in host for h in _NO_PROMPT_HOSTS):
        fields["prompt"] = prompt
    return f"{base}/audio/transcriptions", fields


class OpenAICompatTranscriber:
    name = "openai_compatible"

    def __init__(self, profile: dict, secret: str | None, client: httpx.Client | None = None):
        self._profile = profile
        self._secret = secret
        self._client = client or httpx.Client(timeout=TIMEOUT_S)

    def warmup(self) -> None:
        return None

    def describe(self) -> str:
        return f"{self._profile.get('model')} @ {urlparse(str(self._profile.get('base_url'))).hostname}"

    def transcribe(self, pcm: np.ndarray, language: str | None, prompt: str | None,
                   hotwords: str | None = None) -> Transcript:
        """`hotwords` is a faster-whisper decoder setting with no equivalent in the
        OpenAI transcription API, so it is accepted and ignored here: the local
        backend is the one that can be told what to listen for."""
        if not self._secret:
            raise TranscriptionError(
                f"no API key for {self.describe()}: set api_key or api_key_env in the profile")
        url, fields = build_request(self._profile, language, prompt or self._profile.get("prompt") or None)
        files = {"file": ("audio.wav", to_wav_bytes(pcm), "audio/wav")}
        start = time.time()
        try:
            resp = self._client.post(url, data=fields, files=files,
                                     headers={"Authorization": f"Bearer {self._secret}"})
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"request to {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = resp.text[:200]
            raise TranscriptionError(f"{self.describe()} returned {resp.status_code}: {detail}")
        try:
            text = str(resp.json().get("text", "")).strip()
        except ValueError as exc:
            raise TranscriptionError(f"{self.describe()} returned non-JSON") from exc
        lang = None if language in (None, "", "auto") else language
        return Transcript(text, lang, duration_s(pcm), time.time() - start, self.name)
