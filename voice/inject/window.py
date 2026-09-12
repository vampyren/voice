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

#: Plasma through KWin's own D-Bus interface, which needs nothing installed -
#: qdbus ships with Plasma, and `resourceClass` is exactly the form
#: `inject.terminal_classes` already lists.
#:
#: NOT a default, deliberately. KWin's `DBusInterface::queryWindowInfo()` sets
#: a delayed reply and calls `startInteractiveWindowSelection()` - the
#: crosshair-and-click flow behind "Detect Window Properties" in KWin Rules. If
#: that is what it does, running it before every paste puts an input grab in
#: front of the owner, and because the command is a pipeline the 1 s timeout
#: kills only `/bin/sh` while `qdbus` survives holding the grab until somebody
#: clicks or presses Escape. One capture of its output proves the shape of the
#: answer, not that it arrived without a click.
#:
#: To settle it, on a Plasma session, touching nothing while it runs:
#:     time timeout 5 qdbus6 org.kde.KWin /KWin org.kde.KWin.queryWindowInfo
#: Returning immediately makes this safe to promote to the default below.
KWIN_QUERY = ("{qdbus} org.kde.KWin /KWin org.kde.KWin.queryWindowInfo "
              "| sed -n 's/^resourceClass: //p' | head -n1")

#: The fallback for a Plasma that predates qdbus6, or where the owner already
#: has kdotool. Never required, only used if it happens to be there.
_KDOTOOL = "kdotool getactivewindow getwindowclassname"

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
    if "kde" in desktop or "plasma" in desktop:
        # `KWIN_QUERY` is deliberately not used here - see its comment. Until
        # it is shown to answer without a click, the honest answer on a Plasma
        # box with no kdotool is "cannot say", which costs the terminal chord
        # and nothing else: the transcript stays on the clipboard and the pill
        # says which key to press. A wrong guess here would cost a session.
        return _KDOTOOL if which("kdotool") else ""
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
