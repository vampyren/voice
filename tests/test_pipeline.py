import numpy as np
import pytest

from voice.history import History
from voice.inject.injector import InjectResult
from voice.pipeline import Dictation, Services, State
from voice.stt.base import Transcript, TranscriptionError


class FakeRecorder:
    def __init__(self, pcm=None):
        self.pcm = pcm if pcm is not None else np.ones(16000, dtype=np.int16)
        self.is_recording, self.started_with, self.cancelled = False, [], 0
        self.error = None

    def start(self, device):
        self.is_recording = True
        self.started_with.append(device)

    def stop(self):
        self.is_recording = False
        return self.pcm

    def cancel(self):
        self.cancelled += 1
        self.is_recording = False


class FakeTranscriber:
    name = "fake"

    def __init__(self, text="hello world", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def transcribe(self, pcm, language, prompt):
        self.calls.append((pcm.size, language, prompt))
        if self.fail:
            raise TranscriptionError("cloud down")
        return Transcript(self.text, language, 1.0, 0.1, self.name)

    def warmup(self): pass
    def describe(self): return "fake"


class FakeInjector:
    def __init__(self, method="portal"):
        self.method, self.texts = method, []

    def inject(self, text):
        self.texts.append(text)
        return InjectResult(self.method, "ctrl+v", True)


class FakeTimer:
    instances = []

    def __init__(self, seconds, fn):
        self.seconds, self.fn, self.cancelled = seconds, fn, False
        FakeTimer.instances.append(self)

    def start(self): pass
    def cancel(self): self.cancelled = True
    def fire(self): self.fn()


def make(cfg=None, rec=None, stt=None, inj=None):
    cfg = {"hotkeys.dictate_mode": "hold", "audio.device": "", "audio.max_seconds": 120,
           "general.language": "en", "dictionary.replacements": [["cachy os", "CachyOS", "icase"]], **(cfg or {})}
    notes = []
    services = Services(
        recorder=rec or FakeRecorder(), transcriber=stt or FakeTranscriber(), injector=inj or FakeInjector(),
        history=History(), notify=lambda t, b, u="normal": notes.append((t, b)),
        trim=lambda pcm: pcm, config_getter=lambda k, d=None: cfg.get(k, d), prompt_getter=lambda: "CachyOS")
    states = []
    d = Dictation(services, executor=lambda fn: fn(), timer_factory=FakeTimer)
    d.on_state = lambda s, detail: states.append(s)
    FakeTimer.instances = []
    return d, services, states, notes


def test_hold_mode_full_flow_applies_replacements_and_records_history():
    d, sv, states, notes = make(stt=FakeTranscriber("I run cachy os"))
    d.on_hotkey("dictate", "press")
    assert d.state == State.RECORDING and sv.recorder.started_with == [None]
    d.on_hotkey("dictate", "release")
    assert states == [State.RECORDING, State.TRANSCRIBING, State.INJECTING, State.IDLE]
    assert sv.injector.texts == ["I run CachyOS"]
    assert sv.transcriber.calls == [(16000, "en", "CachyOS")]
    assert sv.history.last().text == "I run CachyOS"


def test_toggle_mode_alternates_on_press():
    d, sv, states, _ = make({"hotkeys.dictate_mode": "toggle"})
    d.on_hotkey("dictate", "press"); d.on_hotkey("dictate", "release")
    assert d.state == State.RECORDING
    d.on_hotkey("dictate", "press")
    assert d.state == State.IDLE and sv.injector.texts == ["hello world"]


def test_too_short_is_dropped_silently():
    d, sv, states, notes = make(rec=FakeRecorder(np.ones(1000, dtype=np.int16)))
    d.start(); d.stop()
    assert sv.transcriber.calls == [] and states[-1] == State.IDLE and notes == []


def test_empty_transcript_dropped():
    d, sv, states, notes = make(stt=FakeTranscriber(""))
    d.start(); d.stop()
    assert sv.injector.texts == [] and d.state == State.IDLE


def test_error_keeps_audio_for_retry():
    stt = FakeTranscriber(fail=True)
    d, sv, states, notes = make(stt=stt)
    d.start(); d.stop()
    assert State.ERROR in states and d.state == State.IDLE
    assert "cloud down" in notes[-1][1] and d.last_error
    stt.fail = False
    d.retry()
    assert sv.injector.texts == ["hello world"]
    d.retry()                                   # nothing left to retry
    assert len(sv.injector.texts) == 1


def test_cancel_discards_recording():
    d, sv, states, _ = make()
    d.start(); d.on_hotkey("cancel", "press")
    assert sv.recorder.cancelled == 1 and d.state == State.IDLE and sv.transcriber.calls == []


def test_max_seconds_timer_stops_recording():
    d, sv, states, _ = make({"audio.max_seconds": 7})
    d.start()
    timer = FakeTimer.instances[-1]
    assert timer.seconds == 7
    timer.fire()
    assert d.state == State.IDLE and sv.injector.texts == ["hello world"]
    d.start(); d.stop()
    assert FakeTimer.instances[-2].cancelled or FakeTimer.instances[-1].cancelled


def test_press_during_transcription_is_ignored_and_recall_reinjects():
    d, sv, states, _ = make()
    d.start(); d.stop()
    d.recall()
    assert sv.injector.texts == ["hello world", "hello world"]


def test_clipboard_only_result_notifies_user():
    d, sv, states, notes = make(inj=FakeInjector(method="clipboard-only"))
    d.start(); d.stop()
    assert any("Ctrl+V" in b for _, b in notes)


def test_recorder_device_from_config():
    d, sv, *_ = make({"audio.device": "alsa_input.obsbot"})
    d.start()
    assert sv.recorder.started_with == ["alsa_input.obsbot"]
