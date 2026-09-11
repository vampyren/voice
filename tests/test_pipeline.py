import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from voice.audio.capture import Source
from voice.history import Entry, History
from voice.inject.injector import InjectResult
from voice.pipeline import (DEFAULT_STT_TIMEOUT, INJECT_BOUND, NO_MICROPHONE, STUCK_GRACE,
                            Dictation, Services, State)
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

    def transcribe(self, pcm, language, prompt, hotwords=None):
        self.calls.append((pcm.size, language, prompt))
        self.hotwords = hotwords
        if self.fail:
            raise TranscriptionError("cloud down")
        return Transcript(self.text, language, 1.0, 0.1, self.name)

    def warmup(self): pass
    def describe(self): return "fake"


class BlockingTranscriber:
    """A transcriber whose transcribe() blocks until the test releases it, for real cross-thread tests."""
    name = "blocking"

    def __init__(self, text="hello world"):
        self.text, self.calls = text, []
        self.release = threading.Event()

    def transcribe(self, pcm, language, prompt, hotwords=None):
        self.calls.append((pcm.size, language, prompt))
        self.release.wait(5)
        return Transcript(self.text, language, 1.0, 0.1, self.name)

    def warmup(self): pass
    def describe(self): return "blocking"


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


#: What `make()` says PipeWire is holding: one working microphone.
A_MICROPHONE = [Source("mic", "Some Microphone", True)]


def make(cfg=None, rec=None, stt=None, inj=None, executor=None, notify=None, sources=None):
    cfg = {"hotkeys.dictate_mode": "hold", "audio.device": "", "audio.max_seconds": 120,
           "general.language": "en", "dictionary.replacements": [["cachy os", "CachyOS", "icase"]], **(cfg or {})}
    notes = []
    services = Services(
        recorder=rec or FakeRecorder(), transcriber=stt or FakeTranscriber(), injector=inj or FakeInjector(),
        history=History(), notify=notify or (lambda t, b, u="normal": notes.append((t, b))),
        trim=lambda pcm: pcm, config_getter=lambda k, d=None: cfg.get(k, d), prompt_getter=lambda: "CachyOS",
        hotwords_getter=lambda: "CachyOS, OBSBOT",
        sources=sources if sources is not None else (lambda: list(A_MICROPHONE)))
    states = []
    d = Dictation(services, executor=executor or (lambda fn: fn()), timer_factory=FakeTimer)
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
    assert d.last_error is None                  # cleared on the next successful dictation
    d.retry()                                   # nothing left to retry
    assert len(sv.injector.texts) == 1


def test_cancel_discards_recording():
    d, sv, states, _ = make()
    d.start(); d.on_hotkey("cancel", "press")
    assert sv.recorder.cancelled == 1 and d.state == State.IDLE and sv.transcriber.calls == []


def _timer(seconds):
    """The one armed timer with this bound - start() also arms a state guard."""
    found = [t for t in FakeTimer.instances if t.seconds == seconds and not t.cancelled]
    assert len(found) == 1, f"expected one live {seconds}s timer, got {found}"
    return found[0]


def test_max_seconds_timer_stops_recording():
    d, sv, states, _ = make({"audio.max_seconds": 7})
    d.start()
    timer = _timer(7)
    timer.fire()
    assert d.state == State.IDLE and sv.injector.texts == ["hello world"]
    d.start()
    second_timer = _timer(7)
    assert second_timer is not timer and not second_timer.cancelled
    d.stop()
    assert second_timer.cancelled


def test_operations_ignored_while_transcribing_pending_then_recall_reinjects():
    pending = []
    d, sv, states, _ = make(executor=pending.append)
    kept = np.ones(4000, dtype=np.int16)
    sv.history.keep_audio(kept)  # simulate audio kept from an earlier retry-able error
    # A prior entry so recall()'s "nothing to recall" short-circuit (last is None)
    # can't mask the state guard below - we need recall() to actually reach it.
    sv.history.add(Entry("previous", time.time(), "fake", 1.0, 0.1))

    d.start()
    d.stop()
    assert d.state == State.TRANSCRIBING
    assert len(pending) == 1

    # All of these must be no-ops while the worker is pending: state stays
    # TRANSCRIBING, the recorder is not started again, retry() must not
    # silently discard whatever audio history is holding onto, and recall()
    # must not reinject despite there being a prior entry to recall.
    d.on_hotkey("dictate", "press")
    d.toggle()
    d.recall()
    d.retry()
    assert d.state == State.TRANSCRIBING
    assert sv.recorder.started_with == [None]
    assert len(pending) == 1
    assert sv.injector.texts == []
    # retry() while TRANSCRIBING must not have silently drained the kept audio.
    # (Checked before the worker runs: completing a fresh dictation deliberately
    # discards older retry audio - see the stale-retry-audio test below.)
    assert sv.history.take_audio() is kept

    pending.pop(0)()  # run the deferred worker
    assert states == [State.RECORDING, State.TRANSCRIBING, State.INJECTING, State.IDLE]
    assert sv.injector.texts == ["hello world"]

    d.recall()
    assert len(pending) == 1
    pending.pop(0)()
    assert sv.injector.texts == ["hello world", "hello world"]


