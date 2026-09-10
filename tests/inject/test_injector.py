import subprocess

from voice.inject.clipboard import Snapshot
from voice.inject.injector import InjectResult, Injector, run_window_command
from voice.inject.keys import KeySendError

SETTINGS = {"paste_chord": "ctrl+v", "terminal_chord": "ctrl+shift+v",
            "terminal_classes": ["konsole", "kitty"], "restore_clipboard": True}


class FakeClipboard:
    def __init__(self, existing="old", restore_succeeds=True):
        self.existing, self.log, self.restore_succeeds = existing, [], restore_succeeds

    def snapshot(self):
        self.log.append("snapshot")
        return Snapshot(self.existing)

    def set_text(self, text):
        self.log.append(("set", text))

    def restore(self, snap):
        self.log.append(("restore", snap.text))
        return self.restore_succeeds and snap.text is not None


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


def test_restore_failure_reported_as_not_restored():
    clip = FakeClipboard(restore_succeeds=False)
    inj = Injector(clip, FakeSender(), SETTINGS, lambda: False, lambda: None, sleep=lambda s: None)
    res = inj.inject("x")
    assert res.restored is False
    assert ("restore", "old") in clip.log          # restore was attempted, just failed


class RecordingRun:
    def __init__(self, rc=0, stdout="", raise_exc=None):
        self.rc, self.stdout, self.raise_exc = rc, stdout, raise_exc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raise_exc is not None:
            raise self.raise_exc
        return subprocess.CompletedProcess(cmd, self.rc, stdout=self.stdout, stderr="")


def test_run_window_command_empty_returns_none_without_calling_run():
    run = RecordingRun()
    assert run_window_command("", run=run) is None
    assert run.calls == []


def test_run_window_command_returns_stripped_stdout_on_success():
    run = RecordingRun(rc=0, stdout="Konsole\n")
    assert run_window_command("xdotool getactivewindow", run=run) == "Konsole"
    cmd, kw = run.calls[-1]
    assert cmd == "xdotool getactivewindow"
    assert kw["shell"] is True
    assert kw["timeout"] == 1


def test_run_window_command_empty_stdout_returns_none():
    assert run_window_command("cmd", run=RecordingRun(rc=0, stdout="")) is None


def test_run_window_command_nonzero_exit_returns_none():
    assert run_window_command("cmd", run=RecordingRun(rc=1, stdout="whatever")) is None


def test_run_window_command_oserror_returns_none():
    assert run_window_command("cmd", run=RecordingRun(raise_exc=OSError("boom"))) is None


def test_unparseable_chord_leaves_the_text_on_the_clipboard():
    # A hand-edited inject.paste_chord must degrade to clipboard-only, not raise
    # out of inject() - which skipped the restore and lost the dictation.
    clip, sender = FakeClipboard(), FakeSender()
    inj = Injector(clip, sender, {**SETTINGS, "paste_chord": "hyper+v"},
                   lambda: False, lambda: None, sleep=lambda s: None)
    res = inj.inject("keep me")
    assert res == InjectResult("clipboard-only", "hyper+v", False)
    assert clip.log == ["snapshot", ("set", "keep me")]   # not restored over
    assert sender.chords == []


def test_unparseable_terminal_chord_is_handled_too():
    inj = Injector(FakeClipboard(), FakeSender(), {**SETTINGS, "terminal_chord": "ctrl+shift+nope"},
                   lambda: False, lambda: "konsole", sleep=lambda s: None)
    assert inj.inject("x").method == "clipboard-only"


def test_clipboard_mode_copies_without_touching_the_keyboard():
    # The deliberate mode for sessions where the chord goes into the void: copy,
    # and leave the text there. No snapshot, no chord, no restore over it.
    clip, sender = FakeClipboard(), FakeSender()
    slept = []
    inj = Injector(clip, sender, {**SETTINGS, "mode": "clipboard"},
                   modifiers_held=lambda: True, window_class=lambda: "firefox", sleep=slept.append)
    res = inj.inject("keep me")
    assert res == InjectResult(method="clipboard", chord="", restored=False)
    assert clip.log == [("set", "keep me")]
    assert sender.chords == []          # the sender was never called
    assert slept == []                  # no modifier wait, no settle


def test_clipboard_mode_keeps_the_text_even_with_restore_enabled():
    # restore_clipboard is true in a shipped config; the copy must survive it.
    clip = FakeClipboard()
    inj = Injector(clip, FakeSender(), {**SETTINGS, "mode": "clipboard", "restore_clipboard": True},
                   lambda: False, lambda: None, sleep=lambda s: None)
    inj.inject("x")
    assert not any(isinstance(e, tuple) and e[0] == "restore" for e in clip.log)
    assert "snapshot" not in clip.log


def test_paste_is_the_mode_a_config_without_the_key_gets():
    clip, sender = FakeClipboard(), FakeSender()
    res = Injector(clip, sender, SETTINGS, lambda: False, lambda: "firefox",
                   sleep=lambda s: None).inject("hello")
    assert res == InjectResult(method="fake", chord="ctrl+v", restored=True)
    assert sender.chords == [[29, 47]]
    # ...and an explicit "paste" behaves exactly the same way.
    clip2, sender2 = FakeClipboard(), FakeSender()
    res2 = Injector(clip2, sender2, {**SETTINGS, "mode": "paste"}, lambda: False,
                    lambda: "firefox", sleep=lambda s: None).inject("hello")
    assert res2 == res and clip2.log == clip.log and sender2.chords == sender.chords
