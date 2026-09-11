"""The desktop's own shortcut store, which is where the key really lives.

Every test drives a fake runner: nothing here may reach the real `dconf`, which
is the owner's live desktop configuration.
"""
import pytest

from voice.hotkey.desktop_shortcuts import (GNOME_KEY_TEMPLATE, GnomeShortcutStore,
                                            ShortcutStoreError, desktop_shortcut_store,
                                            to_accelerator)

APP_ID = "io.github.vampyren.voice"
KEY = GNOME_KEY_TEMPLATE.format(app_id=APP_ID)

STORED = ("[('dictate', {'shortcuts': <['<Control>space']>, 'description': "
          "<'Voice dictation'>}), ('recall', {'shortcuts': <['F14']>, 'description': "
          "<'Re-insert the last dictation'>})]")


class FakeRunner:
    """`subprocess.run` for dconf, with a scripted read and a recorded write."""

    def __init__(self, stored: str = STORED, read_code: int = 0, write_code: int = 0):
        self.stored = stored
        self.read_code, self.write_code = read_code, write_code
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["dconf", "read"]:
            return _Done(self.read_code, self.stored + "\n")
        if argv[:2] == ["dconf", "write"]:
            if self.write_code == 0:
                self.stored = argv[3]
            return _Done(self.write_code, "", "dconf: write refused")
        raise AssertionError(f"unexpected command {argv}")


class _Done:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def store(runner=None, **kwargs) -> GnomeShortcutStore:
    return GnomeShortcutStore(runner or FakeRunner(**kwargs), app_id=APP_ID,
                              which=lambda name: f"/usr/bin/{name}")


# -- our syntax into the desktop's ------------------------------------------
@pytest.mark.parametrize("ours,theirs", [
    ("CTRL+space", "<Control>space"),
    ("F13", "F13"),
    ("f13", "F13"),
    ("CTRL+SHIFT+l", "<Shift><Control>l"),
    ("SUPER+d", "<Super>d"),
    ("ALT+F4", "<Alt>F4"),
    ("ctrl+alt+delete", "<Control><Alt>Delete"),
    ("", ""),                                   # "not bound", not a bad trigger
])
def test_a_trigger_becomes_a_gtk_accelerator(ours, theirs):
    assert to_accelerator(ours) == theirs


@pytest.mark.parametrize("bad", ["CTRL", "CTRL+SHIFT", "+", "CTRL+"])
def test_a_chord_with_no_key_in_it_is_refused(bad):
    with pytest.raises(ValueError):
        to_accelerator(bad)


#: Every key the settings dialog can hand us in Qt's own spelling
#: (`QKeySequence(key).toString()`), with the keysym name GTK parses back.
#: Measured, both halves: the left column is what PySide6 emits, the right one
#: is what `Gtk.accelerator_parse` accepts - `Ins`, `PgUp`, `PgDown`, `Del`,
#: `Esc`, `Space` and `Backspace` are all refused by GTK as they stand.
@pytest.mark.parametrize("qt,keysym", [
    ("Ins", "Insert"), ("PgUp", "Page_Up"), ("PgDown", "Page_Down"),
    ("Del", "Delete"), ("Esc", "Escape"), ("Space", "space"),
    ("Backspace", "BackSpace"), ("Enter", "Return"), ("Backtab", "ISO_Left_Tab"),
    ("SysReq", "Sys_Req"),
    # And the ones Qt already spells the way GTK wants, which must not move.
    ("Return", "Return"), ("Tab", "Tab"), ("Home", "Home"), ("End", "End"),
    ("Up", "Up"), ("Down", "Down"), ("Left", "Left"), ("Right", "Right"),
    ("Print", "Print"), ("Menu", "Menu"), ("Pause", "Pause"), ("Clear", "Clear"),
    ("Help", "Help"), ("Cancel", "Cancel"), ("F13", "F13"), ("A", "a"), ("1", "1"),
])
def test_qts_own_spelling_of_a_key_becomes_one_gtk_can_parse(qt, keysym):
    assert to_accelerator(f"CTRL+{qt}") == f"<Control>{keysym}"


