"""TOML config with comment-preserving round trips (tomlkit)."""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from voice import paths
from voice.inject.injector import PILL_FOCUS_CHOICES
from voice.inject.keys import parse_chord
from voice.ui.placement import (DEFAULT_MARGIN_X, DEFAULT_MARGIN_Y, DEFAULT_POSITION,
                                LEGACY_POSITIONS, MARGIN_LIMIT, POSITIONS, clamp_margin,
                                is_margin, normalise_position)

DEFAULT_CONFIG = '''# voice configuration. Edited by the settings window; hand edits are fine too.

[general]
language = "en"            # "en", "sv", or "auto"
languages = ["en", "sv"]   # cycle order for the language toggle
notifications = true

[general.language_profiles]
# Profile to switch to when a language is selected; add the local-swedish profile first.
# en = "local"
# sv = "local-swedish"

[hotkeys]
backend = "auto"           # "auto" | "evdev" (kernel devices) | "portal" (desktop shortcuts)
dictate = "KEY_F13"        # any evdev key, or a combination like "KEY_LEFTMETA+KEY_SPACE"
dictate_mode = "hold"      # "hold" (push-to-talk) or "toggle"
recall = ""                # re-insert the last dictation
cancel = "KEY_ESC"         # discard the current recording
language_toggle = ""       # cycle through general.languages
# Portal backend triggers (XDG shortcut syntax): a first-run preference, not a
# setting. Once your desktop knows a shortcut the key belongs to the desktop,
# and on GNOME that is true from the very first run - these are never applied
# there. Set the key in Settings -> Keyboard -> Keyboard Shortcuts; `voice
# status` and the settings window show what the desktop actually holds.
# Compositors reject bare modifiers, so a preference needs a combination.
# Empty = not bound.
portal_dictate = "CTRL+space"
portal_recall = ""
portal_cancel = ""
portal_language_toggle = ""

[audio]
device = ""                # PipeWire source node name; "" = default source
max_seconds = 120

[ui]
overlay = true             # the recording pill: waveform, timer, language badge
overlay_position = "bottom-center"   # top|middle|bottom with left|center|right,
                                     # e.g. "bottom-right"; needs gtk4-layer-shell
overlay_margin_x = 0       # pixels in from the anchored side; a "center" or
overlay_margin_y = 48      # "middle" half is centred and ignores its margin
overlay_pad_to_place = true      # where the desktop places the window itself (GNOME), make the
                                 # window bigger than the pill and draw the pill at the edge you
                                 # asked for, so overlay_position still moves it; false = a
                                 # pill-sized window wherever the compositor drops it
overlay_allow_fallback = false   # show the pill without gtk4-layer-shell, accepting
                                 # that it takes keyboard focus when it appears

[stt]
active = "local"           # name of a [stt.profiles.*] table

[stt.profiles.local]
backend = "local"
model = "large-v3-turbo"   # or "KBLab/kb-whisper-large" for Swedish
device = "cuda"            # falls back to cpu/int8 with a warning
compute_type = "float16"
beam_size = 5
prompt = ""                # vocabulary hint, e.g. "CachyOS, OBSBOT, Keychron"

[stt.profiles.openai]
backend = "openai_compatible"
base_url = "https://api.openai.com/v1"
model = "gpt-transcribe"
api_key_env = "OPENAI_API_KEY"
prompt = ""

[stt.profiles.groq]
backend = "openai_compatible"
base_url = "https://api.groq.com/openai/v1"
model = "whisper-large-v3-turbo"
api_key_env = "GROQ_API_KEY"
prompt = ""

[stt.profiles.openrouter]
backend = "openai_compatible"
base_url = "https://openrouter.ai/api/v1"
model = "openai/whisper-large-v3-turbo"
api_key_env = "OPENROUTER_API_KEY"
# OpenRouter ignores prompt; the backend drops it for this base_url

[dictionary]
# ordered [from, to] pairs, optional third element "icase" and/or "regex"
replacements = [
  ["cachy os", "CachyOS", "icase"],
  ["obs bot", "OBSBOT", "icase"],
]

[inject]
mode = "paste"             # "paste" sends the paste chord; "clipboard" only copies and
                           # tells you to press Ctrl+V yourself (remote desktops, and any
                           # compositor that refuses synthetic keystrokes)
paste_chord = "ctrl+v"
terminal_chord = "ctrl+shift+v"
terminal_classes = ["konsole", "org.kde.konsole", "kitty", "alacritty", "foot", "wezterm", "org.gnome.Ptyxis", "gnome-terminal"]
active_window_command = ""   # command printing the focused window class; "" = unknown
restore_clipboard = true
pill_focus = "hide"        # what to do when the recording pill can only be an ordinary
                           # window that takes keyboard focus (GNOME, where there is no
                           # layer-shell) and mode = "paste": "hide" takes the pill off
                           # screen for the chord and brings it straight back,
                           # "clipboard" does not paste at all and says so once,
                           # "paste" sends the chord anyway and hopes
pill_settle_ms = 150       # how long to let the compositor hand focus back after the
                           # pill is hidden, before the chord is sent
'''