def test_recall_reserves_injecting_so_start_is_a_no_op_until_worker_runs():
    pending = []
    d, sv, states, _ = make(executor=pending.append)
    sv.history.add(Entry("hello world", time.time(), "fake", 1.0, 0.1))

    d.recall()
    assert d.state == State.INJECTING
    assert len(pending) == 1

    d.start()  # must be a no-op: state is INJECTING, not IDLE
    assert d.state == State.INJECTING
    assert sv.recorder.started_with == []

    pending.pop(0)()
    assert d.state == State.IDLE
    assert sv.recorder.started_with == []
    assert sv.injector.texts == ["hello world"]


def test_recall_path_exception_after_injection_still_reaches_idle():
    def boom_notify(title, body, urgency="normal"):
        raise RuntimeError("dbus down")

    d, sv, states, _ = make(inj=FakeInjector(method="clipboard-only"), notify=boom_notify)
    sv.history.add(Entry("hello world", time.time(), "fake", 1.0, 0.1))

    d.recall()  # clipboard-only triggers the "Text copied" notify, which raises
    assert d.state == State.IDLE
    assert d.last_error and "dbus down" in d.last_error


def test_cross_thread_stop_blocks_start_until_injection_completes():
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-dictation")
    try:
        stt = BlockingTranscriber()
        d, sv, states, _ = make(stt=stt, executor=lambda fn: pool.submit(fn))

        d.start()
        d.stop()

        deadline = time.monotonic() + 1.0
        while d.state != State.TRANSCRIBING and time.monotonic() < deadline:
            time.sleep(0.01)
        assert d.state == State.TRANSCRIBING

        d.start()  # ignored: a dictation is already in flight on the worker thread
        assert sv.recorder.started_with == [None]

        stt.release.set()

        # _set() assigns self._state before invoking on_state(), so polling on
        # d.state alone can observe IDLE before the worker thread has appended
        # its final entry to `states` - poll on the callback's own record instead.
        deadline = time.monotonic() + 2.0
        while len(states) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert d.state == State.IDLE
        assert sv.injector.texts == ["hello world"]
        assert states == [State.RECORDING, State.TRANSCRIBING, State.INJECTING, State.IDLE]
    finally:
        pool.shutdown(wait=True)


def test_clipboard_only_result_notifies_user():
    d, sv, states, notes = make(inj=FakeInjector(method="clipboard-only"))
    d.start(); d.stop()
    assert any("Ctrl+V" in b for _, b in notes)


def test_recorder_device_from_config():
    d, sv, *_ = make({"audio.device": "alsa_input.obsbot"})
    d.start()
    assert sv.recorder.started_with == ["alsa_input.obsbot"]


def test_bad_max_seconds_fails_before_the_recorder_is_started():
    # A malformed audio.max_seconds must be caught before pw-record is spawned,
    # or the process is orphaned behind a dead listener thread.
    d, sv, states, notes = make({"audio.max_seconds": "2m"})
    d.start()
    assert d.state == State.IDLE
    assert sv.recorder.started_with == [] and sv.recorder.is_recording is False
    assert State.ERROR in states
    assert notes and "2m" in notes[-1][1]


def test_non_positive_max_seconds_is_rejected():
    d, sv, states, notes = make({"audio.max_seconds": 0})
    d.start()
    assert d.state == State.IDLE and sv.recorder.started_with == []
    assert notes and "max_seconds" in notes[-1][1]


def test_start_cancels_the_recorder_when_the_timer_cannot_be_armed():
    def boom_timer(seconds, fn):
        raise RuntimeError("no timers left")

    d, sv, states, notes = make()
    d._timer_factory = boom_timer
    d.start()
    assert d.state == State.IDLE
    assert sv.recorder.cancelled == 1 and sv.recorder.is_recording is False
    assert notes and "no timers left" in notes[-1][1]


