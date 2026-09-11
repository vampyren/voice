"""The desktop's own store of global shortcuts, so an edited trigger can move.

`hotkeys.portal_*` is only what we *ask* the portal for on a genuine first run,
and on GNOME that never happens: `ListShortcuts` is scoped to the session we
have just created, so it can never say "never seen", and re-requesting a trigger
destroys the key the user assigned (see `voice.hotkey.portal_listener`). The
result was a settings field that looked editable and changed nothing.

The key does live somewhere, though - in GNOME's own dconf tree, under our app
id - and that is what this module writes, as the desktop's configuration tool
would. The rules it works by:

* only the shortcut ids the caller names, and only where the desktop already
  holds one. An id nobody registered is reported back, never invented, and an
  entry belonging to anything else is copied through byte for byte;
* a value that is missing, unreadable or not a shortcut list at all is refused
  out loud - a wrong write here silently unbinds every shortcut in the list;
* nothing is written when nothing would change.

Only GNOME keeps its shortcuts this way. On KDE (and anything else) there is no
such store and `desktop_shortcut_store` answers None, which means "leave the
desktop alone" - KDE's portal has a reconfigure dialog for exactly this.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable

from voice import APP_ID

log = logging.getLogger(__name__)

#: Where GNOME keeps the trigger, per app id. Verified on GNOME 49 and 50.
GNOME_KEY_TEMPLATE = "/org/gnome/settings-daemon/global-shortcuts/{app_id}/shortcuts"
#: dconf answers at once or not at all; this is only there to bound a hang.
DCONF_TIMEOUT_S = 5

#: Our trigger syntax to GTK's, in the order `gtk_accelerator_name` emits them.
MODIFIERS = {"SHIFT": "<Shift>", "CTRL": "<Control>", "CONTROL": "<Control>",
             "ALT": "<Alt>", "SUPER": "<Super>", "META": "<Super>", "LOGO": "<Super>"}
_MODIFIER_ORDER = ("<Shift>", "<Control>", "<Alt>", "<Super>")
#: X keysym names whose spelling is not simply the word the user typed.
_KEYSYMS = {"space": "space", "return": "Return", "enter": "Return", "tab": "Tab",
            "escape": "Escape", "esc": "Escape", "backspace": "BackSpace",
            "delete": "Delete", "del": "Delete", "insert": "Insert", "home": "Home",
            "end": "End", "pageup": "Page_Up", "page_up": "Page_Up",
            "pagedown": "Page_Down", "page_down": "Page_Down", "up": "Up",
            "down": "Down", "left": "Left", "right": "Right", "print": "Print",
            "menu": "Menu", "pause": "Pause"}
#: An empty GVariant array of strings needs its type: `<[]>` alone is not valid.
EMPTY_SHORTCUTS = "@as []"
#: The member of each entry's vardict that holds the key itself.
SHORTCUTS_MEMBER = "shortcuts"


class ShortcutStoreError(RuntimeError):
    """The desktop's store could not be read or written, with the reason why."""


def to_accelerator(trigger: str) -> str:
    """Our trigger syntax ("CTRL+space") as GTK's ("<Control>space").

    An empty trigger stays empty: that is "do not bind this", not a bad value.
    A chord with no key in it raises - the compositor would refuse it anyway,
    and silently writing a modifier-only accelerator unbinds the shortcut.
    """
    text = (trigger or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in text.split("+")]
    if any(not p for p in parts):
        raise ValueError(f"{trigger!r} is not a shortcut: it has an empty part")
    modifiers, keys = [], []
    for part in parts:
        modifier = MODIFIERS.get(part.upper())
        if modifier is not None:
            if modifier not in modifiers:
                modifiers.append(modifier)
        else:
            keys.append(part)
    if len(keys) != 1:
        raise ValueError(f"{trigger!r} needs exactly one key besides its modifiers"
                         if keys else f"{trigger!r} is only modifiers; add a key")
    ordered = [m for m in _MODIFIER_ORDER if m in modifiers]
    return "".join(ordered) + _keysym(keys[0])


