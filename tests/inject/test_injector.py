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
    # A known window: an unknown one is never restored over at all, which is
    # its own test below.
    inj = Injector(clip, FakeSender(), SETTINGS, lambda: False, lambda: "firefox",
                   sleep=lambda s: None)
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


# -- a pill that takes keyboard focus (GNOME: no layer-shell) -----------------
class Recorder:
    """One log of everything the injector does, so ordering can be asserted."""

    def __init__(self):
        self.log = []

    def hide_pill(self):
        self.log.append("hide pill")

    def sleep(self, seconds):
        self.log.append(("sleep", round(seconds, 3)))

    def sender(self):
        rec = self

        class Sender:
            name = "fake"

            def send_chord(self, codes):
                rec.log.append(("chord", codes))

            def available(self):
                return True

        return Sender()


def _injector(rec, policy, settle=0.15, **kwargs):
    return Injector(FakeClipboard(), rec.sender(), SETTINGS, modifiers_held=lambda: False,
                    window_class=lambda: "firefox", sleep=rec.sleep,
                    pill_policy=policy, hide_pill=rec.hide_pill, settle_s=settle, **kwargs)


def test_the_pill_is_hidden_before_the_chord_and_the_compositor_given_a_moment():
    """The owner's "ctrl-v dont work with auto paste".

    Without layer-shell the pill is an ordinary window and it has the keyboard
    when the chord is sent, so the paste lands in the pill. Taking it off screen
    first and letting the compositor hand focus back is what fixes it - and the
    order is the whole fix: hide, settle, *then* chord.
    """
    rec = Recorder()
    res = _injector(rec, "hide", settle=0.2).inject("hello")
    assert rec.log == ["hide pill", ("sleep", 0.2), ("chord", [29, 47]), ("sleep", 0.15)]
    assert res.method == "fake" and res.restored is True


def test_a_pill_that_cannot_take_focus_is_left_alone():
    """With a real layer-shell surface the pill never has the keyboard, so
    hiding it would be a flicker for nothing."""
    rec = Recorder()
    _injector(rec, "none").inject("hello")
    assert "hide pill" not in rec.log


def test_the_clipboard_policy_refuses_to_paste_into_the_pill():
    """The honest second line of defence: no chord at all, and no restore.

    Sending it anyway loses the transcript outright - the chord goes to the
    pill and `restore_clipboard` puts the old contents back 150 ms later.
    """
    rec = Recorder()
    clip = FakeClipboard()
    inj = Injector(clip, rec.sender(), SETTINGS, lambda: False, lambda: None,
                   sleep=rec.sleep, pill_policy="clipboard", hide_pill=rec.hide_pill)
    res = inj.inject("hello")
    assert res == InjectResult(method="clipboard-pill", chord="", restored=False)
    assert clip.log == [("set", "hello")]        # no snapshot, and nothing restored
    assert rec.log == []                         # no chord, no hide, no sleep


def test_the_paste_policy_is_the_escape_hatch_and_still_sends_the_chord():
    """For a user whose compositor does hand the chord on regardless.

    The pill is left holding the keyboard by design. That no longer costs the
    chord its target: the window was read when the dictation began, before the
    pill existed, so it is known here like anywhere else.
    """
    rec = Recorder()
    res = _injector(rec, "paste").inject("hello")
    assert "hide pill" not in rec.log
    assert ("chord", [29, 47]) in rec.log
    assert res.method == "fake"


def test_hiding_the_pill_is_never_allowed_to_stop_the_paste():
    """The pill is decoration; a helper that has died must not eat the text."""
    def boom():
        raise RuntimeError("the helper is gone")

    rec = Recorder()
    inj = Injector(FakeClipboard(), rec.sender(), SETTINGS, lambda: False, lambda: None,
                   sleep=rec.sleep, pill_policy="hide", hide_pill=boom, settle_s=0.2)
    # The window is unknown here, so the paste is reported as unverifiable -
    # what matters to this test is that it was sent at all.
    assert inj.inject("hello").method == "paste-blind"
    assert ("chord", [29, 47]) in rec.log