def test_on_hotkey_never_raises_into_the_listener_thread():
    d, sv, states, notes = make()

    def boom(key, default=None):
        raise RuntimeError("config gone")

    d.sv.config_getter = boom
    d.on_hotkey("dictate", "press")          # must not propagate to the evdev thread
    assert d.state == State.IDLE
    assert d.last_error and "config gone" in d.last_error


def test_a_later_dictation_clears_the_previous_failures_retry_audio():
    # "Retry last recording" must only ever re-run the most recent recording:
    # keep_audio was set on error and never cleared, so a retry long after a
    # successful dictation re-pasted a stale one.
    stt = FakeTranscriber(fail=True)
    d, sv, states, notes = make(stt=stt)
    d.start(); d.stop()                            # A fails: its audio is kept for retry
    stt.fail = False
    d.start(); d.stop()                            # B succeeds
    assert sv.injector.texts == ["hello world"]

    d.retry()
    assert sv.injector.texts == ["hello world"]    # nothing re-pasted
    assert len(stt.calls) == 2                     # and nothing re-transcribed
    assert sv.history.take_audio() is None


class ExplodingStopRecorder(FakeRecorder):
    """A recorder whose stop() blows up mid-dictation, leaving pw-record running."""

    def stop(self):
        raise RuntimeError("pw-record wedged")


def test_a_failing_stop_still_cancels_the_recorder():
    rec = ExplodingStopRecorder()
    d, sv, states, notes = make(rec=rec)
    d.on_hotkey("dictate", "press")
    assert d.state == State.RECORDING

    d.on_hotkey("dictate", "release")            # stop() raises on the listener thread
    assert d.state == State.IDLE
    assert rec.cancelled == 1 and rec.is_recording is False
    assert d.last_error and "pw-record wedged" in d.last_error


def test_the_catch_all_does_not_cancel_when_no_recording_is_in_flight():
    d, sv, states, notes = make()

    def boom(key, default=None):
        raise RuntimeError("config gone")

    d.sv.config_getter = boom
    d.on_hotkey("dictate", "press")              # fails before anything was recorded
    assert sv.recorder.cancelled == 0
    assert d.state == State.IDLE


# -- the worker thread ---------------------------------------------------------
def test_the_dictation_worker_runs_jobs_in_order_on_a_daemon_thread():
    """A ThreadPoolExecutor's threads are joined at interpreter exit, so a job
    still running there held a quitting daemon open. This one cannot."""
    from voice.pipeline import Worker

    done = threading.Event()
    seen = []
    worker = Worker()
    worker.submit(lambda: seen.append(1))
    worker.submit(lambda: seen.append(2))
    worker.submit(done.set)
    assert done.wait(2)
    assert seen == [1, 2]
    assert worker._thread.daemon is True
    worker.shutdown()


def test_shutdown_drops_work_queued_behind_the_running_job():
    from voice.pipeline import Worker

    started, release = threading.Event(), threading.Event()
    ran = []
    worker = Worker()
    worker.submit(lambda: (started.set(), release.wait(5)))
    worker.submit(lambda: ran.append("queued"))
    assert started.wait(2)
    worker.shutdown()
    release.set()
    time.sleep(0.1)
    assert ran == []                       # quitting does not wait for it


def test_a_job_that_raises_does_not_kill_the_worker(caplog):
    from voice.pipeline import Worker

    done = threading.Event()
    worker = Worker()
    with caplog.at_level("ERROR", logger="voice.pipeline"):
        worker.submit(lambda: 1 / 0)
        worker.submit(done.set)
        assert done.wait(2)
    assert "dictation job failed" in caplog.text
    worker.shutdown()


def _recording_notify():
    """A notify that keeps the urgency the pipeline chose, unlike make()'s default."""
    calls = []
    return calls, lambda title, body, urgency="normal": calls.append((title, body, urgency))


def test_deliberate_clipboard_mode_says_press_ctrl_v_without_alarming_the_user():
    # inject.mode = "clipboard" is a choice, not a failure: friendly wording,
    # normal urgency, and the dictation still lands in history and IDLE.
    calls, notify = _recording_notify()
    d, sv, states, _ = make(inj=FakeInjector(method="clipboard"), notify=notify)
    d.start(); d.stop()
    assert len(calls) == 1
    title, body, urgency = calls[0]
    assert urgency == "normal"
    assert "could not" not in (title + body).lower()      # nothing went wrong
    assert "Ctrl+V" in body
    assert states[-1] == State.IDLE
    assert sv.history.last().text == "hello world"
    assert sv.injector.texts == ["hello world"]