#: Every ASCII punctuation key, in both the spellings that reach us: the bare
#: character, which is what Qt hands the dialog, and the X keysym name, which
#: is what a person types into the Advanced fields and what GTK actually
#: parses. Measured against GTK 4 on this machine - `<Control>comma` is
#: (True, 44, CONTROL_MASK) and `<Control>,` is (False, 0, 0) - so the
#: character has to become the name before anything is written.
@pytest.mark.parametrize("ours,theirs", [
    ("CTRL+comma", "<Control>comma"), ("CTRL+,", "<Control>comma"),
    ("CTRL+period", "<Control>period"), ("CTRL+.", "<Control>period"),
    ("SUPER+bracketleft", "<Super>bracketleft"), ("SUPER+[", "<Super>bracketleft"),
    ("CTRL+minus", "<Control>minus"), ("CTRL+-", "<Control>minus"),
    ("CTRL+plus", "<Control>plus"),
    ("CTRL+slash", "<Control>slash"), ("CTRL+/", "<Control>slash"),
    ("CTRL+SHIFT+equal", "<Shift><Control>equal"),
    ("ALT+grave", "<Alt>grave"), ("ALT+`", "<Alt>grave"),
    ("CTRL+backslash", "<Control>backslash"),
])
def test_a_punctuation_key_reaches_the_desktop_as_the_keysym_gtk_parses(ours, theirs):
    """Ctrl+, worked before this branch and has to keep working: the whitelist
    that replaced the pass-through refused every punctuation key there is."""
    assert to_accelerator(ours) == theirs


#: Not a keysym and not spellable as one, so the write would go through and the
#: shortcut would never fire. F36 is past the last function key there is, and
#: "Volume" is half of Qt's "Volume Up", which is not a keysym name at all.
@pytest.mark.parametrize("bad", ["CTRL+banana", "CTRL+F36", "CTRL+Volume",
                                 "CTRL+Page Up"])
def test_a_key_that_cannot_be_spelled_for_the_desktop_is_refused(bad):
    """A write that cannot work has to report failure, not success: the dialog
    said "Your desktop now uses Ctrl+Ins" over an accelerator GTK drops."""
    with pytest.raises(ValueError):
        to_accelerator(bad)


def test_the_media_keys_are_still_passed_through():
    """The XF86* family is what a media key really is called, GTK parses it,
    and it can only ever be typed by hand - so it must survive the refusal."""
    assert to_accelerator("SUPER+XF86AudioPlay") == "<Super>XF86AudioPlay"


def test_a_key_that_cannot_be_spelled_is_refused_before_anything_is_written():
    runner = FakeRunner()
    with pytest.raises(ShortcutStoreError) as exc:
        store(runner).write({"dictate": "CTRL+banana"})
    assert runner.calls == []
    assert "dictate" in str(exc.value)


def test_one_unusable_trigger_fails_that_key_and_not_the_whole_write():
    """A legacy field nobody can spell used to poison the entire save: one
    `hotkeys.portal_cancel` this version cannot spell and no key in the window
    could be changed at all, with no way to find out which field was at fault."""
    runner = FakeRunner()
    desktop = store(runner)
    message = desktop.write({"dictate": "CTRL+ALT+d", "recall": "CTRL+banana"})
    written = runner.calls[1][3]
    assert "<Alt><Control>d" in written or "<Control><Alt>d" in written
    assert "<['F14']>" in written                 # recall left exactly as it was
    assert "recall" in message and "banana" in message
    assert set(desktop.refused) == {"recall"}
    assert "recall" in desktop.refused["recall"]


# -- which desktop this is ---------------------------------------------------
def test_gnome_gets_a_store_and_other_desktops_get_none():
    gnome = desktop_shortcut_store(env={"XDG_CURRENT_DESKTOP": "GNOME"}, app_id=APP_ID)
    assert isinstance(gnome, GnomeShortcutStore)
    assert gnome.key == KEY
    assert desktop_shortcut_store(env={"XDG_CURRENT_DESKTOP": "KDE"}, app_id=APP_ID) is None
    assert desktop_shortcut_store(env={}, app_id=APP_ID) is None
    assert desktop_shortcut_store(env={"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"},
                                  app_id=APP_ID) is not None