def test_an_explicit_clipboard_mode_still_wins_over_the_pill_policy():
    """`inject.mode = "clipboard"` is the user's own choice, not a workaround."""
    rec = Recorder()
    inj = Injector(FakeClipboard(), rec.sender(), dict(SETTINGS, mode="clipboard"),
                   lambda: False, lambda: None, sleep=rec.sleep,
                   pill_policy="clipboard", hide_pill=rec.hide_pill)
    assert inj.inject("hello").method == "clipboard"


# -- saying where the text went ----------------------------------------------
#: Nothing in the daemon's log said which chord a dictation was pasted with or
#: what window it was aimed at, so a paste that silently went nowhere left
#: nothing behind to work it out from afterwards.

def test_the_chord_and_the_window_it_was_aimed_at_are_logged(caplog):
    inj = Injector(FakeClipboard(), FakeSender(), SETTINGS, lambda: False,
                   lambda: "Konsole", sleep=lambda s: None)
    with caplog.at_level("INFO", logger="voice.inject.injector"):
        inj.inject("hello")
    assert "ctrl+shift+v" in caplog.text
    assert "konsole" in caplog.text.lower()


def test_an_unknown_window_is_logged_as_unknown_rather_than_left_out(caplog):
    inj = Injector(FakeClipboard(), FakeSender(), SETTINGS, lambda: False,
                   lambda: None, sleep=lambda s: None)
    with caplog.at_level("INFO", logger="voice.inject.injector"):
        inj.inject("hello")
    assert "ctrl+v" in caplog.text
    # The distinction that matters: "we asked and got nothing" is why the
    # terminal chord can never be chosen, and it has to be visible.
    assert "unknown" in caplog.text.lower()


def test_the_window_is_asked_only_once_the_pill_is_off_screen():
    """kdotool and friends name whatever has focus *now*.

    Asked while the focus-stealing pill is still up, they name the pill - so
    the class is never a terminal, the terminal chord can never be chosen on
    the very desktops that will answer, and the new log line records the pill
    as the window the paste was aimed at.
    """
    order = []

    def window_class():
        order.append("asked")
        return "org.gnome.Ptyxis"

    inj = Injector(FakeClipboard(), s := FakeSender(),
                   {**SETTINGS, "terminal_classes": ["org.gnome.ptyxis"]},
                   modifiers_held=lambda: False, window_class=window_class,
                   sleep=lambda _: None, pill_policy="hide",
                   hide_pill=lambda: order.append("pill hidden"))

    res = inj.inject("hello")

    assert order == ["pill hidden", "asked"], \
        "the focused window was read before the pill got out of the way"
    assert res.chord == "ctrl+shift+v"
    assert s.chords == [[29, 42, 47]]


# -- admitting that the paste could not be verified ---------------------------
#: The compositor accepts the chord whatever window has focus, so a paste into
#: a terminal that ignores ctrl+v is indistinguishable from one that worked.
#: Where the window cannot be read at all, the result says so, and the pill
#: turns its bare checkmark into "the text is on the clipboard, here is how".

def test_a_paste_aimed_at_an_unknown_window_is_reported_as_unverified():
    inj = Injector(FakeClipboard(), FakeSender(), SETTINGS, lambda: False,
                   window_class=lambda: None, sleep=lambda _: None)
    res = inj.inject("hello")
    assert res.method == "paste-blind"
    assert res.chord == "ctrl+v"


def test_a_paste_into_a_window_we_could_read_is_reported_normally():
    inj = Injector(FakeClipboard(), FakeSender(), SETTINGS, lambda: False,
                   window_class=lambda: "firefox", sleep=lambda _: None)
    assert inj.inject("hello").method == "fake"


def test_an_unknown_window_is_not_flagged_when_one_chord_serves_everything():
    # The owner has already decided: same chord for terminals and everything
    # else, so not knowing the window costs nothing and there is nothing to say.
    settings = {**SETTINGS, "paste_chord": "ctrl+shift+v"}
    inj = Injector(FakeClipboard(), FakeSender(), settings, lambda: False,
                   window_class=lambda: None, sleep=lambda _: None)
    assert inj.inject("hello").method == "fake"


