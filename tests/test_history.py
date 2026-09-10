import json

import numpy as np

from voice import paths
from voice.history import Entry, History


def e(text, ts=1.0):
    return Entry(text=text, ts=ts, backend="local", audio_s=1.2, elapsed_s=0.3)


def test_add_last_and_persist_roundtrip(isolated_xdg):
    h = History()
    assert h.last() is None
    h.add(e("one"))
    h.add(e("two", 2.0))
    assert h.last().text == "two"
    lines = paths.history_file().read_text().splitlines()
    assert json.loads(lines[-1])["text"] == "two"
    again = History()
    assert [x.text for x in again.entries()] == ["one", "two"]


def test_limit_is_enforced_on_add_and_load(isolated_xdg):
    h = History(limit=3)
    for i in range(5):
        h.add(e(str(i), float(i)))
    assert [x.text for x in h.entries()] == ["2", "3", "4"]
    assert len(paths.history_file().read_text().splitlines()) == 3


def test_corrupt_line_is_skipped(isolated_xdg):
    paths.history_file().write_text('{"text": "ok", "ts": 1, "backend": "x", "audio_s": 1, "elapsed_s": 0}\nnot json\n')
    assert [x.text for x in History().entries()] == ["ok"]


def test_retry_audio_is_taken_once(isolated_xdg):
    h = History()
    pcm = np.ones(10, dtype=np.int16)
    h.keep_audio(pcm)
    assert np.array_equal(h.take_audio(), pcm)
    assert h.take_audio() is None
