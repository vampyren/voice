import io
import json
import subprocess

import numpy as np
import pytest

from voice.audio.capture import Recorder, RecorderError, list_sources, pw_record_command


def test_command_shape():
    assert pw_record_command(None) == ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "-"]
    assert "--target" in pw_record_command("alsa_input.usb-OBSBOT")
    assert pw_record_command("alsa_input.usb-OBSBOT")[-3:] == ["--target", "alsa_input.usb-OBSBOT", "-"]


class FakeProc:
    def __init__(self, data: bytes, rc=0, stderr=b""):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self._rc = rc
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = self._rc

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def kill(self):
        self.returncode = -9

    def poll(self):
        return self.returncode


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


@pytest.mark.boundary
def test_real_pw_record_captures_half_second():
    import time
    rec = Recorder()
    rec.start(None)
    time.sleep(0.5)
    out = rec.stop()
    assert rec.error is None
    assert out.size > 16000 * 0.3