def test_the_failure_path_keeps_its_own_wording():
    calls, notify = _recording_notify()
    d, sv, states, _ = make(inj=FakeInjector(method="clipboard-only"), notify=notify)
    d.start(); d.stop()
    assert calls == [("Text copied", "Could not paste automatically. Paste with Ctrl+V.", "normal")]
    assert states[-1] == State.IDLE
    assert sv.history.last().text == "hello world"


def test_a_successful_paste_still_notifies_nothing():
    calls, notify = _recording_notify()
    d, sv, states, _ = make(notify=notify)
    d.start(); d.stop()
    assert calls == []
    assert states[-1] == State.IDLE


def test_the_focus_stealing_pill_explains_itself_once_and_only_once():
    """`clipboard-pill`: the paste was refused because the pill has the keyboard.

    That needs its own wording - "could not paste" would send the owner
    debugging the portal - and it needs to be said once, not on every single
    dictation for the rest of the session.
    """
    calls, notify = _recording_notify()
    d, sv, states, _ = make(inj=FakeInjector(method="clipboard-pill"), notify=notify)
    d.start(); d.stop()
    assert len(calls) == 1
    title, body, urgency = calls[0]
    assert urgency == "normal"
    assert "pill" in body.lower() and "Ctrl+V" in body
    assert "could not" not in (title + body).lower()      # nothing is broken
    assert states[-1] == State.IDLE
    assert sv.history.last().text == "hello world"

    d.start(); d.stop()
    assert len(calls) == 1, f"said it again: {calls}"
    assert sv.injector.texts == ["hello world", "hello world"]


def test_the_idle_detail_names_the_method_readably():
    """The daemon reads the method back out of this line to decide what the pill
    says, so the two have to agree about its shape."""
    from voice.pipeline import detail_method

    d, sv, states, _ = make(inj=FakeInjector(method="clipboard"))
    details = []
    d.on_state = lambda s, detail: details.append((s, detail))
    d.start(); d.stop()
    idle = [detail for state, detail in details if state is State.IDLE][-1]
    assert detail_method(idle) == "clipboard"
    assert detail_method("11 chars via portal in 0.9s") == "portal"
    assert detail_method("cancelled") == ""
    assert detail_method("") == ""


# -- a dictation that hangs -----------------------------------------------
#: The owner started a recording with no microphone connected, the daemon went
#: to `transcribing` and stayed there: the tray stuck amber, `voice status`
#: still saying transcribing many minutes later, and only a restart cleared it.
#: Nothing bounded a transcription, nothing could abandon one, and a missing
#: capture device was never reported at all.


