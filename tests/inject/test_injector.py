from voice.inject.clipboard import Snapshot
from voice.inject.injector import InjectResult, Injector
from voice.inject.keys import KeySendError

SETTINGS = {"paste_chord": "ctrl+v", "terminal_chord": "ctrl+shift+v",
            "terminal_classes": ["konsole", "kitty"], "restore_clipboard": True}


class FakeClipboard:
    def __init__(self, existing="old"):
        self.existing, self.log = existing, []

    def snapshot(self):
        self.log.append("snapshot")
        return Snapshot(self.existing)

    def set_text(self, text):
        self.log.append(("set", text))

    def restore(self, snap):
        self.log.append(("restore", snap.text))


class FakeSender:
    name = "fake"

    def __init__(self, fail=False):
        self.fail, self.chords = fail, []

    def send_chord(self, codes):
        if self.fail:
            raise KeySendError("nope")
        self.chords.append(codes)

    def available(self):
        return True


def test_happy_path_copy_chord_restore():
    clip, sender = FakeClipboard(), FakeSender()
    inj = Injector(clip, sender, SETTINGS, modifiers_held=lambda: False, window_class=lambda: "firefox", sleep=lambda s: None)
    res = inj.inject("hello")
    assert res == InjectResult(method="fake", chord="ctrl+v", restored=True)
    assert clip.log == ["snapshot", ("set", "hello"), ("restore", "old")]
    assert sender.chords == [[29, 47]]


def test_terminal_gets_terminal_chord_case_insensitive():
    inj = Injector(FakeClipboard(), s := FakeSender(), SETTINGS, lambda: False, lambda: "Konsole", sleep=lambda s: None)
    assert inj.inject("x").chord == "ctrl+shift+v"
    assert s.chords == [[29, 42, 47]]


def test_waits_for_modifiers_then_gives_up_after_budget():
    ticks = iter([True] * 5 + [False] * 100)
    slept = []
    inj = Injector(FakeClipboard(), s := FakeSender(), SETTINGS, lambda: next(ticks), lambda: None, sleep=slept.append)
    inj.inject("x")
    assert len([t for t in slept if t == 0.02]) == 5
    assert s.chords            # chord sent after modifiers released

    always = Injector(FakeClipboard(), s2 := FakeSender(), SETTINGS, lambda: True, lambda: None, sleep=slept.append)
    always.inject("x")
    assert s2.chords           # sent anyway after the 1.5 s budget


def test_sender_failure_leaves_text_on_clipboard():
    clip = FakeClipboard()
    inj = Injector(clip, FakeSender(fail=True), SETTINGS, lambda: False, lambda: None, sleep=lambda s: None)
    res = inj.inject("keep me")
    assert res.method == "clipboard-only" and res.restored is False
    assert ("restore", "old") not in clip.log


def test_restore_disabled():
    clip = FakeClipboard()
    inj = Injector(clip, FakeSender(), {**SETTINGS, "restore_clipboard": False}, lambda: False, lambda: None, sleep=lambda s: None)
    assert inj.inject("x").restored is False
    assert not any(isinstance(x, tuple) and x[0] == "restore" for x in clip.log)
