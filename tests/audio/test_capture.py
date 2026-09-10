import io
import json
import subprocess
import threading

import numpy as np
import pytest

from voice.audio.capture import Recorder, RecorderError, list_sources, pw_record_command


def test_command_shape():
    assert pw_record_command(None) == ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "-"]
    assert "--target" in pw_record_command("alsa_input.usb-OBSBOT")
    assert pw_record_command("alsa_input.usb-OBSBOT")[-3:] == ["--target", "alsa_input.usb-OBSBOT", "-"]


class _Stdout:
    """pw-record's stdout: yields `data`, then stays open until the process ends.

    `eof_early` models the process dying mid-capture instead - the pipe closes
    while the recorder still believes it is recording.
    """

    def __init__(self, data: bytes, ended: threading.Event, eof_early: bool):
        self._buf = io.BytesIO(data)
        self._ended, self._eof_early = ended, eof_early

    def read(self, n=-1):
        chunk = self._buf.read(n)
        if chunk:
            return chunk
        if not self._eof_early:
            self._ended.wait(5)
        return b""


class FakeProc:
    def __init__(self, data: bytes, rc=0, stderr=b"", eof_early=False):
        self._ended = threading.Event()
        self.stdout = _Stdout(data, self._ended, eof_early)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self._rc = rc
        self.terminated = False
        if eof_early:
            self._ended.set()

    def terminate(self):
        self.terminated = True
        self.returncode = self._rc
        self._ended.set()

    def wait(self, timeout=None):
        self.returncode = self._rc
        self._ended.set()
        return self._rc

    def kill(self):
        self.returncode = -9
        self._ended.set()

    def poll(self):
        return self.returncode


class StubbornProc(FakeProc):
    """Ignores terminate(); like a real Popen, only wait() publishes a returncode."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.killed = False
        self.waits_after_kill = 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired("pw-record", timeout or 0)
        self.waits_after_kill += 1
        self.returncode = -9
        return -9

    def kill(self):
        self.killed = True
        self._ended.set()          # SIGKILL closes the pipe; returncode stays unset


def test_recorder_collects_pcm_until_stop():
    pcm = np.arange(1600, dtype=np.int16)
    proc = FakeProc(pcm.tobytes())
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    assert rec.is_recording
    out = rec.stop()
    assert proc.terminated
    assert np.array_equal(out, pcm)
    assert not rec.is_recording


def test_recorder_cancel_discards_audio():
    proc = FakeProc(b"\x01\x00" * 100)
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    rec.cancel()
    assert not rec.is_recording
    assert rec.stop().size == 0


def test_recorder_reports_process_failure():
    proc = FakeProc(b"", rc=1, stderr=b"pw-record: no such target\n")
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start("nope")
    rec.stop()
    assert "no such target" in (rec.error or "")


def test_recorder_tolerates_nonzero_exit_after_capturing_data():
    pcm = np.arange(1600, dtype=np.int16)
    proc = FakeProc(pcm.tobytes(), rc=1, stderr=b"pw-record: some warning\n")
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    out = rec.stop()
    assert rec.error is None
    assert np.array_equal(out, pcm)


def test_recorder_cancel_never_reports_error():
    proc = FakeProc(b"", rc=1, stderr=b"pw-record: terminated\n")
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    rec.cancel()
    assert rec.error is None
    assert rec.stop().size == 0


def test_recorder_spawn_failure_raises():
    def boom(*a, **k):
        raise FileNotFoundError("pw-record")
    with pytest.raises(RecorderError, match="pw-record"):
        Recorder(popen=boom).start(None)


def test_list_sources_parses_pw_dump():
    dump = [
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "media.class": "Audio/Source", "node.name": "alsa_input.usb-OBSBOT", "node.description": "OBSBOT Tiny 3"}}},
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "media.class": "Audio/Sink", "node.name": "alsa_output.hdmi", "node.description": "HDMI"}}},
        {"type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
         "metadata": [{"key": "default.audio.source", "value": {"name": "alsa_input.usb-OBSBOT"}}]},
    ]
    run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=json.dumps(dump), stderr="")
    sources = list_sources(run=run)
    assert [s.name for s in sources] == ["alsa_input.usb-OBSBOT"]
    assert sources[0].description == "OBSBOT Tiny 3"
    assert sources[0].is_default is True


def test_list_sources_tolerates_failure():
    run = lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no pipewire")
    assert list_sources(run=run) == []


def _await_reader(rec):
    """Block until the reader thread has finished observing the stream."""
    rec._reader.join(2)
    assert not rec._reader.is_alive()


def test_recorder_reports_a_process_that_dies_mid_capture():
    # pw-record exiting on its own (source unplugged, server restart) used to be
    # silent whenever any audio had already been captured.
    pcm = np.arange(1600, dtype=np.int16)
    proc = FakeProc(pcm.tobytes(), rc=1, stderr=b"pw-record: node disappeared\n", eof_early=True)
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    _await_reader(rec)
    out = rec.stop()
    assert "node disappeared" in (rec.error or "")
    assert np.array_equal(out, pcm)          # whatever was captured is still returned


def test_early_exit_without_stderr_still_reports_an_error():
    proc = FakeProc(b"\x01\x00" * 100, rc=1, eof_early=True)
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    _await_reader(rec)
    rec.stop()
    assert rec.error == "pw-record exited early"


def test_early_exit_is_not_reported_after_cancel():
    proc = FakeProc(b"\x01\x00" * 100, rc=1, eof_early=True)
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    _await_reader(rec)
    rec.cancel()
    assert rec.error is None


def test_kill_is_followed_by_wait_so_the_returncode_is_known():
    proc = StubbornProc(b"\x01\x00" * 100, rc=0)
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    rec.stop()
    assert proc.killed and proc.waits_after_kill == 1
    assert proc.returncode == -9             # unset until stop() waits on the kill


@pytest.mark.boundary
def test_real_pw_record_captures_half_second():
    import time
    rec = Recorder()
    rec.start(None)
    time.sleep(0.5)
    out = rec.stop()
    assert rec.error is None
    assert out.size > 16000 * 0.3


def test_recorder_reports_a_level_for_every_chunk():
    loud = np.full(2048, 12000, dtype=np.int16)     # 4096 bytes: exactly one read
    quiet = np.full(2048, 300, dtype=np.int16)
    proc = FakeProc(loud.tobytes() + quiet.tobytes())
    levels: list[float] = []
    rec = Recorder(popen=lambda *a, **k: proc, on_level=levels.append)
    rec.start(None)
    rec.stop()
    assert len(levels) == 2
    assert all(0.0 <= lvl <= 1.0 for lvl in levels)
    assert levels[0] > levels[1]                    # the loud chunk reads higher


def test_recorder_survives_a_raising_level_callback(caplog):
    pcm = np.full(4096, 5000, dtype=np.int16)       # 8192 bytes: two reads
    proc = FakeProc(pcm.tobytes())
    calls = []

    def boom(level):
        calls.append(level)
        raise RuntimeError("overlay pipe is broken")

    rec = Recorder(popen=lambda *a, **k: proc, on_level=boom)
    rec.start(None)
    with caplog.at_level("WARNING"):
        out = rec.stop()
    assert len(calls) == 2                          # the second chunk still arrived
    assert np.array_equal(out, pcm)                 # and capture kept every sample
    assert rec.error is None
    assert "overlay pipe is broken" in caplog.text


def test_recorder_without_a_level_callback_still_records():
    pcm = np.arange(1600, dtype=np.int16)
    proc = FakeProc(pcm.tobytes())
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    assert np.array_equal(rec.stop(), pcm)
