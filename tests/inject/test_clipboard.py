import subprocess

import pytest

from voice.inject.clipboard import Clipboard, ClipboardError, Snapshot


class Runner:
    def __init__(self, responses):
        self.responses = responses      # {argv_prefix: (rc, stdout)}
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        for prefix, (rc, out) in self.responses.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        raise FileNotFoundError(argv[0])


def test_snapshot_returns_text_when_text_type_present():
    r = Runner({("wl-paste", "--list-types"): (0, "text/plain;charset=utf-8\nTEXT\n"), ("wl-paste", "--no-newline"): (0, "old")})
    assert Clipboard(run=r).snapshot() == Snapshot("old")


def test_snapshot_is_none_for_image_or_empty():
    r = Runner({("wl-paste", "--list-types"): (0, "image/png\n")})
    assert Clipboard(run=r).snapshot() == Snapshot(None)
    r2 = Runner({("wl-paste", "--list-types"): (1, "")})
    assert Clipboard(run=r2).snapshot() == Snapshot(None)


def test_set_text_pipes_to_wl_copy():
    r = Runner({("wl-copy",): (0, "")})
    Clipboard(run=r).set_text("hej å ä ö")
    argv, stdin = r.calls[-1]
    assert argv[0] == "wl-copy" and "--type" in argv
    assert stdin == "hej å ä ö"


def test_set_text_raises_when_wl_copy_missing():
    with pytest.raises(ClipboardError, match="wl-copy"):
        Clipboard(run=Runner({})).set_text("x")


def test_restore_noop_when_none_and_sets_otherwise():
    r = Runner({("wl-copy",): (0, "")})
    c = Clipboard(run=r)
    c.restore(Snapshot(None))
    assert r.calls == []
    c.restore(Snapshot("back"))
    assert r.calls[-1][1] == "back"


@pytest.mark.boundary
def test_real_wl_clipboard_roundtrip():
    c = Clipboard()
    assert c.available()
    before = c.snapshot()
    c.set_text("voice-test-åäö")
    assert c.snapshot().text == "voice-test-åäö"
    c.restore(before)