#: Shipped portal triggers, also the fallback for a config.toml written before
#: the portal backend existed - such a file has no hotkeys.portal_* keys at all.
DEFAULT_PORTAL_TRIGGERS = {"dictate": "CTRL+space", "recall": "", "cancel": "",
                           "language_toggle": ""}

_VALID_MODES = {"hold", "toggle"}
#: `inject.mode`: send the paste chord, or leave the text on the clipboard and say so.
INJECT_MODES = ("paste", "clipboard")
#: `inject.pill_focus`: see PILL_FOCUS_CHOICES, and the README's GNOME section.
DEFAULT_PILL_SETTLE_MS = 150
_VALID_HOTKEY_BACKENDS = {"auto", "evdev", "portal"}
_VALID_BACKENDS = {"local", "openai_compatible"}


class Config:
    """A config file. Every accessor is serialised: the Qt thread, the IPC handler
    and the pipeline worker all reach the same instance."""

    def __init__(self, path: Path, doc: tomlkit.TOMLDocument):
        self.path = path
        self._doc = doc
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or paths.config_file()
        if not path.exists():
            path.write_text(DEFAULT_CONFIG)
            path.chmod(0o600)
        try:
            doc = tomlkit.parse(path.read_text())
        except TOMLKitError as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
        return cls(path, doc)

    def reload(self) -> None:
        doc = Config.load(self.path)._doc          # parse before taking the lock
        with self._lock:
            self._doc = doc

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._doc
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return _plain(node)

    def set(self, dotted: str, value: Any) -> None:
        with self._lock:
            *parents, leaf = dotted.split(".")
            node: Any = self._doc
            for part in parents:
                if part not in node:
                    node[part] = tomlkit.table()
                node = node[part]
            node[leaf] = value

    def save(self) -> None:
        """Write via a private temp file in the same directory, then os.replace.

        A crash or a full disk part-way through must never leave a truncated
        config.toml: the next start would refuse to parse it.
        """
        with self._lock:
            data = tomlkit.dumps(self._doc).encode()
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
            try:
                # Wrap the raw descriptor first: from here the `with` owns it on
                # every path, so nothing below can leak it for the daemon's life.
                with os.fdopen(fd, "wb") as fh:
                    os.fchmod(fh.fileno(), 0o600)
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def stt_profile(self) -> tuple[str, dict]:
        with self._lock:
            name = self.get("stt.active", "local")
            profile = self.get(f"stt.profiles.{name}")
            if not isinstance(profile, dict):
                raise ValueError(f"stt.active refers to unknown profile '{name}'")
            return name, profile

    def languages(self) -> list[str]:
        """The cycle order for the language toggle.

        A config written before the toggle existed has no list at all; the one
        language it does name is then the whole cycle, so a toggle bound in a
        newer build simply does nothing instead of jumping to a language the
        user never chose.
        """
        value = self.get("general.languages")
        if isinstance(value, list) and value:
            return [str(v).strip() for v in value if str(v).strip()]
        current = str(self.get("general.language", "en") or "en").strip()
        return [current] if current else []

    def language_profiles(self) -> dict[str, str]:
        """`general.language_profiles`, normalised to lower-case codes.

        The table is empty in a shipped config (its entries are comments), so
        an install that never opts in behaves exactly as it did before.
        """
        value = self.get("general.language_profiles", {}) or {}
        if not isinstance(value, dict):
            return {}
        return {str(code).strip().lower(): str(name).strip()
                for code, name in value.items() if str(name).strip()}

    def profile_for_language(self, code: str) -> str | None:
        """The profile a language selects, or None when it maps to nothing."""
        return self.language_profiles().get(str(code).strip().lower())

    def overlay_placement(self) -> tuple[str, int, int]:
        """Where the pill goes: (position, margin_x, margin_y), always usable.

        Normalised rather than raw, so every caller - the launcher, the helper,
        the settings dialog - reads the same placement out of a file that may
        hold one of the two older values, no keys at all, or (until the owner
        fixes what `errors()` told them) something that is not a placement.
        """
        with self._lock:
            return (normalise_position(self.get("ui.overlay_position", DEFAULT_POSITION)),
                    clamp_margin(self.get("ui.overlay_margin_x"), DEFAULT_MARGIN_X),
                    clamp_margin(self.get("ui.overlay_margin_y"), DEFAULT_MARGIN_Y))

    def portal_trigger(self, name: str) -> str:
        """The effective XDG trigger for a portal shortcut id.

        A missing key means the file predates this feature: fall back to the
        shipped default, or an upgraded install binds nothing and says nothing.
        An explicitly empty value is the user saying "do not bind this" and stays
        empty.
        """
        value = self.get(f"hotkeys.portal_{name}")
        if value is None:
            value = DEFAULT_PORTAL_TRIGGERS.get(name, "")
        return str(value).strip()

    @staticmethod
    def secret(profile: dict) -> str | None:
        if profile.get("api_key"):
            return str(profile["api_key"])
        env = profile.get("api_key_env")
        return os.environ.get(env) if env else None

    def errors(self) -> list[str]:
        with self._lock:
            return self._errors()

    def _errors(self) -> list[str]:
        errs: list[str] = []
        mode = self.get("hotkeys.dictate_mode")
        if mode not in _VALID_MODES:
            errs.append(f"hotkeys.dictate_mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")
        if not self.get("hotkeys.dictate"):
            errs.append("hotkeys.dictate must not be empty")
        backend = self.get("hotkeys.backend", "auto")
        if backend not in _VALID_HOTKEY_BACKENDS:
            errs.append(f"hotkeys.backend must be one of {sorted(_VALID_HOTKEY_BACKENDS)}, got {backend!r}")
        # "auto" resolves to portal on a machine without readable keyboards or a
        # local seat, so only an explicit "evdev" makes the trigger irrelevant.
        if backend != "evdev" and not self.portal_trigger("dictate"):
            errs.append("hotkeys.portal_dictate must not be empty unless hotkeys.backend is 'evdev'")
        active = self.get("stt.active")
        profiles = self.get("stt.profiles", {}) or {}
        if active not in profiles:
            errs.append(f"stt.active '{active}' is not a defined profile")
        for name, prof in profiles.items():
            if prof.get("backend") not in _VALID_BACKENDS:
                errs.append(f"stt.profiles.{name}.backend must be one of {sorted(_VALID_BACKENDS)}")
        errs += _language_errors("general.language", self.get("general.language"))
        languages = self.get("general.languages")
        if languages is not None:
            if not isinstance(languages, list) or not languages:
                errs.append(f"general.languages must be a non-empty list of language codes, got {languages!r}")
            else:
                for entry in languages:
                    errs += _language_errors("general.languages", entry)
        errs += self._language_profile_errors(profiles)
        if not isinstance(self.get("audio.max_seconds"), int) or self.get("audio.max_seconds") <= 0:
            errs.append("audio.max_seconds must be a positive integer")
        # Absent in a config written before this option existed: that file pastes,
        # exactly as it did then.
        inject_mode = self.get("inject.mode", "paste")
        if inject_mode not in INJECT_MODES:
            errs.append(f"inject.mode must be one of {sorted(INJECT_MODES)}, got {inject_mode!r}")
        pill_focus = self.get("inject.pill_focus", "hide")
        if pill_focus not in PILL_FOCUS_CHOICES:
            errs.append(f"inject.pill_focus must be one of {sorted(PILL_FOCUS_CHOICES)}, "
                        f"got {pill_focus!r}")
        settle = self.get("inject.pill_settle_ms", DEFAULT_PILL_SETTLE_MS)
        if not isinstance(settle, (int, float)) or isinstance(settle, bool) or settle < 0:
            errs.append("inject.pill_settle_ms must be a non-negative number of milliseconds, "
                        f"got {settle!r}")
        errs += self._placement_errors()
        for key in ("inject.paste_chord", "inject.terminal_chord"):
            chord = self.get(key)
            if chord is None:
                continue
            try:
                parse_chord(str(chord))
            except ValueError as exc:
                errs.append(f"{key}: {exc}")
        return errs

    def _placement_errors(self) -> list[str]:
        """Where the pill sits. Absent keys are a config written before the
        placement existed, and mean the default."""
        errs: list[str] = []
        position = self.get("ui.overlay_position")
        if position is not None and not (
                isinstance(position, str)
                and position.strip().lower() in set(POSITIONS) | set(LEGACY_POSITIONS)):
            errs.append(f"ui.overlay_position must be one of {sorted(POSITIONS)} "
                        f"(or the older {sorted(LEGACY_POSITIONS)}), got {position!r}")
        for key in ("ui.overlay_margin_x", "ui.overlay_margin_y"):
            margin = self.get(key)
            if margin is not None and not is_margin(margin):
                errs.append(f"{key} must be a whole number of pixels between "
                            f"-{MARGIN_LIMIT} and {MARGIN_LIMIT}, got {margin!r}")
        pad = self.get("ui.overlay_pad_to_place")
        if pad is not None and not isinstance(pad, bool):
            errs.append(f"ui.overlay_pad_to_place must be true or false, got {pad!r}")
        return errs

    def _language_profile_errors(self, profiles: dict) -> list[str]:
        """Every mapped profile must exist: switching language must never leave
        stt.active naming something the daemon cannot build."""
        mapping = self.get("general.language_profiles")
        if mapping is None:
            return []
        if not isinstance(mapping, dict):
            return [f"general.language_profiles must be a table of language = profile, got {mapping!r}"]
        errs: list[str] = []
        for code, name in mapping.items():
            if isinstance(name, str) and not name.strip():
                continue          # "leave the profile alone", same as no entry
            if not is_language_code(code):
                errs.append(f"general.language_profiles key {code!r} must be "
                            '"auto" or a two-letter code')
            if not isinstance(name, str) or name not in profiles:
                errs.append(f"general.language_profiles.{code} {name!r} is not a defined profile")
        return errs


def is_language_code(value: Any) -> bool:
    """A language setting is "auto" or a two-letter code; anything else is a typo."""
    return isinstance(value, str) and (
        value.lower() == "auto" or (len(value) == 2 and value.isalpha()))


def _language_errors(key: str, value: Any) -> list[str]:
    if is_language_code(value):
        return []
    return [f"{key} must be \"auto\" or a two-letter code, got {value!r}"]


def _plain(node: Any) -> Any:
    """Convert tomlkit containers to plain dict/list so callers never see tomlkit types."""
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if hasattr(node, "unwrap"):
        return node.unwrap()
    return node