# -- writing it --------------------------------------------------------------
def test_writing_a_trigger_replaces_only_that_id():
    runner = FakeRunner()
    message = store(runner).write({"dictate": "CTRL+ALT+d"})
    assert runner.calls[0][:2] == ["dconf", "read"]
    assert runner.calls[1][:3] == ["dconf", "write", KEY]
    written = runner.calls[1][3]
    assert "<Alt><Control>d" in written or "<Control><Alt>d" in written
    # The id we did not touch is still there, key and description intact.
    assert "('recall', {'shortcuts': <['F14']>, 'description': " \
           "<'Re-insert the last dictation'>})" in written
    assert "'description': <'Voice dictation'>" in written      # ours kept its own
    assert "dictate" in message and "GNOME" in message


def test_an_id_we_do_not_own_is_never_touched():
    """The list is keyed by our app id, but it may hold ids from another build."""
    stored = ("[('dictate', {'shortcuts': <['F13']>}), "
              "('somebody-elses', {'shortcuts': <['<Super>k']>, 'description': <'Not ours'>})]")
    runner = FakeRunner(stored)
    store(runner).write({"dictate": "F14"})
    written = runner.calls[1][3]
    assert "('somebody-elses', {'shortcuts': <['<Super>k']>, 'description': <'Not ours'>})" in written
    assert "<['F14']>" in written


def test_an_empty_trigger_clears_the_key_rather_than_inventing_one():
    runner = FakeRunner()
    store(runner).write({"recall": ""})
    written = runner.calls[1][3]
    assert "('recall', {'shortcuts': <@as []>" in written
    assert "<['<Control>space']>" in written               # dictate untouched


def test_an_id_the_desktop_does_not_know_is_reported_not_invented():
    runner = FakeRunner()
    message = store(runner).write({"dictate": "F13", "cancel": "CTRL+ESCAPE"})
    written = runner.calls[1][3]
    assert "cancel" not in written
    assert "cancel" in message and "not registered" in message


def test_writing_nothing_at_all_does_not_write_at_all():
    """Saving without changing a trigger must not rewrite the desktop's store."""
    runner = FakeRunner()
    message = store(runner).write({"dictate": "CTRL+space"})
    assert [c[:2] for c in runner.calls] == [["dconf", "read"]]
    assert "already" in message


# -- refusing, out loud ------------------------------------------------------
def test_a_missing_store_is_refused_with_an_explanation():
    with pytest.raises(ShortcutStoreError) as exc:
        store(FakeRunner(stored="")).write({"dictate": "F13"})
    assert "not stored" in str(exc.value) or "no shortcuts" in str(exc.value)
    assert KEY in str(exc.value)


def test_a_value_we_cannot_parse_is_refused_rather_than_overwritten():
    runner = FakeRunner(stored="not a shortcut list at all")
    with pytest.raises(ShortcutStoreError) as exc:
        store(runner).write({"dictate": "F13"})
    assert [c[:2] for c in runner.calls] == [["dconf", "read"]]     # nothing was written
    assert KEY in str(exc.value)


def test_a_failing_read_is_refused():
    with pytest.raises(ShortcutStoreError):
        store(FakeRunner(read_code=1)).write({"dictate": "F13"})


def test_a_failing_write_says_what_dconf_said():
    runner = FakeRunner(write_code=1)
    with pytest.raises(ShortcutStoreError) as exc:
        store(runner).write({"dictate": "F13"})
    assert "refused" in str(exc.value)


def test_without_dconf_installed_nothing_is_attempted():
    runner = FakeRunner()
    empty = GnomeShortcutStore(runner, app_id=APP_ID, which=lambda name: None)
    with pytest.raises(ShortcutStoreError) as exc:
        empty.write({"dictate": "F13"})
    assert runner.calls == []
    assert "dconf" in str(exc.value)


def test_a_trigger_that_is_not_a_chord_is_refused_before_anything_is_written():
    runner = FakeRunner()
    with pytest.raises(ShortcutStoreError):
        store(runner).write({"dictate": "CTRL"})
    assert runner.calls == []


def test_a_runner_that_blows_up_becomes_a_refusal_not_a_traceback():
    def boom(argv, **kwargs):
        raise OSError("no dconf here")

    with pytest.raises(ShortcutStoreError) as exc:
        store(boom).write({"dictate": "F13"})
    assert "no dconf here" in str(exc.value)
