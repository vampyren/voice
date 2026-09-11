import subprocess

from evdev import ecodes as e

from voice.inject.fallback import (ClipboardOnlySender, WtypeKeySender, YdotoolKeySender,
                                   keycode_to_xkb_name, make_key_sender)


class Runner:
    def __init__(self, present):
        self.present, self.calls = present, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] not in self.present:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_xkb_names():
    assert keycode_to_xkb_name(e.KEY_LEFTCTRL) == "ctrl"
    assert keycode_to_xkb_name(e.KEY_LEFTSHIFT) == "shift"
    assert keycode_to_xkb_name(e.KEY_V) == "v"
    assert keycode_to_xkb_name(e.KEY_INSERT) == "Insert"


def test_wtype_command_shape():
    r = Runner({"wtype"})
    WtypeKeySender(run=r).send_chord([e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_V])
    assert r.calls[-1] == ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]


def test_ydotool_command_shape():
    r = Runner({"ydotool"})
    YdotoolKeySender(run=r).send_chord([e.KEY_LEFTCTRL, e.KEY_V])
    assert r.calls[-1] == ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]


def test_make_key_sender_falls_back_in_order(monkeypatch):
    monkeypatch.setattr("voice.inject.fallback.portal_available", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ydotool" if name == "ydotool" else None)
    assert make_key_sender().name == "ydotool"
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert isinstance(make_key_sender(), ClipboardOnlySender)
    monkeypatch.setattr("voice.inject.fallback.portal_available", lambda: True)
    assert make_key_sender().name == "portal"