def _keysym(key: str) -> str:
    """One key name in the spelling GTK parses back."""
    lowered = key.lower()
    if lowered in _KEYSYMS:
        return _KEYSYMS[lowered]
    if len(lowered) > 1 and lowered[0] == "f" and lowered[1:].isdigit():
        return "F" + lowered[1:]
    return lowered if len(key) == 1 else key


def desktop_shortcut_store(env: dict | None = None, app_id: str = APP_ID,
                           runner: Callable | None = None,
                           which: Callable[[str], str | None] | None = None):
    """The store this desktop keeps its global shortcuts in, or None.

    None is not a failure: it is "this desktop does not work that way", and the
    caller must then leave the desktop's own configuration alone.
    """
    import os

    environment = os.environ if env is None else env
    desktops = str(environment.get("XDG_CURRENT_DESKTOP", "") or "")
    if "gnome" not in desktops.lower():
        return None
    return GnomeShortcutStore(runner or subprocess.run, app_id=app_id,
                              which=which or shutil.which)


class GnomeShortcutStore:
    """GNOME's `global-shortcuts` dconf key for one app id."""

    name = "GNOME"

    def __init__(self, runner: Callable, app_id: str = APP_ID,
                 which: Callable[[str], str | None] = shutil.which):
        self._run = runner
        self._which = which
        self.app_id = app_id
        self.key = GNOME_KEY_TEMPLATE.format(app_id=app_id)

    # -- public ------------------------------------------------------------
    def write(self, triggers: dict[str, str]) -> str:
        """Put `{shortcut id: our trigger}` into the desktop's store.

        Returns one sentence for the user. Raises ShortcutStoreError, with the
        reason, rather than writing anything it is not sure of.
        """
        accelerators = self._converted(triggers)
        self._require_dconf()
        entries = _parse_entries(self._read(), self.key)
        known = {sid for sid, _ in entries}
        skipped = [sid for sid in accelerators if sid not in known]
        rebuilt, changed = [], []
        for sid, entry in entries:
            if sid not in accelerators:
                rebuilt.append(entry)            # not ours to touch
                continue
            replaced = _with_shortcut(entry, accelerators[sid], self.key)
            rebuilt.append(replaced)
            if replaced != entry:
                changed.append(sid)
        if not changed:
            return (f"{self.name} already holds "
                    f"{_spell(accelerators, skipped)}; nothing to change."
                    + _skipped_note(skipped))
        self._write_value("[" + ", ".join(rebuilt) + "]")
        log.info("wrote %s to %s", ", ".join(changed), self.key)
        return (f"Saved to {self.name}'s own shortcut store: "
                f"{_spell({sid: accelerators[sid] for sid in changed}, skipped)}."
                + _skipped_note(skipped))

    # -- dconf -------------------------------------------------------------
    def _converted(self, triggers: dict[str, str]) -> dict[str, str]:
        """Every trigger as an accelerator, or a refusal before anything is read."""
        out = {}
        for sid, trigger in triggers.items():
            try:
                out[sid] = to_accelerator(trigger)
            except ValueError as exc:
                raise ShortcutStoreError(f"{sid}: {exc}") from exc
        return out

    def _require_dconf(self) -> None:
        if not self._which("dconf"):
            raise ShortcutStoreError(
                "dconf is not installed, so this desktop's shortcut store cannot be "
                f"changed from here; set the key in GNOME's Keyboard settings instead")

    def _read(self) -> str:
        done = self._dconf(["dconf", "read", self.key])
        if done.returncode != 0:
            raise ShortcutStoreError(f"could not read {self.key}: "
                                     f"{(done.stderr or '').strip() or 'dconf failed'}")
        value = (done.stdout or "").strip()
        if not value:
            raise ShortcutStoreError(
                f"{self.key} is not stored yet: this desktop has never registered a "
                "shortcut for voice. Start voice, accept the desktop's permission "
                "dialog, then set the key here.")
        return value

    def _write_value(self, value: str) -> None:
        done = self._dconf(["dconf", "write", self.key, value])
        if done.returncode != 0:
            raise ShortcutStoreError(f"could not write {self.key}: "
                                     f"{(done.stderr or '').strip() or 'dconf failed'}")

    def _dconf(self, argv: list[str]):
        try:
            return self._run(argv, capture_output=True, text=True, timeout=DCONF_TIMEOUT_S)
        except Exception as exc:
            raise ShortcutStoreError(f"{argv[0]} failed: {exc}") from exc


