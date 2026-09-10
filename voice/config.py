"""TOML config with comment-preserving round trips (tomlkit)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from voice import paths
from voice.inject.keys import parse_chord

DEFAULT_CONFIG = '''# voice configuration. Edited by the settings window; hand edits are fine too.

[general]
language = "en"            # "en", "sv", or "auto"
notifications = true

[hotkeys]
dictate = "KEY_F13"        # any evdev key, or a combination like "KEY_LEFTMETA+KEY_SPACE"
dictate_mode = "hold"      # "hold" (push-to-talk) or "toggle"
recall = ""                # re-insert the last dictation
cancel = "KEY_ESC"         # discard the current recording

[audio]
device = ""                # PipeWire source node name; "" = default source
max_seconds = 120

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
paste_chord = "ctrl+v"
terminal_chord = "ctrl+shift+v"
terminal_classes = ["konsole", "org.kde.konsole", "kitty", "alacritty", "foot", "wezterm", "org.gnome.Ptyxis", "gnome-terminal"]
active_window_command = ""   # command printing the focused window class; "" = unknown
restore_clipboard = true
'''

_VALID_MODES = {"hold", "toggle"}
_VALID_BACKENDS = {"local", "openai_compatible"}


class Config:
    def __init__(self, path: Path, doc: tomlkit.TOMLDocument):
        self.path = path
        self._doc = doc

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
        self._doc = Config.load(self.path)._doc

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._doc
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return _plain(node)

    def set(self, dotted: str, value: Any) -> None:
        *parents, leaf = dotted.split(".")
        node: Any = self._doc
        for part in parents:
            if part not in node:
                node[part] = tomlkit.table()
            node = node[part]
        node[leaf] = value

    def save(self) -> None:
        self.path.write_text(tomlkit.dumps(self._doc))
        self.path.chmod(0o600)

    def stt_profile(self) -> tuple[str, dict]:
        name = self.get("stt.active", "local")
        profile = self.get(f"stt.profiles.{name}")
        if not isinstance(profile, dict):
            raise ValueError(f"stt.active refers to unknown profile '{name}'")
        return name, profile

    @staticmethod
    def secret(profile: dict) -> str | None:
        if profile.get("api_key"):
            return str(profile["api_key"])
        env = profile.get("api_key_env")
        return os.environ.get(env) if env else None

    def errors(self) -> list[str]:
        errs: list[str] = []
        mode = self.get("hotkeys.dictate_mode")
        if mode not in _VALID_MODES:
            errs.append(f"hotkeys.dictate_mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")
        if not self.get("hotkeys.dictate"):
            errs.append("hotkeys.dictate must not be empty")
        active = self.get("stt.active")
        profiles = self.get("stt.profiles", {}) or {}
        if active not in profiles:
            errs.append(f"stt.active '{active}' is not a defined profile")
        for name, prof in profiles.items():
            if prof.get("backend") not in _VALID_BACKENDS:
                errs.append(f"stt.profiles.{name}.backend must be one of {sorted(_VALID_BACKENDS)}")
        if not isinstance(self.get("audio.max_seconds"), int) or self.get("audio.max_seconds") <= 0:
            errs.append("audio.max_seconds must be a positive integer")
        for key in ("inject.paste_chord", "inject.terminal_chord"):
            chord = self.get(key)
            if chord is None:
                continue
            try:
                parse_chord(str(chord))
            except ValueError as exc:
                errs.append(f"{key}: {exc}")
        return errs


def _plain(node: Any) -> Any:
    """Convert tomlkit containers to plain dict/list so callers never see tomlkit types."""
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if hasattr(node, "unwrap"):
        return node.unwrap()
    return node
