"""Which window has the keyboard, on the desktops that will answer.

The paste chord is chosen from the focused window's class: a terminal pastes
with Ctrl+Shift+V and does nothing at all with Ctrl+V. With no way to ask,
`voice` sent Ctrl+V into a terminal, the text stayed on the clipboard, and the
pill still showed a checkmark - which is how a dictation came to look lost.
"""
import pathlib

import pytest

from voice.inject.window import (NO_WINDOW_ANSWER, default_window_command,
                                 effective_window_command, terminal_chord_is_unreachable)


def env(**over):
    base = {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "XDG_SESSION_TYPE": "wayland"}
    base.update(over)
    return base


def has(*installed):
    return lambda binary: f"/usr/bin/{binary}" if binary in installed else None


def test_the_kwin_picker_is_never_chosen_automatically():
    """`org.kde.KWin.queryWindowInfo` is an interactive window picker.

    Measured on Plasma 6: untouched it times out with no output, and it only
    answers once a window is clicked. Running one before every paste would put
    a crosshair grab in front of the owner, so a Plasma box with qdbus and
    nothing else has to say it cannot tell.
    """
    for desktop in ("KDE", "plasma"):
        assert default_window_command(env(XDG_CURRENT_DESKTOP=desktop), has("qdbus6")) == ""
        assert default_window_command(env(XDG_CURRENT_DESKTOP=desktop), has("qdbus")) == ""


def test_nothing_offers_the_kwin_picker_as_a_command():
    """It must not come back as a copy-pasteable suggestion either."""
    import voice.inject.window as window

    values = [v for k, v in vars(window).items()
              if isinstance(v, str) and not k.startswith("__")]
    assert not [v for v in values if "queryWindowInfo" in v and "$" not in v], \
        "queryWindowInfo is a window picker; it cannot be a window command"


def test_kdotool_is_used_when_it_is_there():
    cmd = default_window_command(env(XDG_CURRENT_DESKTOP="KDE"), has("kdotool"))
    assert "kdotool" in cmd and "getactivewindow" in cmd


def test_kde_with_nothing_admits_it_cannot_ask():
    assert default_window_command(env(XDG_CURRENT_DESKTOP="KDE"), has()) == ""


def test_plasma_is_recognised_however_the_desktop_spells_itself():
    for spelling in ("KDE", "kde", "plasma", "X-Cinnamon:KDE", "KDE:wayland"):
        cmd = default_window_command(env(XDG_CURRENT_DESKTOP=spelling), has("kdotool"))
        assert "kdotool" in cmd, f"{spelling!r} was not recognised as Plasma"


def test_hyprland_is_asked_with_hyprctl():
    cmd = default_window_command(env(HYPRLAND_INSTANCE_SIGNATURE="abc"), has("hyprctl"))
    assert "hyprctl" in cmd and "activewindow" in cmd


def test_sway_is_asked_only_when_it_can_be_parsed():
    assert "swaymsg" in default_window_command(env(SWAYSOCK="/run/sway"), has("swaymsg", "jq"))
    assert default_window_command(env(SWAYSOCK="/run/sway"), has("swaymsg")) == ""


def test_gnome_has_no_answer_and_does_not_invent_one():
    # GNOME exposes no focused-window API to an ordinary client, and guessing
    # here would be worse than admitting it: a wrong class picks a wrong chord.
    assert default_window_command(env(), has("qdbus6", "kdotool", "hyprctl",
                                            "swaymsg", "jq")) == ""


def test_the_desktop_is_asked_before_the_session_type():
    # A Plasma X11 session answers just as well as a Wayland one.
    cmd = default_window_command(env(XDG_CURRENT_DESKTOP="KDE", XDG_SESSION_TYPE="x11"),
                                 has("kdotool"))
    assert "kdotool" in cmd


# -- telling the owner why their terminal never receives anything ------------

def test_an_unreachable_terminal_chord_is_reported():
    unreachable, why = terminal_chord_is_unreachable(
        window_command="", paste_chord="ctrl+v", terminal_chord="ctrl+shift+v",
        terminal_classes=["konsole"])
    assert unreachable
    assert "ctrl+shift+v" in why and "ctrl+v" in why
    assert NO_WINDOW_ANSWER in why


def test_nothing_is_reported_once_the_window_can_be_asked():
    unreachable, _ = terminal_chord_is_unreachable(
        window_command="kdotool getactivewindow getwindowclassname",
        paste_chord="ctrl+v", terminal_chord="ctrl+shift+v",
        terminal_classes=["konsole"])
    assert not unreachable