def test_an_unverifiable_paste_never_takes_the_transcript_off_the_clipboard():
    """The hint and the clipboard have to agree.

    The pill says "Use Ctrl+Shift+V" precisely when nothing can confirm the
    chord reached anything - so the clipboard is the only copy left. Restoring
    the previous contents over it, which is the default, turns that hint into
    an instruction to paste whatever happened to be on the clipboard before.
    """
    clip = FakeClipboard(existing="PREVIOUS CLIPBOARD")
    inj = Injector(clip, FakeSender(), SETTINGS, lambda: False,
                   window_class=lambda: None, sleep=lambda _: None)

    res = inj.inject("the dictation")

    assert res.method == "paste-blind"
    assert res.restored is False, "the transcript was replaced by the old clipboard"
    assert ("restore", "PREVIOUS CLIPBOARD") not in clip.log
    assert clip.log[-1] == ("set", "the dictation")


def test_a_verifiable_paste_still_restores_the_clipboard_as_before():
    clip = FakeClipboard(existing="PREVIOUS CLIPBOARD")
    inj = Injector(clip, FakeSender(), SETTINGS, lambda: False,
                   window_class=lambda: "firefox", sleep=lambda _: None)

    res = inj.inject("the dictation")

    assert res.method == "fake" and res.restored is True
    assert ("restore", "PREVIOUS CLIPBOARD") in clip.log


# -- the window is whatever the dictation began in ---------------------------
#: Read once, before the pill was on screen, and replayed here. The pill takes
#: the keyboard when it maps, so a class read at paste time names the pill and
#: never a terminal; reading it up front removes the question rather than
#: timing a settle against it.

def test_the_window_the_dictation_began_in_is_the_one_the_chord_is_chosen_for():
    inj = Injector(FakeClipboard(), s := FakeSender(),
                   {**SETTINGS, "terminal_classes": ["org.kde.konsole"]},
                   modifiers_held=lambda: False,
                   window_class=lambda: "org.kde.konsole", sleep=lambda _: None,
                   pill_policy="hide", hide_pill=lambda: None)

    res = inj.inject("hello")

    assert res.chord == "ctrl+shift+v" and res.method == "fake"
    assert s.chords == [[29, 42, 47]]


def test_a_pill_that_could_not_be_hidden_no_longer_costs_the_chord_its_target():
    """The hide failing used to make the window unknowable. It does not now."""
    def boom():
        raise RuntimeError("the helper is gone")

    inj = Injector(FakeClipboard(), FakeSender(),
                   {**SETTINGS, "terminal_classes": ["org.kde.konsole"]},
                   modifiers_held=lambda: False,
                   window_class=lambda: "org.kde.konsole", sleep=lambda _: None,
                   pill_policy="hide", hide_pill=boom)

    res = inj.inject("the dictation")

    assert res.chord == "ctrl+shift+v"


def test_a_hidden_pill_still_lets_the_real_window_be_trusted():
    inj = Injector(FakeClipboard(), FakeSender(), SETTINGS, lambda: False,
                   window_class=lambda: "konsole", sleep=lambda _: None,
                   pill_policy="hide", hide_pill=lambda: None)

    res = inj.inject("hello")

    assert res.chord == "ctrl+shift+v" and res.method == "fake"


def test_no_terminal_chord_configured_means_there_is_nothing_to_be_blind_about():
    """An empty `inject.terminal_chord` is "one chord for every window".

    Treated as a second chord we failed to reach, it made every paste
    unverifiable: `restore_clipboard` stopped working, and the pill had no
    chord to name so it fell back to a plain checkmark - the exact "it says it
    worked and it didn't" illusion this is all here to remove.
    """
    settings = {**SETTINGS, "terminal_chord": ""}
    inj = Injector(FakeClipboard(), FakeSender(), settings, lambda: False,
                   window_class=lambda: None, sleep=lambda _: None)

    res = inj.inject("hello")

    assert res.method == "fake" and res.chord == "ctrl+v"
    assert res.restored is True