def _pooled(**kw):
    """A dictation whose worker really is another thread, with its futures."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-dictation")
    futures = []
    d, sv, states, notes = make(executor=lambda fn: futures.append(pool.submit(fn)), **kw)
    return pool, futures, d, sv, states, notes


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), "timed out waiting for the worker thread"


def test_a_transcription_that_never_returns_is_abandoned_and_kept_for_retry():
    """The watchdog: the attempt is dropped, the audio stays retryable, the
    owner is told why, and the state is idle again."""
    stt = BlockingTranscriber()
    pool, futures, d, sv, states, notes = _pooled(stt=stt)
    try:
        d.start(); d.stop()
        _wait(lambda: stt.calls)                       # it is inside transcribe()

        _timer(DEFAULT_STT_TIMEOUT).fire()             # the watchdog goes off

        assert d.state == State.IDLE
        assert State.ERROR in states
        assert notes and "Retry" in notes[-1][1]
        assert d.last_error and d.last_error.startswith("transcription timed out")
        assert "abandoned" in d.last_error

        stt.release.set()                              # the late result lands
        futures[0].result(5)
        assert sv.injector.texts == [], "a late transcript must not be injected"
        assert sv.history.last() is None
        assert states == [State.RECORDING, State.TRANSCRIBING, State.ERROR, State.IDLE]
        kept = sv.history.take_audio()
        assert kept is not None and kept.size == 16000
    finally:
        pool.shutdown(wait=True)


def test_the_transcription_watchdog_bound_is_configurable():
    d, sv, states, _ = make({"stt.timeout_seconds": 45}, executor=lambda fn: None)
    d.start(); d.stop()
    assert d.state == State.TRANSCRIBING
    assert _timer(45) is not None


def test_a_retry_after_a_timeout_still_runs_its_own_dictation():
    """The abandoned worker thread must not take the queue with it."""
    stt = BlockingTranscriber()
    pool, futures, d, sv, states, notes = _pooled(stt=stt)
    try:
        d.start(); d.stop()
        _wait(lambda: stt.calls)
        _timer(DEFAULT_STT_TIMEOUT).fire()
        stt.release.set()
        futures[0].result(5)

        d.retry()
        futures[-1].result(5)
        assert sv.injector.texts == ["hello world"]
    finally:
        pool.shutdown(wait=True)


def test_cancel_abandons_a_transcription_that_has_not_started_yet():
    pending = []
    d, sv, states, _ = make(executor=pending.append)
    d.start(); d.stop()
    assert d.state == State.TRANSCRIBING

    d.on_hotkey("cancel", "press")

    assert d.state == State.IDLE
    assert states == [State.RECORDING, State.TRANSCRIBING, State.IDLE]
    pending.pop(0)()                       # the worker finally gets to it
    assert sv.transcriber.calls == [] and sv.injector.texts == []


def test_cancel_during_a_running_transcription_returns_to_idle():
    """The owner's way out of a stuck conversion: the cancel shortcut."""
    stt = BlockingTranscriber()
    pool, futures, d, sv, states, notes = _pooled(stt=stt)
    try:
        d.start(); d.stop()
        _wait(lambda: stt.calls)

        d.on_hotkey("cancel", "press")

        assert d.state == State.IDLE
        stt.release.set()
        futures[0].result(5)
        assert sv.injector.texts == [], "the late transcript is discarded"
        assert sv.history.last() is None
        assert sv.history.take_audio() is not None, "the recording stays retryable"
    finally:
        pool.shutdown(wait=True)


def test_no_microphone_refuses_to_record_and_says_so():
    """pw-record does not fail without a capture device - it records nothing,
    or silence, and says nothing. So the check has to happen before it runs."""
    d, sv, states, notes = make(sources=lambda: [])

    d.start()

    assert d.state == State.IDLE
    assert sv.recorder.started_with == [], "pw-record must not be spawned"
    assert states == [], "and this is not an error flash either"
    assert notes and notes[-1][0] == NO_MICROPHONE
    assert "microphone" in notes[-1][1].lower()


def test_an_unanswerable_probe_never_blocks_a_recording():
    """pw-dump missing or broken is not the same answer as "no microphone":
    refusing there would break dictation on a machine that records perfectly."""
    for probe in (lambda: None, _raises):
        d, sv, states, notes = make(sources=probe)
        d.start()
        assert d.state == State.RECORDING and sv.recorder.started_with == [None]
        assert notes == []


def _raises():
    raise RuntimeError("pw-dump is not installed")


def test_a_recording_that_outlives_its_bound_returns_to_idle():
    d, sv, states, notes = make()
    d.start()

    _timer(120 + STUCK_GRACE).fire()

    assert d.state == State.IDLE
    assert sv.recorder.cancelled == 1, "pw-record must not be left running"
    assert notes and "recording" in notes[-1][1]


def test_an_injection_that_outlives_its_bound_returns_to_idle():
    pending = []
    d, sv, states, notes = make(executor=pending.append)
    sv.history.add(Entry("hello world", time.time(), "fake", 1.0, 0.1))
    d.recall()
    assert d.state == State.INJECTING

    _timer(INJECT_BOUND).fire()

    assert d.state == State.IDLE
    assert notes and "injecting" in notes[-1][1]


def test_a_guard_belonging_to_a_state_already_left_does_nothing():
    d, sv, states, notes = make()
    d.start()
    guard = _timer(120 + STUCK_GRACE)
    d.stop()                                  # recording is over; so is its guard
    assert guard.cancelled

    guard.fire()                              # a timer that was already running

    assert d.state == State.IDLE and states[-1] == State.IDLE
    assert notes == []


def test_idle_is_allowed_to_wait_for_ever():
    d, sv, states, notes = make()
    assert d.state == State.IDLE
    assert [t for t in FakeTimer.instances if not t.cancelled] == []


def test_the_vocabulary_reaches_the_backend_with_every_transcription():
    stt = FakeTranscriber("hello")
    d, sv, states, notes = make(stt=stt)
    d.start()
    d.stop()
    assert stt.hotwords == "CachyOS, OBSBOT"
