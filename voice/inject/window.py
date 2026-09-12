"""Which window has the keyboard, on the desktops that will answer.

The paste chord depends on it. A terminal pastes with Ctrl+Shift+V and does
nothing whatsoever with Ctrl+V, so a dictation into one, sent the wrong chord,
leaves the text on the clipboard and the window empty - while the injector,
which cannot see where its keystroke went, reports a successful paste and the
pill shows a checkmark. That is how a dictation comes to look lost.

Wayland has no portable way to ask. Each compositor answers - or does not - in
its own way, so this module holds one command per desktop that does, and an
honest "" for the ones that do not. `inject.active_window_command` overrides
everything here; this is only what to do when the owner has not said.
"""
from __future__ import annotations

import shutil
from typing import Callable, Mapping

#: Said wherever the reason has to be explained: doctor, the startup warning.
NO_WINDOW_ANSWER = "this desktop will not say which window has the keyboard"

#: NOT here, and never to be added: `org.kde.KWin.queryWindowInfo`.
#:
#: It looks perfect - it names the focused window, `resourceClass` is exactly
#: the form `inject.terminal_classes` uses, and qdbus ships with Plasma so it
#: would need nothing installed. It is an interactive window PICKER. KWin sets
#: a delayed reply and waits for the user to click a window; the cursor becomes
#: a crosshair. Measured on Plasma 6:
#:
#:     $ time timeout 5 qdbus6 org.kde.KWin /KWin org.kde.KWin.queryWindowInfo
#:     (no output)                        Executed in 5.01 secs   <- timed out
#:     $ ... and again, clicking a window
#:     resourceClass: org.kde.konsole     Executed in 3.26 secs   <- the click
#:
#: Used here it would put a grab in front of every paste, and because the
#: command would be a pipeline the timeout kills only the shell - `qdbus`
#: survives holding the grab until somebody clicks. One capture of its output
#: cannot tell an instant answer from a click, which is how it nearly shipped.
#: Hyprland prints a record; the class line is the second field of it.
_HYPRLAND = "hyprctl activewindow | awk '/^[[:space:]]*class:/{print $2; exit}'"

#: Sway's tree is JSON and needs a parser; `app_id` for Wayland clients,
#: `window_properties.class` for the XWayland ones. The trailing `// empty`
#: matters: a focused node with neither - an empty workspace - makes the
#: alternation yield JSON null, which `jq -r` prints as the four characters
#: "null". That is a non-empty class, so it is never a terminal, the chord is
#: chosen wrongly, and the log reports a window called "null" instead of
#: admitting it does not know.
_SWAY = ("swaymsg -t get_tree | jq -r "
         "'.. | select(.focused? == true) | .app_id // .window_properties.class // empty' "
         "| head -n1")


def is_plasma(env: Mapping[str, str]) -> bool:
    """Is this a KWin session? Then it can be asked in-process.

    Plasma is the one desktop that answers without a command at all - KWin runs
    a script for us and calls back over D-Bus, so there is no subprocess in the
    paste path and nothing to install. See `voice.inject.kwin`.
    """
    desktop = (env.get("XDG_CURRENT_DESKTOP", "") or "").lower()
    return "kde" in desktop or "plasma" in desktop


def default_window_command(env: Mapping[str, str],
                           which: Callable[[str], str | None] | None = None) -> str:
    """A shell command printing the focused window's class, or "" for none.

    "" is a real answer and the important one: it means this desktop cannot be
    asked, so the chord has to be chosen without knowing. Guessing a class
    would be worse than admitting that - a wrong class picks a wrong chord and
    the text silently goes nowhere.

    `which` is resolved here rather than defaulted in the signature, so that a
    test - or a caller with its own idea of what is installed - can replace it.
    """
    which = which or shutil.which
    desktop = (env.get("XDG_CURRENT_DESKTOP", "") or "").lower()
    if is_plasma(env):
        # No command at all: KWin is asked in-process instead, over D-Bus, by
        # `voice.inject.kwin`. That needs nothing installed, runs no subprocess
        # in the paste path, and is what `is_plasma` above is consulted for.
        return ""
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return _HYPRLAND if which("hyprctl") else ""
    if env.get("SWAYSOCK"):
        return _SWAY if which("swaymsg") and which("jq") else ""
    # GNOME lands here, and stays here: it exposes no focused-window API to an
    # ordinary client, and org.gnome.Shell.Eval is refused outside looking-glass.
    return ""


def effective_window_command(get: Callable[..., object], env: Mapping[str, str],
                             which: Callable[[str], str | None] | None = None) -> str:
    """`inject.active_window_command` if the owner set one, else the built-in.

    `get` is the config's own `get(key, default)`, so a hand-written command
    always wins: the built-in is a default, not an override, and somebody who
    has a working script for their compositor must not have it replaced.
    """
    configured = str(get("inject.active_window_command", "") or "").strip()
    return configured or default_window_command(env, which)


def terminal_chord_is_unreachable(window_command: str, paste_chord: str,
                                  terminal_chord: str,
                                  terminal_classes) -> tuple[bool, str]:
    """Is `terminal_chord` configured but impossible to ever reach?

    Two independent ways it can be. Nothing can tell us which window has the
    keyboard, so the class is never known; or the class can be read but there
    is nothing to recognise it against, because `inject.terminal_classes` is
    empty. Either way the chord actually sent is always the non-terminal one.

    This is not a failure the injector can detect for itself - the compositor
    accepts the keystroke whatever window has focus - so it is worth saying up
    front, once, rather than leaving every dictation into a terminal to
    disappear quietly.

    Note what is *not* checked: whether the owner's particular terminal is in
    the list. Ghostty, a renamed WezTerm and anything else unlisted still fall
    through silently, and only running the command and looking at the class it
    returns could catch that.
    """
    paste, terminal = paste_chord.strip().lower(), terminal_chord.strip().lower()
    if not terminal or paste == terminal:
        return False, ""              # one chord everywhere: nothing to reach
    if not window_command:
        return True, (f"{NO_WINDOW_ANSWER}, so every dictation is pasted with "
                      f"{paste} - a terminal only pastes with {terminal} and "
                      f"will silently discard it. Set "
                      f"inject.active_window_command, or set "
                      f"inject.paste_chord to {terminal} if you mostly dictate "
                      f"into a terminal.")
    if not list(terminal_classes or []):
        return True, (f"inject.terminal_classes is empty, so no window is ever "
                      f"recognised as a terminal and every dictation is pasted "
                      f"with {paste} - a terminal only pastes with {terminal} "
                      f"and will silently discard it.")
    return False, ""
