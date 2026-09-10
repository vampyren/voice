import json

import httpx
import numpy as np
import pytest

from voice.stt.base import TranscriptionError
from voice.stt.openai_compat import OpenAICompatTranscriber, build_request

OPENAI = {"backend": "openai_compatible", "base_url": "https://api.openai.com/v1", "model": "gpt-transcribe", "prompt": "CachyOS"}
OPENROUTER = {"backend": "openai_compatible", "base_url": "https://openrouter.ai/api/v1/", "model": "openai/whisper-large-v3-turbo", "prompt": "CachyOS"}


def test_build_request_openai_sends_prompt_and_language():
    url, fields = build_request(OPENAI, "en", "CachyOS")
    assert url == "https://api.openai.com/v1/audio/transcriptions"
    assert fields == {"model": "gpt-transcribe", "response_format": "json", "language": "en", "prompt": "CachyOS"}


def test_build_request_openrouter_drops_prompt_and_handles_trailing_slash():
    url, fields = build_request(OPENROUTER, "auto", "CachyOS")
    assert url == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert "prompt" not in fields and "language" not in fields


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_transcribe_posts_wav_and_parses_text():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["ct"] = request.headers["content-type"]
        body = request.read()
        seen["has_wav"] = b"RIFF" in body and b'filename="audio.wav"' in body
        seen["has_model"] = b"gpt-transcribe" in body
        return httpx.Response(200, json={"text": " Hello there. "})

    t = OpenAICompatTranscriber(OPENAI, "sk-test", client=_client(handler))
    out = t.transcribe(np.zeros(16000, dtype=np.int16), "en", "CachyOS")
    assert out.text == "Hello there."
    assert out.backend == "openai_compatible" and out.audio_s == 1.0
    assert seen["auth"] == "Bearer sk-test"
    assert seen["ct"].startswith("multipart/form-data")
    assert seen["has_wav"] and seen["has_model"]


def test_missing_key_fails_before_request():
    t = OpenAICompatTranscriber(OPENAI, None, client=_client(lambda r: pytest.fail("no request expected")))
    with pytest.raises(TranscriptionError, match="API key"):
        t.transcribe(np.zeros(1600, dtype=np.int16), "en", None)


def test_http_error_surfaces_provider_message():
    handler = lambda r: httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}})
    t = OpenAICompatTranscriber(OPENAI, "bad", client=_client(handler))
    with pytest.raises(TranscriptionError, match="401.*Incorrect API key"):
        t.transcribe(np.zeros(1600, dtype=np.int16), "en", None)


def test_network_error_is_wrapped():
    def handler(r):
        raise httpx.ConnectError("boom")
    t = OpenAICompatTranscriber(OPENAI, "k", client=_client(handler))
    with pytest.raises(TranscriptionError, match="boom"):
        t.transcribe(np.zeros(1600, dtype=np.int16), "en", None)


def test_describe_names_model_and_host():
    t = OpenAICompatTranscriber(OPENROUTER, "k")
    assert "openrouter.ai" in t.describe() and "whisper-large-v3-turbo" in t.describe()