def test_a_window_that_can_be_asked_but_no_list_to_match_it_against_is_reported():
    # Being able to read the class buys nothing if nothing is ever recognised
    # as a terminal: the terminal chord is still unreachable, and the owner
    # gets a green doctor while every terminal paste goes nowhere.
    unreachable, why = terminal_chord_is_unreachable(
        window_command="kdotool getactivewindow getwindowclassname",
        paste_chord="ctrl+v", terminal_chord="ctrl+shift+v", terminal_classes=[])
    assert unreachable and "terminal_classes" in why


def test_nothing_is_reported_when_both_chords_are_the_same():
    # The owner has already settled it: one chord for every window.
    unreachable, _ = terminal_chord_is_unreachable(
        window_command="", paste_chord="ctrl+shift+v", terminal_chord="ctrl+shift+v",
        terminal_classes=["konsole"])
    assert not unreachable


@pytest.mark.parametrize("paste, terminal", [("CTRL+V", "ctrl+shift+v"),
                                             (" ctrl+v ", "ctrl+shift+v")])
def test_the_comparison_survives_however_the_chords_were_typed(paste, terminal):
    unreachable, _ = terminal_chord_is_unreachable(
        window_command="", paste_chord=paste, terminal_chord=terminal,
        terminal_classes=["konsole"])
    assert unreachable


# -- what the daemon actually runs -------------------------------------------

def test_the_owners_own_command_beats_the_built_in_one():
    cfg = {"inject.active_window_command": "my-own-script --class"}
    cmd = effective_window_command(cfg.get, env(XDG_CURRENT_DESKTOP="KDE"), has("kdotool"))
    assert cmd == "my-own-script --class"


def test_the_built_in_command_fills_in_when_the_owner_said_nothing():
    cfg = {"inject.active_window_command": ""}
    cmd = effective_window_command(cfg.get, env(XDG_CURRENT_DESKTOP="KDE"), has("kdotool"))
    assert "kdotool" in cmd


def test_whitespace_is_not_a_configured_command():
    cfg = {"inject.active_window_command": "   "}
    cmd = effective_window_command(cfg.get, env(XDG_CURRENT_DESKTOP="KDE"), has("kdotool"))
    assert "kdotool" in cmd


def test_nothing_configured_and_nothing_built_in_is_still_empty():
    cfg = {}
    assert effective_window_command(cfg.get, env(), has()) == ""


# -- the sway command, run against the real jq -------------------------------

import shutil as _shutil
import subprocess

SWAY_TREE_WITH_APP_ID = '{"nodes":[{"focused":true,"app_id":"foot","window_properties":null}]}'
SWAY_TREE_XWAYLAND = ('{"nodes":[{"focused":true,"app_id":null,'
                      '"window_properties":{"class":"Alacritty"}}]}')
#: A focused node that is neither - an empty workspace. `.a // .b` yields JSON
#: null here, which `jq -r` prints as the four characters "null": a non-empty
#: class that is not a terminal, so the chord is wrong and the log claims the
#: paste was aimed at a window called "null".
SWAY_TREE_NOTHING_FOCUSED = '{"nodes":[{"focused":true,"app_id":null,"window_properties":null}]}'


def _run_sway_command(tree):
    from voice.inject.window import _SWAY
    done = subprocess.run(_SWAY.replace("swaymsg -t get_tree", f"printf %s '{tree}'"),
                          shell=True, capture_output=True, text=True, timeout=5)
    return done.stdout.strip()


@pytest.mark.skipif(not _shutil.which("jq"), reason="needs the real jq")
def test_the_sway_command_reads_a_wayland_client():
    assert _run_sway_command(SWAY_TREE_WITH_APP_ID) == "foot"


@pytest.mark.skipif(not _shutil.which("jq"), reason="needs the real jq")
def test_the_sway_command_reads_an_xwayland_client():
    assert _run_sway_command(SWAY_TREE_XWAYLAND) == "Alacritty"


@pytest.mark.skipif(not _shutil.which("jq"), reason="needs the real jq")
def test_the_sway_command_says_nothing_rather_than_the_word_null():
    assert _run_sway_command(SWAY_TREE_NOTHING_FOCUSED) == "", \
        'an unnamed focused node must read as unknown, not as a window called "null"'