def _spell(accelerators: dict[str, str], skipped: list[str]) -> str:
    return ", ".join(f"{sid} = {accel or 'no key'}"
                     for sid, accel in accelerators.items() if sid not in skipped)


def _skipped_note(skipped: list[str]) -> str:
    if not skipped:
        return ""
    return (f" ({', '.join(skipped)}: not registered with the desktop, so there is "
            "nothing to change - give it a trigger and restart voice)")


# -- the GVariant text dconf speaks ------------------------------------------
def _parse_entries(raw: str, key: str) -> list[tuple[str, str]]:
    """`[(id, entry source), ...]` from a stored `a(sa{sv})`.

    The entry is kept as *source*, not as a structure: an entry we are not
    changing has to go back exactly as it came, and re-serialising a parse of
    it is how a description or an option we have never heard of gets lost.
    """
    text = raw.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise ShortcutStoreError(_unreadable(key, raw))
    entries = []
    for part in _split_top_level(text[1:-1], key):
        entry = part.strip()
        if not entry:
            continue
        if not (entry.startswith("(") and entry.endswith(")")):
            raise ShortcutStoreError(_unreadable(key, raw))
        fields = _split_top_level(entry[1:-1], key)
        if len(fields) != 2:
            raise ShortcutStoreError(_unreadable(key, raw))
        entries.append((_unquote(fields[0].strip(), key, raw), entry))
    return entries


def _with_shortcut(entry: str, accelerator: str, key: str) -> str:
    """One entry with its `shortcuts` member replaced, everything else verbatim."""
    fields = _split_top_level(entry[1:-1], key)
    options = fields[1].strip()
    if not (options.startswith("{") and options.endswith("}")):
        raise ShortcutStoreError(_unreadable(key, entry))
    value = f"<['{_escape(accelerator)}']>" if accelerator else f"<{EMPTY_SHORTCUTS}>"
    members, replaced = [], False
    for member in _split_top_level(options[1:-1], key):
        member = member.strip()
        if not member:
            continue
        name, sep, _ = _split_member(member, key, entry)
        if _unquote(name.strip(), key, entry) == SHORTCUTS_MEMBER:
            members.append(f"{name.strip()}{sep} {value}")
            replaced = True
        else:
            members.append(member)
    if not replaced:
        members.insert(0, f"'{SHORTCUTS_MEMBER}': {value}")
    return f"({fields[0].strip()}, {{{', '.join(members)}}})"


def _split_member(member: str, key: str, source: str) -> tuple[str, str, str]:
    """`'shortcuts': <...>` split at its own colon, not one inside a value."""
    depth, quote, escaped = 0, "", False
    for index, char in enumerate(member):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth -= 1
        elif char == ":" and depth == 0:
            return member[:index], ":", member[index + 1:]
    raise ShortcutStoreError(_unreadable(key, source))


def _split_top_level(text: str, key: str) -> list[str]:
    """Split on commas that are not inside a bracket, a variant or a string."""
    parts, depth, quote, escaped, start = [], 0, "", False, 0
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth -= 1
            if depth < 0:
                raise ShortcutStoreError(_unreadable(key, text))
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    if depth or quote:
        raise ShortcutStoreError(_unreadable(key, text))
    parts.append(text[start:])
    return parts


def _unquote(text: str, key: str, source: str) -> str:
    if len(text) < 2 or text[0] not in "\"'" or text[-1] != text[0]:
        raise ShortcutStoreError(_unreadable(key, source))
    return text[1:-1].replace("\\\\", "\\").replace("\\" + text[0], text[0])


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _unreadable(key: str, raw: str) -> str:
    return (f"{key} does not hold a shortcut list this version understands, so it "
            f"was left alone rather than overwritten: {raw.strip()[:120]!r}")
