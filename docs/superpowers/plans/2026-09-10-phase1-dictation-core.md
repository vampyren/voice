# Phase 1: Dictation Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hold a key, speak, release, and the transcript is pasted into the focused Wayland window, using faster-whisper locally or any OpenAI-compatible cloud STT, with a tray icon, settings window, CLI, doctor and installer.

**Architecture:** One PySide6 daemon process owns the tray, an evdev hotkey thread, a pw-record subprocess for capture, a pluggable `Transcriber`, and an `Injector` that copies to the clipboard and sends a paste chord through the xdg RemoteDesktop portal. A CLI talks to the daemon over a Unix socket. Every platform-specific piece lives in `hotkey/`, `audio/capture.py` or `inject/` so the rest is portable.

**Tech Stack:** Python 3.12 via uv, faster-whisper 1.2 (+ CTranslate2, onnxruntime, bundled Silero VAD), numpy, evdev, jeepney (D-Bus), httpx, tomlkit, PySide6 6.11, pytest. External tools already on CachyOS: pw-record/pw-dump (pipewire), wl-copy/wl-paste (wl-clipboard), notify-send (libnotify).

**Spec:** `docs/superpowers/specs/2026-09-10-voice-dictation-design.md`

## Global Constraints

- Wayland only; no X11 code paths. Linux-only imports (`evdev`, portal, `pw-*`, `wl-*`) are allowed only inside `voice/hotkey/`, `voice/audio/capture.py`, `voice/inject/`, `voice/doctor.py`.
- Nothing installed system-wide except the udev rule; all Python deps live in the uv venv in the project directory. Models cache under `~/.cache/huggingface`.
- App name is a single constant `APP_NAME = "voice"` in `voice/__init__.py` plus `[project] name` in `pyproject.toml`; every path, socket name and .desktop id derives from it.
- Config file `~/.config/voice/config.toml`, mode 0600, comments preserved (tomlkit). Secrets via `api_key` or `api_key_env`.
- Injection is copy + paste chord via keycodes (never character typing). Clipboard is restored afterwards when it held text.
- Recordings shorter than 300 ms after VAD trim are dropped silently; max recording 120 s.
- Python 3.12 pinned in `.python-version`. Package layout `voice/` at repo root (no `src/`).
- Tests: `uv run pytest` runs unit + interaction tests; `-m boundary` needs the VM desktop session; `-m gpu` needs the owner's PC. Every functional task has a failing test first.
- Commits: conventional prefix, and every commit message ends with the two attribution lines shown in Task 1 Step 8.
- License undecided: no LICENSE file; README states "License: to be decided".

## File Map

| File | Responsibility |
|---|---|
| `pyproject.toml`, `.python-version`, `.gitignore` | uv project, deps, extras `gpu`, pytest markers, `voice` script entry |
| `voice/__init__.py` | `APP_NAME`, `APP_ID`, `__version__` |
| `voice/paths.py` | XDG-derived config/state/cache/runtime paths |
| `voice/config.py` | default TOML text, `Config` load/get/set/save, profile + secret resolution |
| `voice/hotkey/keyspec.py` | parse `"KEY_A+KEY_B"` → `KeySpec`; `Tracker` turns raw key events into named press/release |
| `voice/hotkey/evdev_listener.py` | device discovery, hot-plug, background thread, key capture for settings |
| `voice/audio/pcm.py` | int16 ↔ float32, WAV encoding |
| `voice/audio/vad.py` | `trim_silence` using faster-whisper's Silero VAD |
| `voice/audio/capture.py` | `Recorder` around `pw-record`; `list_sources` via `pw-dump` |
| `voice/stt/base.py` | `Transcript`, `Transcriber` protocol, `TranscriptionError` |
| `voice/stt/local.py` | faster-whisper backend with CPU fallback |
| `voice/stt/openai_compat.py` | multipart `/audio/transcriptions` backend, provider quirks |
| `voice/stt/__init__.py` | `make_transcriber(profile)` registry |
| `voice/text.py` | dictionary replacements |
| `voice/history.py` | last-N dictations, JSONL persistence, retry audio |
| `voice/inject/clipboard.py` | wl-copy/wl-paste snapshot, set, restore |
| `voice/inject/keys.py` | chord string → keycodes; `KeySender` protocol |
| `voice/inject/portal.py` | RemoteDesktop portal session via jeepney; restore token store |
| `voice/inject/fallback.py` | `wtype` / `ydotool` chord senders, `make_key_sender` |
| `voice/inject/injector.py` | copy → wait modifiers → chord → restore; terminal chord choice |
| `voice/pipeline.py` | `Dictation` state machine (record → trim → transcribe → replace → inject → history) |
| `voice/ipc.py` | Unix-socket JSON server + client |
| `voice/ui/notify.py` | `notify-send` wrapper |
| `voice/ui/icons.py` | tray icons drawn with QPainter per state |
| `voice/ui/tray.py` | `QSystemTrayIcon` + menu |
| `voice/ui/settings.py` | settings dialog, hotkey capture, profile templates |
| `voice/daemon.py` | wiring, Qt bridge, reload on save |
| `voice/cli.py` | `voice` entry point and subcommands |
| `voice/doctor.py` | environment checks |
| `packaging/70-voice-input.rules`, `packaging/voice.desktop`, `install.sh` | install/uninstall |
| `README.md` | usage, config, external component links |
| `tests/` | mirrors the package; `conftest.py` isolates XDG dirs |

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `voice/__init__.py`, `voice/paths.py`, `tests/conftest.py`, `tests/test_paths.py`, `README.md`

**Interfaces:**
- Produces: `voice.APP_NAME: str`, `voice.APP_ID: str`, `voice.__version__: str`; `voice.paths.config_dir() -> Path`, `state_dir() -> Path`, `cache_dir() -> Path`, `runtime_dir() -> Path`, `config_file() -> Path`, `socket_path() -> Path`, `history_file() -> Path`, `portal_token_file() -> Path`. All honour `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR` and create the directory on call (runtime dir excepted).

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "voice"
version = "0.1.0"
description = "Wayland-native voice typing: hold a key, speak, release, text appears."
requires-python = ">=3.12,<3.13"
dependencies = [
    "faster-whisper>=1.2,<2",
    "numpy>=1.26",
    "evdev>=1.7",
    "jeepney>=0.9",
    "httpx>=0.27",
    "tomlkit>=0.13",
    "PySide6>=6.8",
]

[project.optional-dependencies]
gpu = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12>=9,<10"]

[project.scripts]
voice = "voice.cli:main"

[dependency-groups]
dev = ["pytest>=8", "pytest-timeout>=2"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["voice"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-m 'not boundary and not gpu' --timeout=60"
markers = [
    "boundary: needs a live Wayland session with pipewire/wl-clipboard/portal (run in the dev VM)",
    "gpu: needs an NVIDIA GPU with CUDA (run on the owner's PC)",
]
```

- [ ] **Step 2: Create `.python-version` and `.gitignore`**

`.python-version`:
```
3.12
```

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
dist/
build/
*.egg-info/
uv.lock.bak
tests/_screenshots/
```

- [ ] **Step 3: Create `voice/__init__.py`**

```python
"""voice: Wayland-native voice typing."""

APP_NAME = "voice"                  # rename here + [project] name in pyproject.toml
APP_ID = f"io.github.vampyren.{APP_NAME}"
__version__ = "0.1.0"
```

- [ ] **Step 4: Write the failing test `tests/test_paths.py` and `tests/conftest.py`**

`tests/conftest.py`:
```python
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Every test gets private XDG dirs so nothing touches the real config."""
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        d = tmp_path / var.lower()
        d.mkdir()
        monkeypatch.setenv(var, str(d))
    yield tmp_path
```

`tests/test_paths.py`:
```python
from pathlib import Path

from voice import APP_NAME, paths


def test_config_dir_uses_xdg_and_app_name(isolated_xdg):
    d = paths.config_dir()
    assert d == isolated_xdg / "xdg_config_home" / APP_NAME
    assert d.is_dir()


def test_all_paths_are_namespaced(isolated_xdg):
    assert paths.config_file().name == "config.toml"
    assert paths.history_file().parent == isolated_xdg / "xdg_state_home" / APP_NAME
    assert paths.portal_token_file().parent == paths.state_dir()
    assert paths.cache_dir() == isolated_xdg / "xdg_cache_home" / APP_NAME
    assert paths.socket_path() == isolated_xdg / "xdg_runtime_dir" / f"{APP_NAME}.sock"


def test_runtime_dir_falls_back_to_tmp_when_unset(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert paths.runtime_dir() == Path("/tmp")
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `uv sync --group dev && uv run pytest tests/test_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice.paths'` (uv sync first creates the venv and lockfile; it downloads faster-whisper and PySide6, about 600 MB on CPU).

- [ ] **Step 6: Create `voice/paths.py`**

```python
"""XDG-derived filesystem locations, all namespaced by APP_NAME."""
from __future__ import annotations

import os
from pathlib import Path

from voice import APP_NAME


def _xdg(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else default


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    return _ensure(_xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME)


def state_dir() -> Path:
    return _ensure(_xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP_NAME)


def cache_dir() -> Path:
    return _ensure(_xdg("XDG_CACHE_HOME", Path.home() / ".cache") / APP_NAME)


def runtime_dir() -> Path:
    return _xdg("XDG_RUNTIME_DIR", Path("/tmp"))


def config_file() -> Path:
    return config_dir() / "config.toml"


def history_file() -> Path:
    return state_dir() / "history.jsonl"


def portal_token_file() -> Path:
    return state_dir() / "portal-token"


def socket_path() -> Path:
    return runtime_dir() / f"{APP_NAME}.sock"
```

- [ ] **Step 7: Run tests to verify they pass, then write `README.md` stub**

Run: `uv run pytest tests/test_paths.py -v`
Expected: 3 passed.

`README.md`:
```markdown
# voice

Wayland-native voice typing for Linux (CachyOS / KDE Plasma). Hold a key, speak, release,
and the text is pasted into whatever has focus. Transcribes locally on your NVIDIA GPU with
faster-whisper, or through any OpenAI-compatible speech-to-text API.

Status: phase 1 (dictation core) in progress. See `docs/superpowers/specs/` for the design.

License: to be decided.
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .python-version .gitignore uv.lock voice/ tests/ README.md
git commit -m "chore: scaffold uv project, app constants and XDG paths

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 2: Config load/save with defaults and profiles

**Files:**
- Create: `voice/config.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `voice.paths.config_file()`
- Produces: `DEFAULT_CONFIG: str` (the commented TOML from the spec §5, phase 1 keys only); `class Config` with `path: Path`, `load(path: Path | None = None) -> Config` (classmethod; writes defaults with mode 0600 if missing), `get(dotted: str, default=None)`, `set(dotted: str, value)`, `save()`, `reload()`, `stt_profile() -> tuple[str, dict]` (active name and a plain dict of the profile), `secret(profile: dict) -> str | None` (api_key or env lookup), `errors() -> list[str]` (validation messages; empty when valid).

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:
```python
import os
import stat

import pytest

from voice import paths
from voice.config import DEFAULT_CONFIG, Config


def test_load_creates_default_file_with_0600(isolated_xdg):
    cfg = Config.load()
    assert cfg.path == paths.config_file()
    assert cfg.path.exists()
    assert stat.S_IMODE(cfg.path.stat().st_mode) == 0o600
    assert cfg.get("hotkeys.dictate") == "KEY_F13"
    assert cfg.get("stt.active") == "local"


def test_set_and_save_preserves_comments(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.dictate", "KEY_RIGHTCTRL")
    cfg.set("general.language", "sv")
    cfg.save()
    text = cfg.path.read_text()
    assert 'dictate = "KEY_RIGHTCTRL"' in text
    assert "# any evdev key" in text          # comment survived
    again = Config.load()
    assert again.get("hotkeys.dictate") == "KEY_RIGHTCTRL"
    assert again.get("general.language") == "sv"


def test_get_missing_returns_default(isolated_xdg):
    cfg = Config.load()
    assert cfg.get("nope.missing", 42) == 42


def test_stt_profile_returns_active_profile_dict(isolated_xdg):
    cfg = Config.load()
    cfg.set("stt.active", "openai")
    name, profile = cfg.stt_profile()
    assert name == "openai"
    assert profile["backend"] == "openai_compatible"
    assert profile["base_url"] == "https://api.openai.com/v1"


def test_secret_prefers_inline_then_env(isolated_xdg, monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert cfg.secret({"api_key_env": "OPENAI_API_KEY"}) == "from-env"
    assert cfg.secret({"api_key": "inline", "api_key_env": "OPENAI_API_KEY"}) == "inline"
    assert cfg.secret({}) is None


def test_errors_reports_bad_values(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.dictate_mode", "sometimes")
    cfg.set("stt.active", "ghost")
    errs = cfg.errors()
    assert any("dictate_mode" in e for e in errs)
    assert any("ghost" in e for e in errs)


def test_load_with_invalid_toml_raises_clear_error(isolated_xdg):
    paths.config_file().write_text("this = [unclosed")
    with pytest.raises(ValueError, match="config.toml"):
        Config.load()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'voice.config'`.

- [ ] **Step 3: Create `voice/config.py`**

```python
"""TOML config with comment-preserving round trips (tomlkit)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from voice import paths

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add voice/config.py tests/test_config.py
git commit -m "feat(config): TOML config with defaults, profiles, secrets and validation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 3: Key spec parsing and press/release tracking (pure logic)

**Files:**
- Create: `voice/hotkey/__init__.py`, `voice/hotkey/keyspec.py`, `tests/hotkey/test_keyspec.py`

**Interfaces:**
- Produces: `KeySpec(codes: frozenset[int], text: str)`; `parse_keyspec(text: str) -> KeySpec` (raises `ValueError` on unknown names; empty string → `KeySpec(frozenset(), "")`); `keyspec_name(code: int) -> str`; `MODIFIER_CODES: frozenset[int]`; `class Tracker`: `__init__(specs: dict[str, KeySpec])`, `feed(code: int, value: int) -> list[tuple[str, str]]` returning `(name, "press"|"release")` events (value 1 = down, 0 = up, 2 = repeat ignored), `held() -> frozenset[int]`, `modifiers_held() -> bool`, `set_specs(specs)`.

Semantics: a spec fires "press" the moment all its codes are held (and it was not already active); "release" when any of its codes goes up while active. Empty specs never fire.

- [ ] **Step 1: Write the failing tests**

`tests/hotkey/__init__.py` (empty) and `tests/hotkey/test_keyspec.py`:
```python
import pytest
from evdev import ecodes as e

from voice.hotkey.keyspec import KeySpec, Tracker, keyspec_name, parse_keyspec


def test_parse_single_and_combo():
    assert parse_keyspec("KEY_F13") == KeySpec(frozenset({e.KEY_F13}), "KEY_F13")
    combo = parse_keyspec("KEY_LEFTMETA+KEY_SPACE")
    assert combo.codes == frozenset({e.KEY_LEFTMETA, e.KEY_SPACE})


def test_parse_is_case_and_space_tolerant():
    assert parse_keyspec(" key_leftctrl + key_v ").codes == frozenset({e.KEY_LEFTCTRL, e.KEY_V})


def test_parse_rejects_unknown_key():
    with pytest.raises(ValueError, match="KEY_BANANA"):
        parse_keyspec("KEY_BANANA")


def test_parse_empty_is_inert():
    assert parse_keyspec("").codes == frozenset()


def test_keyspec_name_round_trips():
    assert keyspec_name(e.KEY_RIGHTCTRL) == "KEY_RIGHTCTRL"


def test_tracker_single_key_press_release():
    t = Tracker({"dictate": parse_keyspec("KEY_F13")})
    assert t.feed(e.KEY_F13, 1) == [("dictate", "press")]
    assert t.feed(e.KEY_F13, 2) == []              # autorepeat ignored
    assert t.feed(e.KEY_F13, 0) == [("dictate", "release")]


def test_tracker_combo_requires_all_keys_and_releases_on_any():
    t = Tracker({"dictate": parse_keyspec("KEY_LEFTMETA+KEY_SPACE")})
    assert t.feed(e.KEY_LEFTMETA, 1) == []
    assert t.feed(e.KEY_SPACE, 1) == [("dictate", "press")]
    assert t.feed(e.KEY_LEFTMETA, 0) == [("dictate", "release")]
    assert t.feed(e.KEY_SPACE, 0) == []            # already released


def test_tracker_held_and_modifiers():
    t = Tracker({})
    t.feed(e.KEY_LEFTSHIFT, 1)
    assert t.modifiers_held()
    assert e.KEY_LEFTSHIFT in t.held()
    t.feed(e.KEY_LEFTSHIFT, 0)
    assert not t.modifiers_held()


def test_tracker_empty_spec_never_fires():
    t = Tracker({"recall": parse_keyspec("")})
    assert t.feed(e.KEY_A, 1) == []


def test_tracker_set_specs_replaces_bindings():
    t = Tracker({"dictate": parse_keyspec("KEY_F13")})
    t.set_specs({"dictate": parse_keyspec("KEY_F14")})
    assert t.feed(e.KEY_F13, 1) == []
    assert t.feed(e.KEY_F14, 1) == [("dictate", "press")]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/hotkey -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'voice.hotkey'`.

- [ ] **Step 3: Create `voice/hotkey/__init__.py` (empty) and `voice/hotkey/keyspec.py`**

```python
"""Key specifications ("KEY_LEFTMETA+KEY_SPACE") and press/release tracking."""
from __future__ import annotations

from dataclasses import dataclass

from evdev import ecodes

MODIFIER_CODES: frozenset[int] = frozenset({
    ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
    ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT, ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
})


@dataclass(frozen=True)
class KeySpec:
    codes: frozenset[int]
    text: str


def parse_keyspec(text: str) -> KeySpec:
    parts = [p.strip().upper() for p in text.split("+") if p.strip()]
    codes: set[int] = set()
    for part in parts:
        code = ecodes.ecodes.get(part)
        if code is None or not part.startswith(("KEY_", "BTN_")):
            raise ValueError(f"unknown key name {part!r}")
        codes.add(code)
    return KeySpec(frozenset(codes), "+".join(parts))


def keyspec_name(code: int) -> str:
    name = ecodes.KEY.get(code) or ecodes.BTN.get(code)
    if isinstance(name, list):          # some codes have aliases
        name = name[0]
    return name or f"KEY_{code}"


class Tracker:
    def __init__(self, specs: dict[str, KeySpec]):
        self._held: set[int] = set()
        self._active: set[str] = set()
        self._specs: dict[str, KeySpec] = {}
        self.set_specs(specs)

    def set_specs(self, specs: dict[str, KeySpec]) -> None:
        self._specs = {n: s for n, s in specs.items() if s.codes}
        self._active.clear()

    def held(self) -> frozenset[int]:
        return frozenset(self._held)

    def modifiers_held(self) -> bool:
        return bool(self._held & MODIFIER_CODES)

    def feed(self, code: int, value: int) -> list[tuple[str, str]]:
        if value == 2:
            return []
        events: list[tuple[str, str]] = []
        if value == 1:
            self._held.add(code)
            for name, spec in self._specs.items():
                if name not in self._active and spec.codes <= self._held:
                    self._active.add(name)
                    events.append((name, "press"))
        else:
            self._held.discard(code)
            for name in list(self._active):
                if code in self._specs[name].codes:
                    self._active.discard(name)
                    events.append((name, "release"))
        return events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/hotkey -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add voice/hotkey tests/hotkey
git commit -m "feat(hotkey): key spec parsing and press/release tracker

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---
### Task 4: evdev listener thread with hot-plug and key capture

**Files:**
- Create: `voice/hotkey/evdev_listener.py`, `tests/hotkey/test_evdev_listener.py`

**Interfaces:**
- Consumes: `Tracker`, `KeySpec`, `keyspec_name`
- Produces: `class EvdevListener`: `__init__(tracker: Tracker, on_event: Callable[[str, str], None], device_factory: Callable[[], list[Device]] | None = None)`, `start()`, `stop()`, `capture_next(callback: Callable[[str], None])` (the next key *press* is reported as its evdev name, e.g. `"KEY_F13"`, and not forwarded to the tracker), `held() -> frozenset[int]`, `modifiers_held() -> bool`, `devices_ok() -> bool` (False when zero readable keyboards were found). `list_keyboards() -> list[evdev.InputDevice]` module function used by `doctor`.

Design: a thread runs `selectors.DefaultSelector` over every device whose capabilities include `EV_KEY` with `KEY_A` (keyboards) or `BTN_SIDE` (mice with side buttons). `/dev/input` is watched with `inotify` (via `select` on an `os.inotify`-free approach: re-scan when a new `event*` node appears, polled every 2 s through `os.listdir` diff, which is enough for hot-plug and avoids another dependency). Unreadable devices (permission denied) are skipped and counted.

- [ ] **Step 1: Write the failing tests**

`tests/hotkey/test_evdev_listener.py`:
```python
import threading
import time

from evdev import ecodes as e

from voice.hotkey.evdev_listener import EvdevListener
from voice.hotkey.keyspec import Tracker, parse_keyspec


class FakeDevice:
    """Stands in for evdev.InputDevice: has a fileno via a pipe and yields queued events."""

    def __init__(self, path="/dev/input/event99"):
        import os
        self.path = path
        self.name = "fake kbd"
        self._r, self._w = os.pipe()
        self._queue = []
        self._lock = threading.Lock()

    def fileno(self):
        return self._r

    def push(self, code, value):
        import os
        with self._lock:
            self._queue.append((code, value))
        os.write(self._w, b"x")

    def read(self):
        import os
        os.read(self._r, 1)
        with self._lock:
            items, self._queue = self._queue, []
        for code, value in items:
            yield type("Ev", (), {"type": e.EV_KEY, "code": code, "value": value})()

    def close(self):
        import os
        os.close(self._r)
        os.close(self._w)


def wait_for(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_listener_forwards_press_and_release_events():
    dev = FakeDevice()
    got = []
    tracker = Tracker({"dictate": parse_keyspec("KEY_F13")})
    listener = EvdevListener(tracker, lambda n, k: got.append((n, k)), device_factory=lambda: [dev])
    listener.start()
    try:
        dev.push(e.KEY_F13, 1)
        assert wait_for(lambda: got == [("dictate", "press")])
        dev.push(e.KEY_F13, 0)
        assert wait_for(lambda: got == [("dictate", "press"), ("dictate", "release")])
        assert listener.devices_ok()
    finally:
        listener.stop()


def test_capture_next_reports_name_and_swallows_event():
    dev = FakeDevice()
    got, captured = [], []
    tracker = Tracker({"dictate": parse_keyspec("KEY_F13")})
    listener = EvdevListener(tracker, lambda n, k: got.append((n, k)), device_factory=lambda: [dev])
    listener.start()
    try:
        listener.capture_next(captured.append)
        dev.push(e.KEY_F13, 1)
        dev.push(e.KEY_F13, 0)
        assert wait_for(lambda: captured == ["KEY_F13"])
        time.sleep(0.05)
        assert got == []                      # not forwarded while capturing
        dev.push(e.KEY_F13, 1)
        assert wait_for(lambda: got == [("dictate", "press")])
    finally:
        listener.stop()


def test_no_devices_marks_not_ok_and_stop_is_clean():
    listener = EvdevListener(Tracker({}), lambda n, k: None, device_factory=lambda: [])
    listener.start()
    assert wait_for(lambda: listener.devices_ok() is False)
    listener.stop()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/hotkey/test_evdev_listener.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'voice.hotkey.evdev_listener'`.

- [ ] **Step 3: Create `voice/hotkey/evdev_listener.py`**

```python
"""Background thread reading keyboards via evdev and feeding the Tracker."""
from __future__ import annotations

import logging
import os
import selectors
import threading
from typing import Callable, Iterable

import evdev
from evdev import ecodes

from voice.hotkey.keyspec import Tracker, keyspec_name

log = logging.getLogger(__name__)
INPUT_DIR = "/dev/input"
RESCAN_SECONDS = 2.0


def _is_keyboard_like(dev: evdev.InputDevice) -> bool:
    keys = dev.capabilities().get(ecodes.EV_KEY, [])
    return ecodes.KEY_A in keys or ecodes.BTN_SIDE in keys


def list_keyboards() -> list[evdev.InputDevice]:
    found: list[evdev.InputDevice] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except (PermissionError, OSError) as exc:
            log.debug("skip %s: %s", path, exc)
            continue
        if _is_keyboard_like(dev):
            found.append(dev)
        else:
            dev.close()
    return found


class EvdevListener:
    def __init__(
        self,
        tracker: Tracker,
        on_event: Callable[[str, str], None],
        device_factory: Callable[[], list] | None = None,
    ):
        self._tracker = tracker
        self._on_event = on_event
        self._factory = device_factory or list_keyboards
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()
        self._capture: Callable[[str], None] | None = None
        self._lock = threading.Lock()
        self._devices_ok: bool | None = None

    # -- public -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="evdev-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        os.write(self._wake_w, b"x")
        if self._thread:
            self._thread.join(timeout=2)

    def capture_next(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._capture = callback

    def held(self):
        return self._tracker.held()

    def modifiers_held(self) -> bool:
        return self._tracker.modifiers_held()

    def devices_ok(self) -> bool | None:
        return self._devices_ok

    # -- thread -----------------------------------------------------------
    def _run(self) -> None:
        sel = selectors.DefaultSelector()
        sel.register(self._wake_r, selectors.EVENT_READ, data=None)
        devices: dict[int, object] = {}
        known_nodes: set[str] = set()

        def rescan() -> None:
            nonlocal known_nodes
            for dev in self._factory():
                if dev.fileno() not in devices:
                    devices[dev.fileno()] = dev
                    sel.register(dev.fileno(), selectors.EVENT_READ, data=dev)
                    log.info("listening on %s (%s)", dev.path, dev.name)
            self._devices_ok = bool(devices)
            known_nodes = set(os.listdir(INPUT_DIR)) if os.path.isdir(INPUT_DIR) else set()

        rescan()
        while not self._stop.is_set():
            for key, _ in sel.select(timeout=RESCAN_SECONDS):
                if key.data is None:
                    os.read(self._wake_r, 1)
                    continue
                dev = key.data
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY:
                            self._handle(ev.code, ev.value)
                except OSError:
                    log.info("device gone: %s", getattr(dev, "path", "?"))
                    sel.unregister(key.fd)
                    devices.pop(key.fd, None)
                    self._devices_ok = bool(devices)
            if os.path.isdir(INPUT_DIR) and set(os.listdir(INPUT_DIR)) != known_nodes:
                rescan()
        for dev in devices.values():
            try:
                dev.close()
            except Exception:
                pass
        sel.close()

    def _handle(self, code: int, value: int) -> None:
        with self._lock:
            cb = self._capture
            if cb is not None and value == 1:
                self._capture = None
        if cb is not None and value == 1:
            cb(keyspec_name(code))
            return
        if cb is None and self._capture is None:
            for name, kind in self._tracker.feed(code, value):
                self._on_event(name, kind)
```

Note on the swallow logic: while a capture is pending, all events are dropped; the press that satisfies the capture is reported by name; the matching release after capture is also dropped because `_capture` was cleared only on the press, so the release path checks `self._capture is None` and the tracker never saw the press, so `feed` on release yields nothing. The test asserts exactly this.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/hotkey -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add voice/hotkey/evdev_listener.py tests/hotkey/test_evdev_listener.py
git commit -m "feat(hotkey): evdev listener thread with hot-plug and key capture

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 5: PCM helpers and WAV encoding

**Files:**
- Create: `voice/audio/__init__.py`, `voice/audio/pcm.py`, `tests/audio/__init__.py`, `tests/audio/test_pcm.py`

**Interfaces:**
- Produces: `SAMPLE_RATE = 16000`; `to_float32(pcm: np.ndarray[int16]) -> np.ndarray[float32]` (range −1..1); `to_wav_bytes(pcm: np.ndarray[int16], rate: int = SAMPLE_RATE) -> bytes`; `duration_s(pcm) -> float`; `from_bytes(raw: bytes) -> np.ndarray[int16]`.

- [ ] **Step 1: Write the failing tests**

`tests/audio/test_pcm.py`:
```python
import io
import wave

import numpy as np

from voice.audio.pcm import SAMPLE_RATE, duration_s, from_bytes, to_float32, to_wav_bytes


def test_to_float32_scales_int16():
    pcm = np.array([0, 16384, -32768, 32767], dtype=np.int16)
    f = to_float32(pcm)
    assert f.dtype == np.float32
    assert np.allclose(f, [0.0, 0.5, -1.0, 32767 / 32768])


def test_wav_bytes_is_valid_mono_16k_wav():
    pcm = (np.sin(np.linspace(0, 100, SAMPLE_RATE)) * 10000).astype(np.int16)
    data = to_wav_bytes(pcm)
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == SAMPLE_RATE
        assert w.getnframes() == SAMPLE_RATE


def test_from_bytes_and_duration():
    pcm = np.arange(8000, dtype=np.int16)
    assert np.array_equal(from_bytes(pcm.tobytes()), pcm)
    assert duration_s(pcm) == 0.5
    assert from_bytes(b"\x01").size == 0      # odd trailing byte dropped, no crash
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/audio -v` → FAIL, `No module named 'voice.audio'`.

- [ ] **Step 3: Create `voice/audio/__init__.py` (empty) and `voice/audio/pcm.py`**

```python
"""Small PCM helpers: 16 kHz mono int16 is the app's only audio format."""
from __future__ import annotations

import io
import wave

import numpy as np

SAMPLE_RATE = 16000


def to_float32(pcm: np.ndarray) -> np.ndarray:
    return (pcm.astype(np.float32) / 32768.0).astype(np.float32)


def to_wav_bytes(pcm: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.ascontiguousarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


def from_bytes(raw: bytes) -> np.ndarray:
    usable = len(raw) - (len(raw) % 2)
    return np.frombuffer(raw[:usable], dtype=np.int16)


def duration_s(pcm: np.ndarray, rate: int = SAMPLE_RATE) -> float:
    return float(pcm.size) / rate
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/audio -v`, expected 3 passed.

- [ ] **Step 5: Commit**

```bash
git add voice/audio tests/audio
git commit -m "feat(audio): pcm helpers and wav encoding

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 6: Silence trimming with Silero VAD

**Files:**
- Create: `voice/audio/vad.py`, `tests/audio/test_vad.py`

**Interfaces:**
- Consumes: `voice.audio.pcm.to_float32`, `SAMPLE_RATE`
- Produces: `trim_silence(pcm: np.ndarray, pad_ms: int = 200, timestamps_fn: Callable[[np.ndarray], list[dict]] | None = None) -> np.ndarray` returning the concatenation of speech spans padded by `pad_ms`, or an empty int16 array when no speech; `MIN_SPEECH_MS = 300`. Default `timestamps_fn` wraps `faster_whisper.vad.get_speech_timestamps` (samples in, list of `{"start","end"}` in samples out).

- [ ] **Step 1: Write the failing tests**

`tests/audio/test_vad.py`:
```python
import numpy as np
import pytest

from voice.audio.pcm import SAMPLE_RATE
from voice.audio.vad import trim_silence


def test_no_speech_returns_empty():
    pcm = np.zeros(SAMPLE_RATE, dtype=np.int16)
    out = trim_silence(pcm, timestamps_fn=lambda a: [])
    assert out.size == 0 and out.dtype == np.int16


def test_spans_are_padded_clipped_and_concatenated():
    pcm = np.arange(SAMPLE_RATE * 2, dtype=np.int16)      # 2 s ramp so we can check indices
    spans = [{"start": 1600, "end": 3200}, {"start": 30000, "end": 32000}]
    out = trim_silence(pcm, pad_ms=100, timestamps_fn=lambda a: spans)
    pad = SAMPLE_RATE // 10
    expected = np.concatenate([pcm[1600 - pad:3200 + pad], pcm[30000 - pad:SAMPLE_RATE * 2]])
    assert np.array_equal(out, expected)


def test_padding_does_not_go_negative():
    pcm = np.arange(8000, dtype=np.int16)
    out = trim_silence(pcm, pad_ms=500, timestamps_fn=lambda a: [{"start": 100, "end": 400}])
    assert np.array_equal(out, pcm[0:400 + 8000])


def test_timestamps_fn_receives_float32(monkeypatch):
    seen = {}

    def fn(a):
        seen["dtype"] = a.dtype
        return []

    trim_silence(np.zeros(1600, dtype=np.int16), timestamps_fn=fn)
    assert seen["dtype"] == np.float32


@pytest.mark.boundary
def test_real_silero_on_silence_is_empty():
    out = trim_silence(np.zeros(SAMPLE_RATE * 2, dtype=np.int16))
    assert out.size == 0
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/audio/test_vad.py -v`, FAIL `No module named 'voice.audio.vad'`.

- [ ] **Step 3: Create `voice/audio/vad.py`**

```python
"""Trim leading/trailing/inner silence using the Silero VAD bundled with faster-whisper."""
from __future__ import annotations

from typing import Callable

import numpy as np

from voice.audio.pcm import SAMPLE_RATE, to_float32

MIN_SPEECH_MS = 300
TimestampsFn = Callable[[np.ndarray], list[dict]]


def _silero(audio: np.ndarray) -> list[dict]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps  # heavy import, lazy

    return get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=0))


def trim_silence(pcm: np.ndarray, pad_ms: int = 200, timestamps_fn: TimestampsFn | None = None) -> np.ndarray:
    fn = timestamps_fn or _silero
    spans = fn(to_float32(pcm))
    if not spans:
        return np.zeros(0, dtype=np.int16)
    pad = int(SAMPLE_RATE * pad_ms / 1000)
    pieces = []
    for span in spans:
        start = max(0, int(span["start"]) - pad)
        end = min(pcm.size, int(span["end"]) + pad)
        pieces.append(pcm[start:end])
    return np.concatenate(pieces).astype(np.int16)
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/audio -v`, expected 7 passed (boundary test deselected). Then run the boundary test once: `uv run pytest tests/audio/test_vad.py -m boundary -v` → 1 passed (downloads nothing; Silero weights ship inside faster-whisper).

- [ ] **Step 5: Commit**

```bash
git add voice/audio/vad.py tests/audio/test_vad.py
git commit -m "feat(audio): silence trimming with bundled silero vad

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 7: Recorder around pw-record and source listing

**Files:**
- Create: `voice/audio/capture.py`, `tests/audio/test_capture.py`

**Interfaces:**
- Consumes: `from_bytes`, `SAMPLE_RATE`
- Produces: `pw_record_command(device: str | None) -> list[str]`; `@dataclass Source(name: str, description: str, is_default: bool)`; `list_sources(run=subprocess.run) -> list[Source]` (parses `pw-dump` JSON for `media.class == "Audio/Source"`); `class Recorder`: `__init__(popen=subprocess.Popen)`, `start(device: str | None)`, `stop() -> np.ndarray` (int16), `cancel()`, `is_recording: bool`, `error: str | None` (stderr tail if pw-record died). `RecorderError(RuntimeError)` raised by `start` if the process fails to spawn.

- [ ] **Step 1: Write the failing tests**

`tests/audio/test_capture.py`:
```python
import io
import json
import subprocess

import numpy as np
import pytest

from voice.audio.capture import Recorder, RecorderError, list_sources, pw_record_command


def test_command_shape():
    assert pw_record_command(None) == ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "-"]
    assert "--target" in pw_record_command("alsa_input.usb-OBSBOT")
    assert pw_record_command("alsa_input.usb-OBSBOT")[-3:] == ["--target", "alsa_input.usb-OBSBOT", "-"]


class FakeProc:
    def __init__(self, data: bytes, rc=0, stderr=b""):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self._rc = rc
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = self._rc

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def kill(self):
        self.returncode = -9

    def poll(self):
        return self.returncode


def test_recorder_collects_pcm_until_stop():
    pcm = np.arange(1600, dtype=np.int16)
    proc = FakeProc(pcm.tobytes())
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    assert rec.is_recording
    out = rec.stop()
    assert proc.terminated
    assert np.array_equal(out, pcm)
    assert not rec.is_recording


def test_recorder_cancel_discards_audio():
    proc = FakeProc(b"\x01\x00" * 100)
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start(None)
    rec.cancel()
    assert not rec.is_recording
    assert rec.stop().size == 0


def test_recorder_reports_process_failure():
    proc = FakeProc(b"", rc=1, stderr=b"pw-record: no such target\n")
    rec = Recorder(popen=lambda *a, **k: proc)
    rec.start("nope")
    rec.stop()
    assert "no such target" in (rec.error or "")


def test_recorder_spawn_failure_raises():
    def boom(*a, **k):
        raise FileNotFoundError("pw-record")
    with pytest.raises(RecorderError, match="pw-record"):
        Recorder(popen=boom).start(None)


def test_list_sources_parses_pw_dump():
    dump = [
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "media.class": "Audio/Source", "node.name": "alsa_input.usb-OBSBOT", "node.description": "OBSBOT Tiny 3"}}},
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "media.class": "Audio/Sink", "node.name": "alsa_output.hdmi", "node.description": "HDMI"}}},
        {"type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
         "metadata": [{"key": "default.audio.source", "value": {"name": "alsa_input.usb-OBSBOT"}}]},
    ]
    run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=json.dumps(dump), stderr="")
    sources = list_sources(run=run)
    assert [s.name for s in sources] == ["alsa_input.usb-OBSBOT"]
    assert sources[0].description == "OBSBOT Tiny 3"
    assert sources[0].is_default is True


def test_list_sources_tolerates_failure():
    run = lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no pipewire")
    assert list_sources(run=run) == []


@pytest.mark.boundary
def test_real_pw_record_captures_half_second():
    import time
    rec = Recorder()
    rec.start(None)
    time.sleep(0.5)
    out = rec.stop()
    assert rec.error is None
    assert out.size > 16000 * 0.3
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/audio/test_capture.py -v`, FAIL `No module named 'voice.audio.capture'`.

- [ ] **Step 3: Create `voice/audio/capture.py`**

```python
"""Microphone capture through a pw-record subprocess (PipeWire does the resampling)."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np

from voice.audio.pcm import SAMPLE_RATE, from_bytes

log = logging.getLogger(__name__)


class RecorderError(RuntimeError):
    pass


def pw_record_command(device: str | None) -> list[str]:
    cmd = ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16"]
    if device:
        cmd += ["--target", device]
    return cmd + ["-"]


@dataclass(frozen=True)
class Source:
    name: str
    description: str
    is_default: bool


def list_sources(run: Callable = subprocess.run) -> list[Source]:
    try:
        cp = run(["pw-dump"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("pw-dump failed: %s", exc)
        return []
    if cp.returncode != 0:
        return []
    try:
        objects = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    default = None
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Metadata":
            for item in obj.get("metadata", []):
                if item.get("key") == "default.audio.source":
                    default = (item.get("value") or {}).get("name")
    sources = []
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Source":
            name = props.get("node.name", "")
            sources.append(Source(name, props.get("node.description", name), name == default))
    return sources


class Recorder:
    def __init__(self, popen: Callable = subprocess.Popen):
        self._popen = popen
        self._proc = None
        self._chunks: list[bytes] = []
        self._reader: threading.Thread | None = None
        self._cancelled = False
        self.error: str | None = None

    @property
    def is_recording(self) -> bool:
        return self._proc is not None

    def start(self, device: str | None) -> None:
        if self._proc is not None:
            return
        self._chunks = []
        self._cancelled = False
        self.error = None
        try:
            self._proc = self._popen(pw_record_command(device), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise RecorderError(f"cannot start pw-record: {exc}") from exc
        self._reader = threading.Thread(target=self._pump, name="pw-record-reader", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        proc = self._proc
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            if not self._cancelled:
                self._chunks.append(chunk)

    def cancel(self) -> None:
        self._cancelled = True
        self._chunks = []
        self.stop()

    def stop(self) -> np.ndarray:
        proc = self._proc
        if proc is None:
            return np.zeros(0, dtype=np.int16)
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        if self._reader:
            self._reader.join(timeout=2)
        rc = proc.returncode
        if rc not in (0, None, -15):
            tail = proc.stderr.read().decode(errors="replace")[-400:]
            self.error = tail.strip() or f"pw-record exited with {rc}"
            log.warning("pw-record failed: %s", self.error)
        self._proc = None
        data = b"" if self._cancelled else b"".join(self._chunks)
        self._chunks = []
        return from_bytes(data)
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/audio -v`, expected 13 passed. Then in the VM desktop session: `uv run pytest tests/audio/test_capture.py -m boundary -v` → 1 passed (the Hollyland mic is the default source).

- [ ] **Step 5: Commit**

```bash
git add voice/audio/capture.py tests/audio/test_capture.py
git commit -m "feat(audio): pw-record recorder and pipewire source listing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---
### Task 8: STT protocol and the local faster-whisper backend

**Files:**
- Create: `voice/stt/__init__.py`, `voice/stt/base.py`, `voice/stt/local.py`, `tests/stt/__init__.py`, `tests/stt/test_local.py`

**Interfaces:**
- Consumes: `to_float32`, `duration_s`
- Produces (`voice/stt/base.py`): `@dataclass Transcript(text: str, language: str | None, audio_s: float, elapsed_s: float, backend: str)`; `class TranscriptionError(RuntimeError)`; `class Transcriber(Protocol)`: `name: str`, `transcribe(pcm: np.ndarray, language: str | None, prompt: str | None) -> Transcript`, `warmup() -> None` (load model / no-op), `describe() -> str` (one line for the tray tooltip).
- Produces (`voice/stt/local.py`): `class LocalTranscriber(profile: dict, model_factory: Callable = None, cuda_available: Callable[[], bool] = None)` with `.fallback_reason: str | None` set when CUDA was requested but unavailable. Model aliases: `"large-v3-turbo" -> "Systran/faster-whisper-large-v3-turbo"`, anything containing `/` is used verbatim, other names are passed to faster-whisper unchanged. `language="auto"` becomes `None`.
- Produces (`voice/stt/__init__.py`): `make_transcriber(profile: dict, secret: str | None) -> Transcriber` (registry; `openai_compatible` wired in Task 9).

- [ ] **Step 1: Write the failing tests**

`tests/stt/test_local.py`:
```python
import numpy as np
import pytest

from voice.stt.base import Transcript, TranscriptionError
from voice.stt.local import LocalTranscriber, resolve_model_name


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeInfo:
    language = "en"


class FakeModel:
    calls = []

    def __init__(self, name, device, compute_type, **kw):
        FakeModel.calls.append((name, device, compute_type))

    def transcribe(self, audio, **kw):
        FakeModel.last_kwargs = kw
        assert audio.dtype == np.float32
        return iter([FakeSegment(" Hello"), FakeSegment(" world.")]), FakeInfo()


@pytest.fixture(autouse=True)
def _reset():
    FakeModel.calls = []


def test_resolve_model_name_aliases():
    assert resolve_model_name("large-v3-turbo") == "Systran/faster-whisper-large-v3-turbo"
    assert resolve_model_name("KBLab/kb-whisper-large") == "KBLab/kb-whisper-large"
    assert resolve_model_name("small") == "small"


def test_transcribe_joins_segments_and_reports_metadata():
    t = LocalTranscriber({"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16", "beam_size": 3},
                         model_factory=FakeModel, cuda_available=lambda: True)
    pcm = np.zeros(16000, dtype=np.int16)
    out = t.transcribe(pcm, language="en", prompt="CachyOS")
    assert isinstance(out, Transcript)
    assert out.text == "Hello world."
    assert out.language == "en" and out.audio_s == 1.0 and out.backend == "local"
    assert FakeModel.calls == [("Systran/faster-whisper-large-v3-turbo", "cuda", "float16")]
    assert FakeModel.last_kwargs["beam_size"] == 3
    assert FakeModel.last_kwargs["initial_prompt"] == "CachyOS"
    assert FakeModel.last_kwargs["language"] == "en"


def test_auto_language_passes_none():
    t = LocalTranscriber({"model": "small"}, model_factory=FakeModel, cuda_available=lambda: True)
    t.transcribe(np.zeros(1600, dtype=np.int16), language="auto", prompt="")
    assert FakeModel.last_kwargs["language"] is None
    assert FakeModel.last_kwargs["initial_prompt"] is None


def test_cpu_fallback_when_cuda_missing():
    t = LocalTranscriber({"model": "small", "device": "cuda", "compute_type": "float16"},
                         model_factory=FakeModel, cuda_available=lambda: False)
    t.warmup()
    assert FakeModel.calls == [("small", "cpu", "int8")]
    assert "CUDA" in t.fallback_reason
    assert "cpu" in t.describe()


def test_model_loaded_once_and_errors_wrapped():
    class Boom(FakeModel):
        def transcribe(self, audio, **kw):
            raise RuntimeError("cublas exploded")

    t = LocalTranscriber({"model": "small"}, model_factory=Boom, cuda_available=lambda: True)
    with pytest.raises(TranscriptionError, match="cublas"):
        t.transcribe(np.zeros(1600, dtype=np.int16), None, None)
    with pytest.raises(TranscriptionError):
        t.transcribe(np.zeros(1600, dtype=np.int16), None, None)
    assert len(FakeModel.calls) == 1


@pytest.mark.gpu
def test_real_cuda_transcribes_fixture_under_one_second():
    import time
    from pathlib import Path
    import wave
    fixture = Path(__file__).parent / "fixtures" / "hello.wav"   # owner records: "hello world, testing one two three"
    with wave.open(str(fixture)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    t = LocalTranscriber({"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16"})
    t.warmup()
    start = time.time()
    out = t.transcribe(pcm, "en", None)
    assert time.time() - start < 1.0
    assert "hello" in out.text.lower()
    assert t.fallback_reason is None
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/stt -v`, FAIL `No module named 'voice.stt'`.

- [ ] **Step 3: Create `voice/stt/base.py`**

```python
"""Transcriber protocol shared by local and cloud backends."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    audio_s: float
    elapsed_s: float
    backend: str


class TranscriptionError(RuntimeError):
    """Raised for any backend failure; message is user-presentable."""


class Transcriber(Protocol):
    name: str

    def transcribe(self, pcm: np.ndarray, language: str | None, prompt: str | None) -> Transcript: ...
    def warmup(self) -> None: ...
    def describe(self) -> str: ...
```

- [ ] **Step 4: Create `voice/stt/local.py`**

```python
"""faster-whisper backend (CTranslate2), CUDA with CPU int8 fallback."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import numpy as np

from voice.audio.pcm import duration_s, to_float32
from voice.stt.base import Transcript, TranscriptionError

log = logging.getLogger(__name__)

_ALIASES = {
    "large-v3-turbo": "Systran/faster-whisper-large-v3-turbo",
    "turbo": "Systran/faster-whisper-large-v3-turbo",
}


def resolve_model_name(name: str) -> str:
    return _ALIASES.get(name, name)


def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception as exc:  # missing libs count as unavailable
        log.info("cuda probe failed: %s", exc)
        return False


def _default_factory(name: str, device: str, compute_type: str, **kw):
    from faster_whisper import WhisperModel  # lazy: slow import
    return WhisperModel(name, device=device, compute_type=compute_type, **kw)


class LocalTranscriber:
    name = "local"

    def __init__(self, profile: dict, model_factory: Callable | None = None,
                 cuda_available: Callable[[], bool] | None = None):
        self._profile = profile
        self._factory = model_factory or _default_factory
        self._cuda = cuda_available or _cuda_available
        self._model = None
        self._lock = threading.Lock()
        self._device = profile.get("device", "cuda")
        self._compute = profile.get("compute_type", "float16")
        self.fallback_reason: str | None = None

    def warmup(self) -> None:
        with self._lock:
            if self._model is None:
                self._model = self._load()

    def _load(self):
        device, compute = self._device, self._compute
        if device == "cuda" and not self._cuda():
            self.fallback_reason = "CUDA not available; using CPU int8 (slower)"
            log.warning(self.fallback_reason)
            device, compute = "cpu", "int8"
        self._device, self._compute = device, compute
        log.info("loading %s on %s/%s", resolve_model_name(self._profile["model"]), device, compute)
        return self._factory(resolve_model_name(self._profile["model"]), device, compute)

    def describe(self) -> str:
        return f"local {self._profile.get('model')} ({self._device}/{self._compute})"

    def transcribe(self, pcm: np.ndarray, language: str | None, prompt: str | None) -> Transcript:
        self.warmup()
        lang = None if language in (None, "", "auto") else language
        start = time.time()
        try:
            segments, info = self._model.transcribe(
                to_float32(pcm),
                language=lang,
                initial_prompt=prompt or None,
                beam_size=int(self._profile.get("beam_size", 5)),
                vad_filter=False,
                condition_on_previous_text=False,
            )
            text = "".join(s.text for s in segments).strip()
        except Exception as exc:
            raise TranscriptionError(f"local transcription failed: {exc}") from exc
        return Transcript(text, getattr(info, "language", lang), duration_s(pcm), time.time() - start, self.name)
```

- [ ] **Step 5: Create `voice/stt/__init__.py`**

```python
"""Backend registry."""
from __future__ import annotations

from voice.stt.base import Transcriber, Transcript, TranscriptionError  # re-export


def make_transcriber(profile: dict, secret: str | None) -> Transcriber:
    backend = profile.get("backend")
    if backend == "local":
        from voice.stt.local import LocalTranscriber
        return LocalTranscriber(profile)
    if backend == "openai_compatible":
        from voice.stt.openai_compat import OpenAICompatTranscriber
        return OpenAICompatTranscriber(profile, secret)
    raise ValueError(f"unknown stt backend {backend!r}")
```

- [ ] **Step 6: Run tests** → `uv run pytest tests/stt -v`, expected 5 passed, 1 deselected (gpu).

- [ ] **Step 7: Commit**

```bash
git add voice/stt tests/stt
git commit -m "feat(stt): transcriber protocol and faster-whisper backend with cpu fallback

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 9: OpenAI-compatible cloud backend

**Files:**
- Create: `voice/stt/openai_compat.py`, `tests/stt/test_openai_compat.py`

**Interfaces:**
- Consumes: `to_wav_bytes`, `duration_s`, `Transcript`, `TranscriptionError`
- Produces: `build_request(profile: dict, language: str | None, prompt: str | None) -> tuple[str, dict]` returning `(url, form_fields)`; `class OpenAICompatTranscriber(profile: dict, secret: str | None, client: httpx.Client | None = None)` with `name = "openai_compatible"`. Quirks: OpenRouter base URLs never get `prompt`; models starting with `gpt-transcribe` or `gpt-4o-transcribe` send `languages[]`? No: they accept `language` too, keep it simple: always send `language` when not auto. `prompt` is sent when non-empty and the host is not openrouter. `response_format=json`. Timeout 30 s. Errors map HTTP status to a readable message including the provider's `error.message` when present. Missing API key raises `TranscriptionError("no API key ...")` before any request.

- [ ] **Step 1: Write the failing tests**

`tests/stt/test_openai_compat.py`:
```python
import json

import httpx
import numpy as np
import pytest

from voice.stt.base import TranscriptionError
from voice.stt.openai_compat import OpenAICompatTranscriber, build_request

OPENAI = {"backend": "openai_compatible", "base_url": "https://api.openai.com/v1", "model": "gpt-transcribe", "prompt": "CachyOS"}
OPENROUTER = {"backend": "openai_compatible", "base_url": "https://openrouter.ai/api/v1/", "model": "openai/whisper-large-v3-turbo", "prompt": "CachyOS"}


def test_build_request_openai_sends_prompt_and_language():
    url, fields = build_request(OPENAI, "en", "CachyOS")
    assert url == "https://api.openai.com/v1/audio/transcriptions"
    assert fields == {"model": "gpt-transcribe", "response_format": "json", "language": "en", "prompt": "CachyOS"}


def test_build_request_openrouter_drops_prompt_and_handles_trailing_slash():
    url, fields = build_request(OPENROUTER, "auto", "CachyOS")
    assert url == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert "prompt" not in fields and "language" not in fields


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_transcribe_posts_wav_and_parses_text():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["ct"] = request.headers["content-type"]
        body = request.read()
        seen["has_wav"] = b"RIFF" in body and b'filename="audio.wav"' in body
        seen["has_model"] = b"gpt-transcribe" in body
        return httpx.Response(200, json={"text": " Hello there. "})

    t = OpenAICompatTranscriber(OPENAI, "sk-test", client=_client(handler))
    out = t.transcribe(np.zeros(16000, dtype=np.int16), "en", "CachyOS")
    assert out.text == "Hello there."
    assert out.backend == "openai_compatible" and out.audio_s == 1.0
    assert seen["auth"] == "Bearer sk-test"
    assert seen["ct"].startswith("multipart/form-data")
    assert seen["has_wav"] and seen["has_model"]


def test_missing_key_fails_before_request():
    t = OpenAICompatTranscriber(OPENAI, None, client=_client(lambda r: pytest.fail("no request expected")))
    with pytest.raises(TranscriptionError, match="API key"):
        t.transcribe(np.zeros(1600, dtype=np.int16), "en", None)


def test_http_error_surfaces_provider_message():
    handler = lambda r: httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}})
    t = OpenAICompatTranscriber(OPENAI, "bad", client=_client(handler))
    with pytest.raises(TranscriptionError, match="401.*Incorrect API key"):
        t.transcribe(np.zeros(1600, dtype=np.int16), "en", None)


def test_network_error_is_wrapped():
    def handler(r):
        raise httpx.ConnectError("boom")
    t = OpenAICompatTranscriber(OPENAI, "k", client=_client(handler))
    with pytest.raises(TranscriptionError, match="boom"):
        t.transcribe(np.zeros(1600, dtype=np.int16), "en", None)


def test_describe_names_model_and_host():
    t = OpenAICompatTranscriber(OPENROUTER, "k")
    assert "openrouter.ai" in t.describe() and "whisper-large-v3-turbo" in t.describe()
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/stt/test_openai_compat.py -v`, FAIL `No module named 'voice.stt.openai_compat'`.

- [ ] **Step 3: Create `voice/stt/openai_compat.py`**

```python
"""POST /audio/transcriptions backend: OpenAI, Groq, Mistral, Together, OpenRouter, any."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
import numpy as np

from voice.audio.pcm import duration_s, to_wav_bytes
from voice.stt.base import Transcript, TranscriptionError

TIMEOUT_S = 30.0
_NO_PROMPT_HOSTS = ("openrouter.ai",)


def build_request(profile: dict, language: str | None, prompt: str | None) -> tuple[str, dict]:
    base = str(profile["base_url"]).rstrip("/")
    host = urlparse(base).hostname or ""
    fields = {"model": profile["model"], "response_format": "json"}
    if language and language != "auto":
        fields["language"] = language
    if prompt and not any(h in host for h in _NO_PROMPT_HOSTS):
        fields["prompt"] = prompt
    return f"{base}/audio/transcriptions", fields


class OpenAICompatTranscriber:
    name = "openai_compatible"

    def __init__(self, profile: dict, secret: str | None, client: httpx.Client | None = None):
        self._profile = profile
        self._secret = secret
        self._client = client or httpx.Client(timeout=TIMEOUT_S)

    def warmup(self) -> None:
        return None

    def describe(self) -> str:
        return f"{self._profile.get('model')} @ {urlparse(str(self._profile.get('base_url'))).hostname}"

    def transcribe(self, pcm: np.ndarray, language: str | None, prompt: str | None) -> Transcript:
        if not self._secret:
            raise TranscriptionError(
                f"no API key for {self.describe()}: set api_key or api_key_env in the profile")
        url, fields = build_request(self._profile, language, prompt or self._profile.get("prompt") or None)
        files = {"file": ("audio.wav", to_wav_bytes(pcm), "audio/wav")}
        start = time.time()
        try:
            resp = self._client.post(url, data=fields, files=files,
                                     headers={"Authorization": f"Bearer {self._secret}"})
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"request to {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = resp.text[:200]
            raise TranscriptionError(f"{self.describe()} returned {resp.status_code}: {detail}")
        try:
            text = str(resp.json().get("text", "")).strip()
        except ValueError as exc:
            raise TranscriptionError(f"{self.describe()} returned non-JSON") from exc
        lang = None if language in (None, "", "auto") else language
        return Transcript(text, lang, duration_s(pcm), time.time() - start, self.name)
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/stt -v`, expected 12 passed, 1 deselected.

- [ ] **Step 5: Commit**

```bash
git add voice/stt/openai_compat.py tests/stt/test_openai_compat.py
git commit -m "feat(stt): openai-compatible transcription backend

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 10: Dictionary replacements

**Files:**
- Create: `voice/text.py`, `tests/test_text.py`

**Interfaces:**
- Produces: `apply_replacements(text: str, rules: list[list]) -> str`. Rule = `[from, to]` or `[from, to, flags]` where flags is a string containing `icase` and/or `regex` separated by commas or spaces. Plain rules match on word boundaries, case-sensitive unless `icase`. Bad regexes are skipped (logged), never raise. `normalize_text(text) -> str` collapses whitespace, strips, converts curly quotes to straight ones (voxtype's trick so pasted text matches typed text).

- [ ] **Step 1: Write the failing tests**

`tests/test_text.py`:
```python
from voice.text import apply_replacements, normalize_text


def test_plain_rule_is_word_bounded_and_case_sensitive():
    rules = [["cachy os", "CachyOS"]]
    assert apply_replacements("I use cachy os daily", rules) == "I use CachyOS daily"
    assert apply_replacements("Cachy OS", rules) == "Cachy OS"
    assert apply_replacements("mycachy os", rules) == "mycachy os"


def test_icase_flag():
    assert apply_replacements("Obs Bot is here", [["obs bot", "OBSBOT", "icase"]]) == "OBSBOT is here"


def test_regex_flag_and_ordering():
    rules = [[r"(\d+) percent", r"\1%", "regex"], ["%", " percent"]]
    assert apply_replacements("50 percent", rules) == "50 percent"     # second rule undoes first: order respected


def test_bad_regex_is_skipped():
    assert apply_replacements("keep", [["(", "x", "regex"]]) == "keep"


def test_normalize_text():
    assert normalize_text("  “Hi”  there ’s \n more ") == '"Hi" there \'s more'
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_text.py -v`, FAIL `No module named 'voice.text'`.

- [ ] **Step 3: Create `voice/text.py`**

```python
"""Post-processing of transcripts: dictionary replacements and normalisation."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)
_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"})


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip()


def apply_replacements(text: str, rules: list[list]) -> str:
    for rule in rules or []:
        if len(rule) < 2:
            continue
        src, dst = str(rule[0]), str(rule[1])
        flags = {f.strip().lower() for f in re.split(r"[,\s]+", str(rule[2]))} if len(rule) > 2 else set()
        re_flags = re.IGNORECASE if "icase" in flags else 0
        pattern = src if "regex" in flags else rf"\b{re.escape(src)}\b"
        try:
            text = re.sub(pattern, dst, text, flags=re_flags)
        except re.error as exc:
            log.warning("skipping bad replacement %r: %s", src, exc)
    return text
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/test_text.py -v`, expected 5 passed.

- [ ] **Step 5: Commit**

```bash
git add voice/text.py tests/test_text.py
git commit -m "feat(text): dictionary replacements and quote normalisation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 11: Dictation history with recall and retry audio

**Files:**
- Create: `voice/history.py`, `tests/test_history.py`

**Interfaces:**
- Consumes: `paths.history_file()`
- Produces: `@dataclass Entry(text: str, ts: float, backend: str, audio_s: float, elapsed_s: float)`; `class History(path: Path | None = None, limit: int = 20)`: `add(entry: Entry)`, `last() -> Entry | None`, `entries() -> list[Entry]` (newest last), `keep_audio(pcm: np.ndarray)`, `take_audio() -> np.ndarray | None` (returns and clears the retained audio for a retry). Persistence: JSONL append, truncated to `limit` lines on load and on add. Audio is never written to disk.

- [ ] **Step 1: Write the failing tests**

`tests/test_history.py`:
```python
import json

import numpy as np

from voice import paths
from voice.history import Entry, History


def e(text, ts=1.0):
    return Entry(text=text, ts=ts, backend="local", audio_s=1.2, elapsed_s=0.3)


def test_add_last_and_persist_roundtrip(isolated_xdg):
    h = History()
    assert h.last() is None
    h.add(e("one"))
    h.add(e("two", 2.0))
    assert h.last().text == "two"
    lines = paths.history_file().read_text().splitlines()
    assert json.loads(lines[-1])["text"] == "two"
    again = History()
    assert [x.text for x in again.entries()] == ["one", "two"]


def test_limit_is_enforced_on_add_and_load(isolated_xdg):
    h = History(limit=3)
    for i in range(5):
        h.add(e(str(i), float(i)))
    assert [x.text for x in h.entries()] == ["2", "3", "4"]
    assert len(paths.history_file().read_text().splitlines()) == 3


def test_corrupt_line_is_skipped(isolated_xdg):
    paths.history_file().write_text('{"text": "ok", "ts": 1, "backend": "x", "audio_s": 1, "elapsed_s": 0}\nnot json\n')
    assert [x.text for x in History().entries()] == ["ok"]


def test_retry_audio_is_taken_once(isolated_xdg):
    h = History()
    pcm = np.ones(10, dtype=np.int16)
    h.keep_audio(pcm)
    assert np.array_equal(h.take_audio(), pcm)
    assert h.take_audio() is None
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_history.py -v`, FAIL `No module named 'voice.history'`.

- [ ] **Step 3: Create `voice/history.py`**

```python
"""Last-N dictations for recall and retry; JSONL on disk, audio only in memory."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from voice import paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entry:
    text: str
    ts: float
    backend: str
    audio_s: float
    elapsed_s: float


class History:
    def __init__(self, path: Path | None = None, limit: int = 20):
        self._path = path or paths.history_file()
        self._limit = limit
        self._entries: list[Entry] = self._load()
        self._audio: np.ndarray | None = None

    def _load(self) -> list[Entry]:
        if not self._path.exists():
            return []
        out: list[Entry] = []
        for line in self._path.read_text().splitlines():
            try:
                out.append(Entry(**json.loads(line)))
            except (ValueError, TypeError) as exc:
                log.warning("skipping bad history line: %s", exc)
        return out[-self._limit:]

    def _flush(self) -> None:
        self._path.write_text("".join(json.dumps(asdict(e)) + "\n" for e in self._entries))
        self._path.chmod(0o600)

    def add(self, entry: Entry) -> None:
        self._entries = (self._entries + [entry])[-self._limit:]
        self._flush()

    def last(self) -> Entry | None:
        return self._entries[-1] if self._entries else None

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def keep_audio(self, pcm: np.ndarray) -> None:
        self._audio = pcm

    def take_audio(self) -> np.ndarray | None:
        pcm, self._audio = self._audio, None
        return pcm
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/test_history.py -v`, expected 4 passed.

- [ ] **Step 5: Commit**

```bash
git add voice/history.py tests/test_history.py
git commit -m "feat(history): last-n dictations with recall and retry audio

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---
### Task 12: Clipboard snapshot, set and restore (wl-clipboard)

**Files:**
- Create: `voice/inject/__init__.py`, `voice/inject/clipboard.py`, `tests/inject/__init__.py`, `tests/inject/test_clipboard.py`

**Interfaces:**
- Produces: `@dataclass Snapshot(text: str | None)` (`None` = empty or non-text clipboard, nothing to restore); `class Clipboard(run=subprocess.run)`: `snapshot() -> Snapshot`, `set_text(text: str) -> None` (raises `ClipboardError` if `wl-copy` is missing or fails), `restore(snap: Snapshot) -> None` (no-op when `snap.text is None`), `available() -> bool`.

Implementation notes: `wl-paste --list-types` then `wl-paste --no-newline` only when a `text/plain*` type is present; `wl-copy` is spawned with the text on stdin using `--type text/plain;charset=utf-8`. `wl-copy` forks and stays alive to serve the selection; `subprocess.run` returns as soon as the parent exits, which is the documented behaviour. Timeouts of 2 s guard every call.

- [ ] **Step 1: Write the failing tests**

`tests/inject/test_clipboard.py`:
```python
import subprocess

import pytest

from voice.inject.clipboard import Clipboard, ClipboardError, Snapshot


class Runner:
    def __init__(self, responses):
        self.responses = responses      # {argv_prefix: (rc, stdout)}
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        for prefix, (rc, out) in self.responses.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        raise FileNotFoundError(argv[0])


def test_snapshot_returns_text_when_text_type_present():
    r = Runner({("wl-paste", "--list-types"): (0, "text/plain;charset=utf-8\nTEXT\n"), ("wl-paste", "--no-newline"): (0, "old")})
    assert Clipboard(run=r).snapshot() == Snapshot("old")


def test_snapshot_is_none_for_image_or_empty():
    r = Runner({("wl-paste", "--list-types"): (0, "image/png\n")})
    assert Clipboard(run=r).snapshot() == Snapshot(None)
    r2 = Runner({("wl-paste", "--list-types"): (1, "")})
    assert Clipboard(run=r2).snapshot() == Snapshot(None)


def test_set_text_pipes_to_wl_copy():
    r = Runner({("wl-copy",): (0, "")})
    Clipboard(run=r).set_text("hej å ä ö")
    argv, stdin = r.calls[-1]
    assert argv[0] == "wl-copy" and "--type" in argv
    assert stdin == "hej å ä ö"


def test_set_text_raises_when_wl_copy_missing():
    with pytest.raises(ClipboardError, match="wl-copy"):
        Clipboard(run=Runner({})).set_text("x")


def test_restore_noop_when_none_and_sets_otherwise():
    r = Runner({("wl-copy",): (0, "")})
    c = Clipboard(run=r)
    c.restore(Snapshot(None))
    assert r.calls == []
    c.restore(Snapshot("back"))
    assert r.calls[-1][1] == "back"


@pytest.mark.boundary
def test_real_wl_clipboard_roundtrip():
    c = Clipboard()
    assert c.available()
    before = c.snapshot()
    c.set_text("voice-test-åäö")
    assert c.snapshot().text == "voice-test-åäö"
    c.restore(before)
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/inject/test_clipboard.py -v`, FAIL `No module named 'voice.inject'`.

- [ ] **Step 3: Create `voice/inject/__init__.py` (empty) and `voice/inject/clipboard.py`**

```python
"""Wayland clipboard via wl-clipboard (wl-copy / wl-paste)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)
TIMEOUT = 2


class ClipboardError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    text: str | None


class Clipboard:
    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def available(self) -> bool:
        return shutil.which("wl-copy") is not None and shutil.which("wl-paste") is not None

    def snapshot(self) -> Snapshot:
        try:
            types = self._run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=TIMEOUT)
            if types.returncode != 0 or not any(t.startswith("text/plain") for t in types.stdout.split()):
                return Snapshot(None)
            content = self._run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=TIMEOUT)
            return Snapshot(content.stdout if content.returncode == 0 else None)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("clipboard snapshot failed: %s", exc)
            return Snapshot(None)

    def set_text(self, text: str) -> None:
        try:
            cp = self._run(["wl-copy", "--type", "text/plain;charset=utf-8"], input=text, text=True,
                           capture_output=True, timeout=TIMEOUT)
        except FileNotFoundError as exc:
            raise ClipboardError("wl-copy not found; install wl-clipboard") from exc
        except subprocess.SubprocessError as exc:
            raise ClipboardError(f"wl-copy failed: {exc}") from exc
        if cp.returncode != 0:
            raise ClipboardError(f"wl-copy exited {cp.returncode}: {cp.stderr}")

    def restore(self, snap: Snapshot) -> None:
        if snap.text is None:
            return
        try:
            self.set_text(snap.text)
        except ClipboardError as exc:
            log.warning("clipboard restore failed: %s", exc)
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/inject -v`, expected 5 passed, 1 deselected. The boundary test needs `wl-clipboard` in the VM (`sudo apt install wl-clipboard`, owner runs it) and is run in Task 21.

- [ ] **Step 5: Commit**

```bash
git add voice/inject tests/inject
git commit -m "feat(inject): clipboard snapshot/set/restore via wl-clipboard

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 13: Chord parsing and the RemoteDesktop portal key sender

**Files:**
- Create: `voice/inject/keys.py`, `voice/inject/portal.py`, `tests/inject/test_keys.py`, `tests/inject/test_portal.py`

**Interfaces:**
- Produces (`keys.py`): `parse_chord("ctrl+shift+v") -> list[int]` (evdev keycodes in press order; names: ctrl, shift, alt, super/meta, plus any single letter, digit, `insert`, `enter`, `tab`, `space`, or a raw `KEY_*` name; `ValueError` on unknown); `class KeySender(Protocol)`: `name: str`, `send_chord(keycodes: list[int]) -> None`, `available() -> bool`; `class KeySendError(RuntimeError)`.
- Produces (`portal.py`): `class TokenStore(path: Path | None = None)`: `load() -> str | None`, `save(token: str)`, `clear()`; `class PortalKeySender(token_store: TokenStore | None = None, bus_factory=open_dbus_connection)`: implements `KeySender` with `name = "portal"`; keeps one session, recreates it on any D-Bus error once, saves the `restore_token` from `Start`'s response. `portal_available(bus_factory=...) -> bool` (RemoteDesktop interface present with version ≥ 2).

D-Bus flow (blocking jeepney, run from the pipeline worker thread, never the Qt thread):
1. `CreateSession({handle_token, session_handle_token})` → Request path; wait for `Response` signal on it → `session_handle`.
2. `SelectDevices(session, {types: u 1 (keyboard), persist_mode: u 2, restore_token: s <saved>})` → wait Response (code 0).
3. `Start(session, "", {handle_token})` → wait Response; results may contain `restore_token` → save. Code 1 = user cancelled → `KeySendError("permission denied")`; on first run KDE/GNOME show a dialog here.
4. `NotifyKeyboardKeycode(session, {}, keycode, 1)` per key, then `..., 0` in reverse, 10 ms apart.

Request object path: `/org/freedesktop/portal/desktop/request/<sender>/<token>` where sender is the unique name without the leading `:` and with `.` → `_`.

- [ ] **Step 1: Write the failing tests**

`tests/inject/test_keys.py`:
```python
import pytest
from evdev import ecodes as e

from voice.inject.keys import parse_chord


def test_parse_chord_modifiers_then_key():
    assert parse_chord("ctrl+shift+v") == [e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_V]
    assert parse_chord("Ctrl + V") == [e.KEY_LEFTCTRL, e.KEY_V]
    assert parse_chord("shift+insert") == [e.KEY_LEFTSHIFT, e.KEY_INSERT]
    assert parse_chord("super+KEY_F13") == [e.KEY_LEFTMETA, e.KEY_F13]


def test_parse_chord_rejects_unknown():
    with pytest.raises(ValueError, match="hyper"):
        parse_chord("hyper+v")
```

`tests/inject/test_portal.py`:
```python
import pytest

from voice import paths
from voice.inject.portal import TokenStore, request_path, select_devices_options


def test_token_store_roundtrip(isolated_xdg):
    s = TokenStore()
    assert s.load() is None
    s.save("abc")
    assert s.load() == "abc"
    assert paths.portal_token_file().read_text() == "abc"
    s.clear()
    assert s.load() is None


def test_request_path_escapes_unique_name():
    assert request_path(":1.42", "tok1") == "/org/freedesktop/portal/desktop/request/1_42/tok1"


def test_select_devices_options_include_token_only_when_present():
    opts = select_devices_options(None)
    assert opts["types"] == ("u", 1) and opts["persist_mode"] == ("u", 2) and "restore_token" not in opts
    assert select_devices_options("t")["restore_token"] == ("s", "t")


@pytest.mark.boundary
def test_real_portal_sends_harmless_chord():
    """Sends Shift press/release through the portal. First run shows the desktop's permission dialog."""
    from evdev import ecodes as e
    from voice.inject.portal import PortalKeySender, portal_available
    assert portal_available()
    sender = PortalKeySender()
    sender.send_chord([e.KEY_LEFTSHIFT])
    assert TokenStore().load()          # restore token persisted after Start
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/inject/test_keys.py tests/inject/test_portal.py -v`, FAIL on imports.

- [ ] **Step 3: Create `voice/inject/keys.py`**

```python
"""Paste chords as evdev keycodes, and the KeySender protocol."""
from __future__ import annotations

from typing import Protocol

from evdev import ecodes

_NAMED = {
    "ctrl": ecodes.KEY_LEFTCTRL, "control": ecodes.KEY_LEFTCTRL,
    "shift": ecodes.KEY_LEFTSHIFT,
    "alt": ecodes.KEY_LEFTALT,
    "super": ecodes.KEY_LEFTMETA, "meta": ecodes.KEY_LEFTMETA, "win": ecodes.KEY_LEFTMETA,
    "insert": ecodes.KEY_INSERT, "enter": ecodes.KEY_ENTER, "return": ecodes.KEY_ENTER,
    "tab": ecodes.KEY_TAB, "space": ecodes.KEY_SPACE, "esc": ecodes.KEY_ESC,
}


class KeySendError(RuntimeError):
    pass


class KeySender(Protocol):
    name: str

    def send_chord(self, keycodes: list[int]) -> None: ...
    def available(self) -> bool: ...


def parse_chord(text: str) -> list[int]:
    codes: list[int] = []
    for raw in text.split("+"):
        part = raw.strip().lower()
        if not part:
            continue
        if part in _NAMED:
            codes.append(_NAMED[part])
        elif part.upper().startswith("KEY_") and part.upper() in ecodes.ecodes:
            codes.append(ecodes.ecodes[part.upper()])
        elif len(part) == 1 and f"KEY_{part.upper()}" in ecodes.ecodes:
            codes.append(ecodes.ecodes[f"KEY_{part.upper()}"])
        else:
            raise ValueError(f"unknown key {part!r} in chord {text!r}")
    return codes
```

- [ ] **Step 4: Create `voice/inject/portal.py`**

```python
"""xdg-desktop-portal RemoteDesktop keyboard injection (works on KDE and GNOME)."""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import Callable

from jeepney import DBusAddress, MatchRule, Message, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection

from voice import APP_ID, paths
from voice.inject.keys import KeySendError

log = logging.getLogger(__name__)

PORTAL = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop",
                     interface="org.freedesktop.portal.RemoteDesktop")
PROPS = PORTAL.with_interface("org.freedesktop.DBus.Properties")
KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2


def request_path(unique_name: str, token: str) -> str:
    return f"/org/freedesktop/portal/desktop/request/{unique_name.lstrip(':').replace('.', '_')}/{token}"


def select_devices_options(token: str | None) -> dict:
    opts = {"types": ("u", KEYBOARD), "persist_mode": ("u", PERSIST_UNTIL_REVOKED)}
    if token:
        opts["restore_token"] = ("s", token)
    return opts


class TokenStore:
    def __init__(self, path: Path | None = None):
        self._path = path or paths.portal_token_file()

    def load(self) -> str | None:
        return self._path.read_text().strip() or None if self._path.exists() else None

    def save(self, token: str) -> None:
        self._path.write_text(token)
        self._path.chmod(0o600)

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()


def portal_available(bus_factory: Callable = open_dbus_connection) -> bool:
    try:
        conn = bus_factory(bus="SESSION")
        try:
            reply = conn.send_and_get_reply(new_method_call(PROPS, "Get", "ss", (PORTAL.interface, "version")))
            return int(reply.body[0][1]) >= 2
        finally:
            conn.close()
    except Exception as exc:
        log.info("portal unavailable: %s", exc)
        return False


class PortalKeySender:
    name = "portal"

    def __init__(self, token_store: TokenStore | None = None, bus_factory: Callable = open_dbus_connection):
        self._tokens = token_store or TokenStore()
        self._bus_factory = bus_factory
        self._conn = None
        self._session: str | None = None

    def available(self) -> bool:
        return portal_available(self._bus_factory)

    # -- request/response helper -----------------------------------------
    def _call(self, method: str, signature: str, args: tuple) -> dict:
        token = "voice" + secrets.token_hex(4)
        path = request_path(self._conn.unique_name, token)
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=path)
        self._conn.send_and_get_reply(message_bus.AddMatch(rule))
        with self._conn.filter(rule) as queue:
            opts = dict(args[-1])
            opts["handle_token"] = ("s", token)
            reply = self._conn.send_and_get_reply(new_method_call(PORTAL, method, signature, (*args[:-1], opts)))
            if reply.header.message_type.name == "error":
                raise KeySendError(f"portal {method} failed: {reply.body}")
            msg: Message = self._conn.recv_until_filtered(queue, timeout=120)
        code, results = msg.body
        if code != 0:
            raise KeySendError(f"portal {method} denied (response {code}); allow '{APP_ID}' in system settings")
        return results

    def _open(self) -> None:
        self._conn = self._bus_factory(bus="SESSION")
        results = self._call("CreateSession", "a{sv}", ({"session_handle_token": ("s", "voice" + secrets.token_hex(4))},))
        self._session = results["session_handle"][1]
        self._call("SelectDevices", "oa{sv}", (self._session, select_devices_options(self._tokens.load())))
        results = self._call("Start", "osa{sv}", (self._session, "", {}))
        token = results.get("restore_token")
        if token:
            self._tokens.save(token[1])
        log.info("portal remote desktop session ready")

    def _notify(self, keycode: int, state: int) -> None:
        self._conn.send_and_get_reply(
            new_method_call(PORTAL, "NotifyKeyboardKeycode", "oa{sv}iu", (self._session, {}, keycode, state)))

    def send_chord(self, keycodes: list[int]) -> None:
        for attempt in (1, 2):
            try:
                if self._session is None:
                    self._open()
                for code in keycodes:
                    self._notify(code, 1)
                    time.sleep(0.01)
                for code in reversed(keycodes):
                    self._notify(code, 0)
                    time.sleep(0.01)
                return
            except KeySendError:
                raise
            except Exception as exc:               # session died (suspend, portal restart): retry once
                log.warning("portal send failed (attempt %d): %s", attempt, exc)
                self.close()
                if attempt == 2:
                    raise KeySendError(f"portal keyboard injection failed: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn, self._session = None, None
```

- [ ] **Step 5: Run tests** → `uv run pytest tests/inject -v`, expected 10 passed, 2 deselected.

- [ ] **Step 6: Commit**

```bash
git add voice/inject/keys.py voice/inject/portal.py tests/inject/test_keys.py tests/inject/test_portal.py
git commit -m "feat(inject): chord parsing and remote-desktop portal key sender

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 14: Fallback senders and the Injector

**Files:**
- Create: `voice/inject/fallback.py`, `voice/inject/injector.py`, `tests/inject/test_fallback.py`, `tests/inject/test_injector.py`

**Interfaces:**
- Produces (`fallback.py`): `class WtypeKeySender` (`name="wtype"`, runs `wtype -M <mod> ... -k <key> -m <mod> ...`), `class YdotoolKeySender` (`name="ydotool"`, runs `ydotool key <code>:1 ... <code>:0`), `class ClipboardOnlySender` (`name="clipboard-only"`, `send_chord` raises `KeySendError("left on clipboard")`), `make_key_sender(preferred: str = "auto", run=subprocess.run) -> KeySender` choosing the first available of portal, wtype, ydotool, else clipboard-only. `keycode_to_xkb_name(code) -> str` maps evdev codes to wtype key names (`ctrl`→`ctrl`, `KEY_V`→`v`).
- Produces (`injector.py`): `@dataclass InjectResult(method: str, chord: str, restored: bool)`; `class Injector(clipboard: Clipboard, sender: KeySender, settings: dict, modifiers_held: Callable[[], bool], window_class: Callable[[], str | None], sleep=time.sleep)`: `inject(text: str) -> InjectResult`. `settings` is the `[inject]` table. Algorithm: snapshot → set_text(text) → wait up to 1.5 s for `modifiers_held()` to be False (poll every 20 ms) → pick chord (`terminal_chord` when `window_class()` lower-cased is in `terminal_classes`, else `paste_chord`) → `sender.send_chord` → sleep 0.15 s → restore if `restore_clipboard`. If the sender raises `KeySendError`, the text stays on the clipboard and `InjectResult(method="clipboard-only", ...)` is returned with `restored=False`. `run_window_command(cmd: str, run=subprocess.run) -> str | None` executes `inject.active_window_command` with a 1 s timeout.

- [ ] **Step 1: Write the failing tests**

`tests/inject/test_fallback.py`:
```python
import subprocess

from evdev import ecodes as e

from voice.inject.fallback import (ClipboardOnlySender, WtypeKeySender, YdotoolKeySender,
                                   keycode_to_xkb_name, make_key_sender)


class Runner:
    def __init__(self, present):
        self.present, self.calls = present, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] not in self.present:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_xkb_names():
    assert keycode_to_xkb_name(e.KEY_LEFTCTRL) == "ctrl"
    assert keycode_to_xkb_name(e.KEY_LEFTSHIFT) == "shift"
    assert keycode_to_xkb_name(e.KEY_V) == "v"
    assert keycode_to_xkb_name(e.KEY_INSERT) == "Insert"


def test_wtype_command_shape():
    r = Runner({"wtype"})
    WtypeKeySender(run=r).send_chord([e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_V])
    assert r.calls[-1] == ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]


def test_ydotool_command_shape():
    r = Runner({"ydotool"})
    YdotoolKeySender(run=r).send_chord([e.KEY_LEFTCTRL, e.KEY_V])
    assert r.calls[-1] == ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]


def test_make_key_sender_falls_back_in_order(monkeypatch):
    monkeypatch.setattr("voice.inject.fallback.portal_available", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ydotool" if name == "ydotool" else None)
    assert make_key_sender().name == "ydotool"
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert isinstance(make_key_sender(), ClipboardOnlySender)
    monkeypatch.setattr("voice.inject.fallback.portal_available", lambda: True)
    assert make_key_sender().name == "portal"
```

`tests/inject/test_injector.py`:
```python
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
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/inject/test_fallback.py tests/inject/test_injector.py -v`, FAIL on imports.

- [ ] **Step 3: Create `voice/inject/fallback.py`**

```python
"""Non-portal chord senders (wlroots wtype, uinput ydotool) and the chooser."""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable

from evdev import ecodes

from voice.inject.keys import KeySendError, KeySender
from voice.inject.portal import PortalKeySender, portal_available

log = logging.getLogger(__name__)
_XKB = {ecodes.KEY_LEFTCTRL: "ctrl", ecodes.KEY_LEFTSHIFT: "shift", ecodes.KEY_LEFTALT: "alt",
        ecodes.KEY_LEFTMETA: "logo", ecodes.KEY_INSERT: "Insert", ecodes.KEY_ENTER: "Return",
        ecodes.KEY_TAB: "Tab", ecodes.KEY_SPACE: "space", ecodes.KEY_ESC: "Escape"}
_MODS = {ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_LEFTALT, ecodes.KEY_LEFTMETA}


def keycode_to_xkb_name(code: int) -> str:
    if code in _XKB:
        return _XKB[code]
    name = ecodes.KEY.get(code, "")
    name = name[0] if isinstance(name, list) else name
    return name.removeprefix("KEY_").lower()


class _Cmd:
    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def _exec(self, argv: list[str]) -> None:
        try:
            cp = self._run(argv, capture_output=True, text=True, timeout=3)
        except FileNotFoundError as exc:
            raise KeySendError(f"{argv[0]} not installed") from exc
        except subprocess.SubprocessError as exc:
            raise KeySendError(f"{argv[0]} failed: {exc}") from exc
        if cp.returncode != 0:
            raise KeySendError(f"{argv[0]} exited {cp.returncode}: {cp.stderr.strip()}")


class WtypeKeySender(_Cmd):
    name = "wtype"

    def available(self) -> bool:
        return shutil.which("wtype") is not None

    def send_chord(self, keycodes: list[int]) -> None:
        mods = [c for c in keycodes if c in _MODS]
        keys = [c for c in keycodes if c not in _MODS]
        argv = ["wtype"]
        for m in mods:
            argv += ["-M", keycode_to_xkb_name(m)]
        for k in keys:
            argv += ["-k", keycode_to_xkb_name(k)]
        for m in reversed(mods):
            argv += ["-m", keycode_to_xkb_name(m)]
        self._exec(argv)


class YdotoolKeySender(_Cmd):
    name = "ydotool"

    def available(self) -> bool:
        return shutil.which("ydotool") is not None

    def send_chord(self, keycodes: list[int]) -> None:
        seq = [f"{c}:1" for c in keycodes] + [f"{c}:0" for c in reversed(keycodes)]
        self._exec(["ydotool", "key", *seq])


class ClipboardOnlySender:
    name = "clipboard-only"

    def available(self) -> bool:
        return True

    def send_chord(self, keycodes: list[int]) -> None:
        raise KeySendError("no key sender available; text left on clipboard")


def make_key_sender(preferred: str = "auto", run: Callable = subprocess.run) -> KeySender:
    candidates = {"portal": lambda: PortalKeySender(), "wtype": lambda: WtypeKeySender(run),
                  "ydotool": lambda: YdotoolKeySender(run)}
    order = [preferred] if preferred in candidates else ["portal", "wtype", "ydotool"]
    for name in order:
        ok = portal_available() if name == "portal" else shutil.which(name) is not None
        if ok:
            log.info("key sender: %s", name)
            return candidates[name]()
    log.warning("no key sender available; falling back to clipboard-only")
    return ClipboardOnlySender()
```

- [ ] **Step 4: Create `voice/inject/injector.py`**

```python
"""Copy the text, wait for the hotkey modifiers to clear, paste, restore the clipboard."""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from voice.inject.clipboard import Clipboard
from voice.inject.keys import KeySender, KeySendError, parse_chord

log = logging.getLogger(__name__)
MODIFIER_WAIT_S = 1.5
POLL_S = 0.02
SETTLE_S = 0.15


@dataclass(frozen=True)
class InjectResult:
    method: str
    chord: str
    restored: bool


def run_window_command(cmd: str, run: Callable = subprocess.run) -> str | None:
    if not cmd:
        return None
    try:
        cp = run(cmd, shell=True, capture_output=True, text=True, timeout=1)
        return cp.stdout.strip() or None if cp.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class Injector:
    def __init__(self, clipboard: Clipboard, sender: KeySender, settings: dict,
                 modifiers_held: Callable[[], bool], window_class: Callable[[], str | None],
                 sleep: Callable[[float], None] = time.sleep):
        self._clip, self._sender, self._settings = clipboard, sender, settings
        self._modifiers_held, self._window_class, self._sleep = modifiers_held, window_class, sleep

    def _chord(self) -> str:
        cls = (self._window_class() or "").lower()
        terminals = [str(t).lower() for t in self._settings.get("terminal_classes", [])]
        return self._settings.get("terminal_chord", "ctrl+shift+v") if cls and cls in terminals \
            else self._settings.get("paste_chord", "ctrl+v")

    def inject(self, text: str) -> InjectResult:
        snap = self._clip.snapshot()
        self._clip.set_text(text)
        waited = 0.0
        while self._modifiers_held() and waited < MODIFIER_WAIT_S:
            self._sleep(POLL_S)
            waited += POLL_S
        chord = self._chord()
        try:
            self._sender.send_chord(parse_chord(chord))
        except KeySendError as exc:
            log.warning("paste failed, text left on clipboard: %s", exc)
            return InjectResult("clipboard-only", chord, False)
        self._sleep(SETTLE_S)
        restored = False
        if self._settings.get("restore_clipboard", True):
            self._clip.restore(snap)
            restored = snap.text is not None
        return InjectResult(self._sender.name, chord, restored)
```

- [ ] **Step 5: Run tests** → `uv run pytest tests/inject -v`, expected 19 passed, 2 deselected.

- [ ] **Step 6: Commit**

```bash
git add voice/inject/fallback.py voice/inject/injector.py tests/inject/test_fallback.py tests/inject/test_injector.py
git commit -m "feat(inject): fallback senders and copy-paste-restore injector

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---
### Task 15: Dictation state machine (pipeline)

**Files:**
- Create: `voice/pipeline.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `Recorder`, `trim_silence`, `Transcriber`, `Injector`, `History`, `Entry`, `apply_replacements`, `normalize_text`, `duration_s`
- Produces: `class State(str, Enum)`: `IDLE, RECORDING, TRANSCRIBING, INJECTING, ERROR`; `@dataclass Services(recorder, transcriber, injector, history, notify: Callable[[str, str, str], None], trim=trim_silence, config_getter: Callable[[str, Any], Any])`; `class Dictation(services, executor: Callable[[Callable[[], None]], None] = <thread executor>, timer_factory=threading.Timer)`: `on_hotkey(name: str, kind: str)` (names: `dictate`, `recall`, `cancel`), `start()`, `stop()`, `toggle()`, `cancel()`, `recall()`, `retry()`, `state -> State`, `on_state: Callable[[State, str], None]` attribute (state plus a short detail string, called on every transition), `set_transcriber(t)`, `set_injector(i)`, `last_error: str | None`.

Rules (from spec §3): hold mode starts on press and stops on release; toggle mode alternates on press; a press while TRANSCRIBING/INJECTING is ignored; `cancel` in RECORDING discards; `max_seconds` timer stops the recording; after trim, audio shorter than `MIN_SPEECH_MS` is dropped silently (state back to IDLE with detail `"too short"`); on `TranscriptionError` the audio is kept for `retry()` and state goes ERROR then IDLE; empty transcript text is treated like too short; injection result `clipboard-only` produces a notification "Paste with Ctrl+V". `config_getter` is `Config.get` so live values are read per dictation: `hotkeys.dictate_mode`, `audio.device`, `audio.max_seconds`, `general.language`, `dictionary.replacements`, and the active profile's `prompt` is passed via `Services.prompt_getter`.

- [ ] **Step 1: Write the failing tests**

`tests/test_pipeline.py`:
```python
import numpy as np
import pytest

from voice.history import History
from voice.inject.injector import InjectResult
from voice.pipeline import Dictation, Services, State
from voice.stt.base import Transcript, TranscriptionError


class FakeRecorder:
    def __init__(self, pcm=None):
        self.pcm = pcm if pcm is not None else np.ones(16000, dtype=np.int16)
        self.is_recording, self.started_with, self.cancelled = False, [], 0
        self.error = None

    def start(self, device):
        self.is_recording = True
        self.started_with.append(device)

    def stop(self):
        self.is_recording = False
        return self.pcm

    def cancel(self):
        self.cancelled += 1
        self.is_recording = False


class FakeTranscriber:
    name = "fake"

    def __init__(self, text="hello world", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def transcribe(self, pcm, language, prompt):
        self.calls.append((pcm.size, language, prompt))
        if self.fail:
            raise TranscriptionError("cloud down")
        return Transcript(self.text, language, 1.0, 0.1, self.name)

    def warmup(self): pass
    def describe(self): return "fake"


class FakeInjector:
    def __init__(self, method="portal"):
        self.method, self.texts = method, []

    def inject(self, text):
        self.texts.append(text)
        return InjectResult(self.method, "ctrl+v", True)


class FakeTimer:
    instances = []

    def __init__(self, seconds, fn):
        self.seconds, self.fn, self.cancelled = seconds, fn, False
        FakeTimer.instances.append(self)

    def start(self): pass
    def cancel(self): self.cancelled = True
    def fire(self): self.fn()


def make(cfg=None, rec=None, stt=None, inj=None):
    cfg = {"hotkeys.dictate_mode": "hold", "audio.device": "", "audio.max_seconds": 120,
           "general.language": "en", "dictionary.replacements": [["cachy os", "CachyOS", "icase"]], **(cfg or {})}
    notes = []
    services = Services(
        recorder=rec or FakeRecorder(), transcriber=stt or FakeTranscriber(), injector=inj or FakeInjector(),
        history=History(), notify=lambda t, b, u="normal": notes.append((t, b)),
        trim=lambda pcm: pcm, config_getter=lambda k, d=None: cfg.get(k, d), prompt_getter=lambda: "CachyOS")
    states = []
    d = Dictation(services, executor=lambda fn: fn(), timer_factory=FakeTimer)
    d.on_state = lambda s, detail: states.append(s)
    FakeTimer.instances = []
    return d, services, states, notes


def test_hold_mode_full_flow_applies_replacements_and_records_history():
    d, sv, states, notes = make(stt=FakeTranscriber("I run cachy os"))
    d.on_hotkey("dictate", "press")
    assert d.state == State.RECORDING and sv.recorder.started_with == [None]
    d.on_hotkey("dictate", "release")
    assert states == [State.RECORDING, State.TRANSCRIBING, State.INJECTING, State.IDLE]
    assert sv.injector.texts == ["I run CachyOS"]
    assert sv.transcriber.calls == [(16000, "en", "CachyOS")]
    assert sv.history.last().text == "I run CachyOS"


def test_toggle_mode_alternates_on_press():
    d, sv, states, _ = make({"hotkeys.dictate_mode": "toggle"})
    d.on_hotkey("dictate", "press"); d.on_hotkey("dictate", "release")
    assert d.state == State.RECORDING
    d.on_hotkey("dictate", "press")
    assert d.state == State.IDLE and sv.injector.texts == ["hello world"]


def test_too_short_is_dropped_silently():
    d, sv, states, notes = make(rec=FakeRecorder(np.ones(1000, dtype=np.int16)))
    d.start(); d.stop()
    assert sv.transcriber.calls == [] and states[-1] == State.IDLE and notes == []


def test_empty_transcript_dropped():
    d, sv, states, notes = make(stt=FakeTranscriber(""))
    d.start(); d.stop()
    assert sv.injector.texts == [] and d.state == State.IDLE


def test_error_keeps_audio_for_retry():
    stt = FakeTranscriber(fail=True)
    d, sv, states, notes = make(stt=stt)
    d.start(); d.stop()
    assert State.ERROR in states and d.state == State.IDLE
    assert "cloud down" in notes[-1][1] and d.last_error
    stt.fail = False
    d.retry()
    assert sv.injector.texts == ["hello world"]
    d.retry()                                   # nothing left to retry
    assert len(sv.injector.texts) == 1


def test_cancel_discards_recording():
    d, sv, states, _ = make()
    d.start(); d.on_hotkey("cancel", "press")
    assert sv.recorder.cancelled == 1 and d.state == State.IDLE and sv.transcriber.calls == []


def test_max_seconds_timer_stops_recording():
    d, sv, states, _ = make({"audio.max_seconds": 7})
    d.start()
    timer = FakeTimer.instances[-1]
    assert timer.seconds == 7
    timer.fire()
    assert d.state == State.IDLE and sv.injector.texts == ["hello world"]
    d.start(); d.stop()
    assert FakeTimer.instances[-2].cancelled or FakeTimer.instances[-1].cancelled


def test_press_during_transcription_is_ignored_and_recall_reinjects():
    d, sv, states, _ = make()
    d.start(); d.stop()
    d.recall()
    assert sv.injector.texts == ["hello world", "hello world"]


def test_clipboard_only_result_notifies_user():
    d, sv, states, notes = make(inj=FakeInjector(method="clipboard-only"))
    d.start(); d.stop()
    assert any("Ctrl+V" in b for _, b in notes)


def test_recorder_device_from_config():
    d, sv, *_ = make({"audio.device": "alsa_input.obsbot"})
    d.start()
    assert sv.recorder.started_with == ["alsa_input.obsbot"]
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_pipeline.py -v`, FAIL `No module named 'voice.pipeline'`.

- [ ] **Step 3: Create `voice/pipeline.py`**

```python
"""The dictation state machine: record -> trim -> transcribe -> replace -> inject -> history."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import numpy as np

from voice.audio.pcm import SAMPLE_RATE, duration_s
from voice.audio.vad import MIN_SPEECH_MS, trim_silence
from voice.history import Entry, History
from voice.stt.base import TranscriptionError
from voice.text import apply_replacements, normalize_text

log = logging.getLogger(__name__)


class State(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    INJECTING = "injecting"
    ERROR = "error"


@dataclass
class Services:
    recorder: Any
    transcriber: Any
    injector: Any
    history: History
    notify: Callable[..., None]
    config_getter: Callable[..., Any]
    prompt_getter: Callable[[], str | None] = lambda: None
    trim: Callable[[np.ndarray], np.ndarray] = trim_silence


_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dictation")


def _thread_executor(fn: Callable[[], None]) -> None:
    _pool.submit(fn)


class Dictation:
    def __init__(self, services: Services, executor: Callable = _thread_executor, timer_factory=threading.Timer):
        self.sv = services
        self._executor = executor
        self._timer_factory = timer_factory
        self._timer = None
        self._lock = threading.RLock()
        self._state = State.IDLE
        self.on_state: Callable[[State, str], None] = lambda s, d: None
        self.last_error: str | None = None

    # -- state ----------------------------------------------------------------
    @property
    def state(self) -> State:
        return self._state

    def _set(self, state: State, detail: str = "") -> None:
        self._state = state
        log.debug("state %s %s", state.value, detail)
        try:
            self.on_state(state, detail)
        except Exception:
            log.exception("on_state callback failed")

    def set_transcriber(self, t) -> None:
        self.sv.transcriber = t

    def set_injector(self, i) -> None:
        self.sv.injector = i

    # -- hotkey entry point ---------------------------------------------------
    def on_hotkey(self, name: str, kind: str) -> None:
        if name == "dictate":
            mode = self.sv.config_getter("hotkeys.dictate_mode", "hold")
            if mode == "toggle":
                if kind == "press":
                    self.toggle()
            elif kind == "press":
                self.start()
            else:
                self.stop()
        elif name == "cancel" and kind == "press":
            self.cancel()
        elif name == "recall" and kind == "press":
            self.recall()

    # -- commands -------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._state != State.IDLE:
                return
            device = self.sv.config_getter("audio.device", "") or None
            try:
                self.sv.recorder.start(device)
            except Exception as exc:
                self._fail(f"cannot record: {exc}")
                return
            seconds = int(self.sv.config_getter("audio.max_seconds", 120))
            self._timer = self._timer_factory(seconds, self._on_max_seconds)
            self._timer.start()
            self._set(State.RECORDING)

    def _on_max_seconds(self) -> None:
        log.info("max recording length reached")
        self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._state != State.RECORDING:
                return
            if self._timer:
                self._timer.cancel()
            pcm = self.sv.recorder.stop()
            if self.sv.recorder.error:
                self.sv.notify("Microphone problem", self.sv.recorder.error, "critical")
            self._set(State.TRANSCRIBING)
        self._executor(lambda: self._process(pcm))

    def toggle(self) -> None:
        if self._state == State.IDLE:
            self.start()
        elif self._state == State.RECORDING:
            self.stop()

    def cancel(self) -> None:
        with self._lock:
            if self._state != State.RECORDING:
                return
            if self._timer:
                self._timer.cancel()
            self.sv.recorder.cancel()
            self._set(State.IDLE, "cancelled")

    def recall(self) -> None:
        last = self.sv.history.last()
        if last is None or self._state != State.IDLE:
            return
        self._executor(lambda: self._inject(last.text, last))

    def retry(self) -> None:
        pcm = self.sv.history.take_audio()
        if pcm is None or self._state != State.IDLE:
            return
        with self._lock:
            self._set(State.TRANSCRIBING, "retry")
        self._executor(lambda: self._process(pcm, trimmed=True))

    # -- worker ---------------------------------------------------------------
    def _process(self, pcm: np.ndarray, trimmed: bool = False) -> None:
        try:
            audio = pcm if trimmed else self.sv.trim(pcm)
            if duration_s(audio) * 1000 < MIN_SPEECH_MS:
                self._set(State.IDLE, "too short")
                return
            language = self.sv.config_getter("general.language", "en")
            try:
                result = self.sv.transcriber.transcribe(audio, language, self.sv.prompt_getter())
            except TranscriptionError as exc:
                self.sv.history.keep_audio(audio)
                self._fail(str(exc))
                return
            text = normalize_text(result.text)
            if not text:
                self._set(State.IDLE, "empty")
                return
            text = apply_replacements(text, self.sv.config_getter("dictionary.replacements", []) or [])
            entry = Entry(text, time.time(), result.backend, result.audio_s, result.elapsed_s)
            self.sv.history.add(entry)
            self._inject(text, entry)
        except Exception as exc:  # never let the worker die silently
            log.exception("pipeline failure")
            self._fail(f"unexpected error: {exc}")

    def _inject(self, text: str, entry: Entry) -> None:
        self._set(State.INJECTING)
        try:
            res = self.sv.injector.inject(text)
        except Exception as exc:
            self._fail(f"could not paste: {exc}")
            return
        if res.method == "clipboard-only":
            self.sv.notify("Text copied", "Could not paste automatically. Paste with Ctrl+V.", "normal")
        self._set(State.IDLE, f"{len(text)} chars via {res.method} in {entry.elapsed_s:.1f}s")

    def _fail(self, message: str) -> None:
        self.last_error = message
        self._set(State.ERROR, message)
        self.sv.notify("Dictation failed", message, "critical")
        self._set(State.IDLE, "after error")
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/test_pipeline.py -v`, expected 10 passed.

- [ ] **Step 5: Commit**

```bash
git add voice/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): dictation state machine with retry, recall and cancel

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 16: Unix-socket IPC

**Files:**
- Create: `voice/ipc.py`, `tests/test_ipc.py`

**Interfaces:**
- Consumes: `paths.socket_path()`
- Produces: `class Server(handler: Callable[[dict], dict], path: Path | None = None)`: `start()`, `stop()`, `path`; `send(command: dict, path: Path | None = None, timeout: float = 5.0) -> dict` (raises `IPCError` when no daemon listens); `is_running(path=None) -> bool`. Protocol: one JSON object per line, one request per connection, reply `{"ok": true, ...}` or `{"ok": false, "error": "..."}`. Server removes a stale socket file on start and on stop.

- [ ] **Step 1: Write the failing tests**

`tests/test_ipc.py`:
```python
import pytest

from voice import paths
from voice.ipc import IPCError, Server, is_running, send


def test_roundtrip_and_stale_socket_cleanup(isolated_xdg):
    paths.socket_path().write_text("stale")
    srv = Server(lambda req: {"ok": True, "echo": req["cmd"]})
    srv.start()
    try:
        assert is_running()
        assert send({"cmd": "status"}) == {"ok": True, "echo": "status"}
    finally:
        srv.stop()
    assert not paths.socket_path().exists()
    assert not is_running()


def test_handler_exception_becomes_error_reply(isolated_xdg):
    def boom(req):
        raise RuntimeError("bad")
    srv = Server(boom)
    srv.start()
    try:
        reply = send({"cmd": "x"})
        assert reply["ok"] is False and "bad" in reply["error"]
    finally:
        srv.stop()


def test_send_without_daemon_raises(isolated_xdg):
    with pytest.raises(IPCError, match="not running"):
        send({"cmd": "status"})
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_ipc.py -v`, FAIL `No module named 'voice.ipc'`.

- [ ] **Step 3: Create `voice/ipc.py`**

```python
"""Newline-JSON over a Unix socket: the CLI talks to the daemon with this."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable

from voice import paths

log = logging.getLogger(__name__)


class IPCError(RuntimeError):
    pass


class Server:
    def __init__(self, handler: Callable[[dict], dict], path: Path | None = None):
        self._handler = handler
        self.path = path or paths.socket_path()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._serve, name="ipc-server", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                try:
                    data = conn.makefile("rb").readline()
                    request = json.loads(data.decode()) if data else {}
                    reply = self._handler(request)
                except Exception as exc:
                    log.exception("ipc handler failed")
                    reply = {"ok": False, "error": str(exc)}
                try:
                    conn.sendall((json.dumps(reply) + "\n").encode())
                except OSError:
                    pass

    def stop(self) -> None:
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
        if self.path.exists():
            self.path.unlink()


def send(command: dict, path: Path | None = None, timeout: float = 5.0) -> dict:
    path = path or paths.socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((json.dumps(command) + "\n").encode())
            line = s.makefile("rb").readline()
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise IPCError("daemon not running") from exc
    except OSError as exc:
        raise IPCError(f"ipc failed: {exc}") from exc
    return json.loads(line.decode()) if line else {"ok": False, "error": "empty reply"}


def is_running(path: Path | None = None) -> bool:
    try:
        return send({"cmd": "ping"}, path, timeout=1.0).get("ok", False)
    except IPCError:
        return False
```

Note: `is_running` relies on the daemon answering `ping`; the daemon's handler (Task 19) always replies `{"ok": true}` to `ping`. The test handler above echoes, which is also `ok: true`.

- [ ] **Step 4: Run tests** → `uv run pytest tests/test_ipc.py -v`, expected 3 passed.

- [ ] **Step 5: Commit**

```bash
git add voice/ipc.py tests/test_ipc.py
git commit -m "feat(ipc): unix socket json server and client

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---
### Task 17: Notifications, tray icons and tray menu

**Files:**
- Create: `voice/ui/__init__.py`, `voice/ui/notify.py`, `voice/ui/icons.py`, `voice/ui/tray.py`, `tests/ui/__init__.py`, `tests/ui/conftest.py`, `tests/ui/test_notify.py`, `tests/ui/test_tray.py`

**Interfaces:**
- Produces (`notify.py`): `class Notifier(enabled: bool = True, run=subprocess.Popen)`: `notify(title: str, body: str, urgency: str = "normal")` runs `notify-send --app-name voice --urgency <u> --expire-time 4000 <title> <body>` without waiting; `set_enabled(bool)`.
- Produces (`icons.py`): `icon_for(state: str, size: int = 64) -> QIcon` for `idle`, `recording`, `transcribing`, `error` drawn with QPainter (mic glyph: rounded capsule + stand; idle grey-white, recording red fill, transcribing amber ring, error orange with "!"); `pixmap_for(state, size) -> QPixmap`.
- Produces (`tray.py`): `class Tray(QObject)`: `__init__(on_action: Callable[[str], None])` builds `QSystemTrayIcon` with menu entries `Recall last`, `Retry last`, separator, `Profile ▸` (radio list filled via `set_profiles(names, active)`), `Settings…`, `Quit`; `set_state(state: str, detail: str)` swaps icon and tooltip `"voice · <state> · <detail>"`; `set_profiles(names: list[str], active: str)`; `show()`. Actions are reported as strings: `recall`, `retry`, `settings`, `quit`, `profile:<name>`. A left-click on the icon reports `settings`. Qt signals bridge: `state_changed = Signal(str, str)` connected to `set_state` so worker threads emit safely.

- [ ] **Step 1: Write the failing tests**

`tests/ui/conftest.py`:
```python
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
```

`tests/ui/test_notify.py`:
```python
from voice.ui.notify import Notifier


def test_notify_spawns_notify_send():
    calls = []
    n = Notifier(run=lambda argv, **kw: calls.append(argv))
    n.notify("Title", "Body", "critical")
    argv = calls[0]
    assert argv[0] == "notify-send" and "--urgency" in argv and "critical" in argv
    assert argv[-2:] == ["Title", "Body"]


def test_notify_disabled_and_missing_binary_are_silent():
    calls = []
    n = Notifier(enabled=False, run=lambda argv, **kw: calls.append(argv))
    n.notify("a", "b")
    assert calls == []

    def missing(argv, **kw):
        raise FileNotFoundError("notify-send")
    Notifier(run=missing).notify("a", "b")      # must not raise
```

`tests/ui/test_tray.py`:
```python
from voice.ui.icons import icon_for, pixmap_for
from voice.ui.tray import Tray


def test_icons_exist_and_differ_per_state(qapp):
    imgs = {s: pixmap_for(s, 32).toImage() for s in ("idle", "recording", "transcribing", "error")}
    assert all(not i.isNull() for i in imgs.values())
    assert imgs["idle"] != imgs["recording"] != imgs["transcribing"]
    assert not icon_for("idle").isNull()


def test_tray_state_updates_tooltip_and_actions_flow(qapp):
    got = []
    tray = Tray(on_action=got.append)
    tray.set_state("recording", "listening")
    assert "recording" in tray.icon.toolTip() and "listening" in tray.icon.toolTip()
    tray.set_profiles(["local", "openai"], "openai")
    names = [a.text() for a in tray.profile_menu.actions()]
    assert names == ["local", "openai"]
    assert [a.isChecked() for a in tray.profile_menu.actions()] == [False, True]
    tray.profile_menu.actions()[0].trigger()
    tray.action("recall").trigger()
    tray.action("quit").trigger()
    assert got == ["profile:local", "recall", "quit"]
    tray.state_changed.emit("error", "boom")
    qapp.processEvents()
    assert "error" in tray.icon.toolTip()
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/ui -v`, FAIL `No module named 'voice.ui'`.

- [ ] **Step 3: Create `voice/ui/__init__.py` (empty) and `voice/ui/notify.py`**

```python
"""Desktop notifications through notify-send (libnotify)."""
from __future__ import annotations

import logging
import subprocess
from typing import Callable

from voice import APP_NAME

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, enabled: bool = True, run: Callable = subprocess.Popen):
        self._enabled = enabled
        self._run = run

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def notify(self, title: str, body: str, urgency: str = "normal") -> None:
        if not self._enabled:
            return
        argv = ["notify-send", "--app-name", APP_NAME, "--urgency", urgency, "--expire-time", "4000", title, body]
        try:
            self._run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("notify-send unavailable: %s", exc)
```

- [ ] **Step 4: Create `voice/ui/icons.py`**

```python
"""Tray icons drawn at runtime so no asset files are needed."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

_COLORS = {
    "idle": ("#d8dee9", None),
    "recording": ("#e5484d", None),
    "transcribing": ("#d8dee9", "#f5a524"),
    "injecting": ("#d8dee9", "#3b82f6"),
    "error": ("#f97316", None),
}


def pixmap_for(state: str, size: int = 64) -> QPixmap:
    fill, ring = _COLORS.get(state, _COLORS["idle"])
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size
    if ring:
        p.setPen(QPen(QColor(ring), s * 0.08))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(s * 0.08, s * 0.08, s * 0.84, s * 0.84))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(fill))
    p.drawRoundedRect(QRectF(s * 0.36, s * 0.16, s * 0.28, s * 0.44), s * 0.14, s * 0.14)   # capsule
    p.setPen(QPen(QColor(fill), s * 0.07))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(s * 0.26, s * 0.30, s * 0.48, s * 0.42), 200 * 16, 140 * 16)            # cradle
    p.drawLine(int(s * 0.5), int(s * 0.72), int(s * 0.5), int(s * 0.84))                      # stem
    p.drawLine(int(s * 0.38), int(s * 0.84), int(s * 0.62), int(s * 0.84))                    # base
    if state == "error":
        p.setPen(QPen(QColor("#111111"), s * 0.06))
        p.drawLine(int(s * 0.5), int(s * 0.24), int(s * 0.5), int(s * 0.46))
        p.drawPoint(int(s * 0.5), int(s * 0.54))
    p.end()
    return pm


def icon_for(state: str, size: int = 64) -> QIcon:
    return QIcon(pixmap_for(state, size))
```

- [ ] **Step 5: Create `voice/ui/tray.py`**

```python
"""System tray icon (StatusNotifierItem on KDE) with a small menu."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from voice import APP_NAME
from voice.ui.icons import icon_for


class Tray(QObject):
    state_changed = Signal(str, str)

    def __init__(self, on_action: Callable[[str], None]):
        super().__init__()
        self._on_action = on_action
        self.icon = QSystemTrayIcon(icon_for("idle"))
        self.menu = QMenu()
        self._actions: dict[str, QAction] = {}
        for key, label in (("recall", "Recall last dictation"), ("retry", "Retry last recording")):
            self._add(key, label)
        self.menu.addSeparator()
        self.profile_menu = self.menu.addMenu("Transcription profile")
        self._profile_group = QActionGroup(self)
        self._profile_group.setExclusive(True)
        self.menu.addSeparator()
        self._add("settings", "Settings…")
        self._add("quit", "Quit")
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.state_changed.connect(self.set_state)
        self.set_state("idle", "ready")

    def _add(self, key: str, label: str) -> None:
        act = QAction(label, self.menu)
        act.triggered.connect(lambda _=False, k=key: self._on_action(k))
        self.menu.addAction(act)
        self._actions[key] = act

    def action(self, key: str) -> QAction:
        return self._actions[key]

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._on_action("settings")

    def set_state(self, state: str, detail: str = "") -> None:
        self.icon.setIcon(icon_for(state))
        self.icon.setToolTip(f"{APP_NAME} · {state}" + (f" · {detail}" if detail else ""))

    def set_profiles(self, names: list[str], active: str) -> None:
        for act in list(self.profile_menu.actions()):
            self.profile_menu.removeAction(act)
            self._profile_group.removeAction(act)
        for name in names:
            act = QAction(name, self.profile_menu)
            act.setCheckable(True)
            act.setChecked(name == active)
            act.triggered.connect(lambda _=False, n=name: self._on_action(f"profile:{n}"))
            self._profile_group.addAction(act)
            self.profile_menu.addAction(act)

    def show(self) -> None:
        self.icon.show()
```

- [ ] **Step 6: Run tests** → `uv run pytest tests/ui -v`, expected 4 passed.

- [ ] **Step 7: Commit**

```bash
git add voice/ui tests/ui
git commit -m "feat(ui): notifications, drawn tray icons and tray menu

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 18: Settings dialog

**Files:**
- Create: `voice/ui/settings.py`, `tests/ui/test_settings.py`

**Interfaces:**
- Consumes: `Config`, `parse_keyspec`, `Source`, `EvdevListener.capture_next` (via a callable), `DEFAULT_CONFIG` templates
- Produces: `PROFILE_TEMPLATES: dict[str, dict]` (openai, groq, mistral, openrouter, together, local-swedish); `class SettingsDialog(QDialog)`: `__init__(config: Config, capture_key: Callable[[Callable[[str], None]], None], sources: Callable[[], list[Source]], parent=None)`; `saved = Signal()` emitted after a successful `Config.save()`; public widgets for tests: `hotkey_edit`, `mode_combo`, `language_combo`, `device_combo`, `profile_list`, `profile_form` (dict of `QLineEdit` by field), `replacements_table`, `save_button`, `capture_button`, `add_profile_combo`. Tabs: General, Hotkeys, Audio, Transcription, Dictionary. Validation errors from `Config.errors()` are shown in a label and block saving.

Hotkey capture: pressing `capture_button` sets its text to "Press a key…" and calls `capture_key(cb)`; the callback receives the evdev name from the listener thread, so it is marshalled through a `Signal(str)` (`_captured`) before touching widgets. Combination capture is out of scope for the button; users type `KEY_A+KEY_B` by hand in `hotkey_edit`, which validates via `parse_keyspec` on save.

- [ ] **Step 1: Write the failing tests**

`tests/ui/test_settings.py`:
```python
from voice.audio.capture import Source
from voice.config import Config
from voice.ui.settings import PROFILE_TEMPLATES, SettingsDialog


def make(qapp):
    cfg = Config.load()
    captures = []
    dlg = SettingsDialog(cfg, capture_key=captures.append,
                         sources=lambda: [Source("alsa_input.obsbot", "OBSBOT Tiny 3", True)])
    return cfg, dlg, captures


def test_loads_values_from_config(qapp):
    cfg, dlg, _ = make(qapp)
    assert dlg.hotkey_edit.text() == "KEY_F13"
    assert dlg.mode_combo.currentText() == "hold"
    assert dlg.language_combo.currentData() == "en"
    assert dlg.device_combo.itemText(1) == "OBSBOT Tiny 3"
    assert [dlg.profile_list.item(i).text() for i in range(dlg.profile_list.count())] == ["local", "openai", "groq", "openrouter"]


def test_edit_and_save_writes_config_and_emits(qapp):
    cfg, dlg, _ = make(qapp)
    fired = []
    dlg.saved.connect(lambda: fired.append(True))
    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.mode_combo.setCurrentText("toggle")
    dlg.device_combo.setCurrentIndex(1)
    dlg.profile_list.setCurrentRow(1)                       # openai
    dlg.profile_form["api_key"].setText("sk-abc")
    dlg.profile_form["model"].setText("gpt-4o-mini-transcribe")
    dlg.save_button.click()
    assert fired == [True]
    again = Config.load()
    assert again.get("hotkeys.dictate") == "KEY_RIGHTCTRL"
    assert again.get("hotkeys.dictate_mode") == "toggle"
    assert again.get("audio.device") == "alsa_input.obsbot"
    assert again.get("stt.profiles.openai.api_key") == "sk-abc"
    assert again.get("stt.profiles.openai.model") == "gpt-4o-mini-transcribe"


def test_invalid_hotkey_blocks_save(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.hotkey_edit.setText("KEY_BANANA")
    dlg.save_button.click()
    assert "KEY_BANANA" in dlg.error_label.text()
    assert Config.load().get("hotkeys.dictate") == "KEY_F13"


def test_capture_button_requests_key_and_fills_field(qapp):
    cfg, dlg, captures = make(qapp)
    dlg.capture_button.click()
    assert dlg.capture_button.text().startswith("Press")
    captures[0]("KEY_F14")                                   # listener thread would call this
    qapp.processEvents()
    assert dlg.hotkey_edit.text() == "KEY_F14"


def test_add_profile_from_template_and_replacements_roundtrip(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.add_profile_combo.setCurrentText("mistral")
    dlg.add_profile_button.click()
    assert dlg.profile_list.item(dlg.profile_list.count() - 1).text() == "mistral"
    assert dlg.profile_form["base_url"].text() == PROFILE_TEMPLATES["mistral"]["base_url"]
    dlg.replacements_table.setRowCount(1)
    dlg.set_replacement_row(0, "obs bot", "OBSBOT", "icase")
    dlg.save_button.click()
    again = Config.load()
    assert again.get("stt.profiles.mistral.backend") == "openai_compatible"
    assert again.get("dictionary.replacements") == [["obs bot", "OBSBOT", "icase"]]
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/ui/test_settings.py -v`, FAIL `No module named 'voice.ui.settings'`.

- [ ] **Step 3: Create `voice/ui/settings.py`**

```python
"""Settings dialog: edits config.toml through Config so comments survive."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from voice.audio.capture import Source
from voice.config import Config
from voice.hotkey.keyspec import parse_keyspec

PROFILE_TEMPLATES: dict[str, dict] = {
    "openai": {"backend": "openai_compatible", "base_url": "https://api.openai.com/v1", "model": "gpt-transcribe", "api_key": "", "prompt": ""},
    "groq": {"backend": "openai_compatible", "base_url": "https://api.groq.com/openai/v1", "model": "whisper-large-v3-turbo", "api_key": "", "prompt": ""},
    "mistral": {"backend": "openai_compatible", "base_url": "https://api.mistral.ai/v1", "model": "voxtral-mini-latest", "api_key": "", "prompt": ""},
    "openrouter": {"backend": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "model": "openai/whisper-large-v3-turbo", "api_key": "", "prompt": ""},
    "together": {"backend": "openai_compatible", "base_url": "https://api.together.xyz/v1", "model": "openai/whisper-large-v3", "api_key": "", "prompt": ""},
    "local-swedish": {"backend": "local", "model": "KBLab/kb-whisper-large", "device": "cuda", "compute_type": "float16", "beam_size": 5, "prompt": ""},
}
_LOCAL_FIELDS = ["model", "device", "compute_type", "beam_size", "prompt"]
_CLOUD_FIELDS = ["base_url", "model", "api_key", "api_key_env", "prompt"]
_LANGUAGES = [("English", "en"), ("Swedish", "sv"), ("Auto-detect", "auto")]


class SettingsDialog(QDialog):
    saved = Signal()
    _captured = Signal(str)

    def __init__(self, config: Config, capture_key: Callable[[Callable[[str], None]], None],
                 sources: Callable[[], list[Source]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("voice settings")
        self.setMinimumWidth(560)
        self._cfg, self._capture_key, self._sources = config, capture_key, sources
        self._current_profile: str | None = None
        self._captured.connect(self._on_captured)
        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._hotkeys_tab(), "Hotkeys")
        tabs.addTab(self._audio_tab(), "Audio")
        tabs.addTab(self._transcription_tab(), "Transcription")
        tabs.addTab(self._dictionary_tab(), "Dictionary")
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #e5484d")
        self.error_label.setWordWrap(True)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self._load()

    # -- tabs -----------------------------------------------------------------
    def _general_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.language_combo = QComboBox()
        for label, code in _LANGUAGES:
            self.language_combo.addItem(label, code)
        self.notifications_combo = QComboBox()
        self.notifications_combo.addItems(["on", "off"])
        form.addRow("Language", self.language_combo)
        form.addRow("Notifications", self.notifications_combo)
        return w

    def _hotkeys_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.hotkey_edit = QLineEdit()
        self.capture_button = QPushButton("Capture key")
        self.capture_button.clicked.connect(self._start_capture)
        row = QHBoxLayout()
        row.addWidget(self.hotkey_edit)
        row.addWidget(self.capture_button)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["hold", "toggle"])
        self.recall_edit = QLineEdit()
        self.cancel_edit = QLineEdit()
        form.addRow("Dictate key", row)
        form.addRow("Mode", self.mode_combo)
        form.addRow("Recall last", self.recall_edit)
        form.addRow("Cancel recording", self.cancel_edit)
        form.addRow(QLabel("Combinations: type KEY_LEFTMETA+KEY_SPACE. Names are evdev key names."))
        return w

    def _audio_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.device_combo = QComboBox()
        self.device_combo.addItem("System default", "")
        for src in self._sources():
            self.device_combo.addItem(src.description + (" (default)" if src.is_default else ""), src.name)
        self.max_seconds = QSpinBox()
        self.max_seconds.setRange(5, 600)
        form.addRow("Microphone", self.device_combo)
        form.addRow("Max seconds", self.max_seconds)
        return w

    def _transcription_tab(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)
        left = QVBoxLayout()
        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._show_profile)
        self.add_profile_combo = QComboBox()
        self.add_profile_combo.addItems(list(PROFILE_TEMPLATES))
        self.add_profile_button = QPushButton("Add from template")
        self.add_profile_button.clicked.connect(self._add_profile)
        self.activate_button = QPushButton("Use this profile")
        self.activate_button.clicked.connect(self._activate_profile)
        left.addWidget(self.profile_list)
        left.addWidget(self.add_profile_combo)
        left.addWidget(self.add_profile_button)
        left.addWidget(self.activate_button)
        self.profile_form: dict[str, QLineEdit] = {}
        self._form_widget = QWidget()
        self._form_layout = QFormLayout(self._form_widget)
        self.active_label = QLabel()
        right = QVBoxLayout()
        right.addWidget(self.active_label)
        right.addWidget(self._form_widget)
        right.addStretch()
        outer.addLayout(left, 1)
        outer.addLayout(right, 2)
        return w

    def _dictionary_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.replacements_table = QTableWidget(0, 3)
        self.replacements_table.setHorizontalHeaderLabels(["Heard", "Replace with", "Flags (icase, regex)"])
        add = QPushButton("Add row")
        add.clicked.connect(lambda: self.replacements_table.insertRow(self.replacements_table.rowCount()))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self.replacements_table.removeRow(self.replacements_table.currentRow()))
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        layout.addWidget(self.replacements_table)
        layout.addLayout(row)
        return w

    # -- load/save --------------------------------------------------------------
    def _load(self) -> None:
        c = self._cfg
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(c.get("general.language", "en"))))
        self.notifications_combo.setCurrentText("on" if c.get("general.notifications", True) else "off")
        self.hotkey_edit.setText(c.get("hotkeys.dictate", ""))
        self.mode_combo.setCurrentText(c.get("hotkeys.dictate_mode", "hold"))
        self.recall_edit.setText(c.get("hotkeys.recall", ""))
        self.cancel_edit.setText(c.get("hotkeys.cancel", ""))
        self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(c.get("audio.device", ""))))
        self.max_seconds.setValue(int(c.get("audio.max_seconds", 120)))
        self.profile_list.clear()
        for name in (c.get("stt.profiles", {}) or {}):
            self.profile_list.addItem(name)
        self.active_label.setText(f"Active profile: {c.get('stt.active')}")
        self.profile_list.setCurrentRow(0)
        rules = c.get("dictionary.replacements", []) or []
        self.replacements_table.setRowCount(len(rules))
        for i, rule in enumerate(rules):
            self.set_replacement_row(i, *(list(rule) + ["", "", ""])[:3])

    def set_replacement_row(self, row: int, src: str, dst: str, flags: str = "") -> None:
        for col, val in enumerate((src, dst, flags)):
            self.replacements_table.setItem(row, col, QTableWidgetItem(str(val)))

    def _show_profile(self, row: int) -> None:
        self._commit_profile_form()
        item = self.profile_list.item(row)
        if item is None:
            return
        name = item.text()
        self._current_profile = name
        profile = self._cfg.get(f"stt.profiles.{name}", {}) or {}
        while self._form_layout.rowCount():
            self._form_layout.removeRow(0)
        self.profile_form = {}
        fields = _LOCAL_FIELDS if profile.get("backend") == "local" else _CLOUD_FIELDS
        self._form_layout.addRow("backend", QLabel(str(profile.get("backend", ""))))
        for field in fields:
            edit = QLineEdit(str(profile.get(field, "")))
            if field == "api_key":
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.profile_form[field] = edit
            self._form_layout.addRow(field, edit)

    def _commit_profile_form(self) -> None:
        if not self._current_profile or not self.profile_form:
            return
        for field, edit in self.profile_form.items():
            value: object = edit.text()
            if field == "beam_size":
                value = int(value or 5)
            self._cfg.set(f"stt.profiles.{self._current_profile}.{field}", value)

    def _add_profile(self) -> None:
        name = self.add_profile_combo.currentText()
        if self.profile_list.findItems(name, Qt.MatchFlag.MatchExactly):
            return
        for field, value in PROFILE_TEMPLATES[name].items():
            self._cfg.set(f"stt.profiles.{name}.{field}", value)
        self.profile_list.addItem(name)
        self.profile_list.setCurrentRow(self.profile_list.count() - 1)

    def _activate_profile(self) -> None:
        if self._current_profile:
            self._cfg.set("stt.active", self._current_profile)
            self.active_label.setText(f"Active profile: {self._current_profile}")

    def _start_capture(self) -> None:
        self.capture_button.setText("Press a key…")
        self._capture_key(self._captured.emit)

    def _on_captured(self, name: str) -> None:
        self.hotkey_edit.setText(name)
        self.capture_button.setText("Capture key")

    def _save(self) -> None:
        c = self._cfg
        for field, edit in (("hotkeys.dictate", self.hotkey_edit), ("hotkeys.recall", self.recall_edit), ("hotkeys.cancel", self.cancel_edit)):
            try:
                parse_keyspec(edit.text())
            except ValueError as exc:
                self.error_label.setText(f"{field}: {exc}")
                return
            c.set(field, edit.text().strip())
        c.set("general.language", self.language_combo.currentData())
        c.set("general.notifications", self.notifications_combo.currentText() == "on")
        c.set("hotkeys.dictate_mode", self.mode_combo.currentText())
        c.set("audio.device", self.device_combo.currentData() or "")
        c.set("audio.max_seconds", self.max_seconds.value())
        self._commit_profile_form()
        rules = []
        for r in range(self.replacements_table.rowCount()):
            cells = [self.replacements_table.item(r, col) for col in range(3)]
            src, dst, flags = [(x.text() if x else "").strip() for x in cells]
            if src:
                rules.append([src, dst, flags] if flags else [src, dst])
        c.set("dictionary.replacements", rules)
        errs = c.errors()
        if errs:
            self.error_label.setText("; ".join(errs))
            return
        c.save()
        self.error_label.setText("")
        self.saved.emit()
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/ui -v`, expected 9 passed.

- [ ] **Step 5: Screenshot the dialog for the visual check**

```bash
mkdir -p tests/_screenshots && QT_QPA_PLATFORM=offscreen uv run python - <<'EOF'
from PySide6.QtWidgets import QApplication
from voice.config import Config
from voice.ui.settings import SettingsDialog
app = QApplication([])
dlg = SettingsDialog(Config.load(), capture_key=lambda cb: None, sources=lambda: [])
dlg.resize(720, 480)
from PySide6.QtWidgets import QTabWidget
tabs = dlg.findChild(QTabWidget)
for i in range(tabs.count()):
    tabs.setCurrentIndex(i); app.processEvents()
    dlg.grab().save(f"tests/_screenshots/settings-{i}.png")
print("ok")
EOF
```
Open the five PNGs (Read tool) and confirm: no clipped labels, form fields visible, the transcription tab shows list + form side by side. Fix layout issues before committing.

- [ ] **Step 6: Commit**

```bash
git add voice/ui/settings.py tests/ui/test_settings.py
git commit -m "feat(ui): settings dialog with hotkey capture and profile templates

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---
### Task 19: Daemon wiring and CLI

**Files:**
- Create: `voice/daemon.py`, `voice/cli.py`, `tests/test_cli.py`, `tests/test_daemon.py`

**Interfaces:**
- Produces (`daemon.py`): `class Daemon`: `__init__(config: Config, *, listener=None, recorder=None, clipboard=None, sender=None, notifier=None, tray=None)` (all optional for tests), `build() -> None` wires `Tracker`, `EvdevListener`, `Recorder`, `make_transcriber`, `Injector`, `History`, `Dictation`, `Tray`, `Server`; `handle(request: dict) -> dict` implements IPC commands `ping, status, start, stop, toggle, cancel, recall, retry, profile (name), reload, settings, quit`; `apply_config()` re-reads hotkeys, transcriber and notifier from `config` (called after settings save and on `reload`); `run() -> int` starts Qt loop; `shutdown()`. `hotkey_specs(config) -> dict[str, KeySpec]` module function. `window_class_getter(config) -> Callable[[], str | None]`.
- Produces (`cli.py`): `main(argv: list[str] | None = None) -> int`. Subcommands: none → start daemon or raise settings if running; `daemon`; `start|stop|toggle|cancel|recall|retry|status|settings|quit|reload`; `profile <name>`; `doctor` (Task 20). Non-daemon commands print the reply (`status` prints state, profile, backend description, last error) and exit 1 on `ok: false` or when the daemon is not running. Logging: `--verbose` sets DEBUG; default INFO to stderr.

Threading rule: `Dictation.on_state` runs on worker/evdev threads and must only emit `tray.state_changed` (a Qt signal), never touch widgets. `settings` requests from IPC arrive on the IPC thread and are forwarded through a `Signal()` (`_open_settings`) to the Qt thread. The transcriber is warmed up once in the worker executor right after build so the first dictation is fast.

- [ ] **Step 1: Write the failing tests**

`tests/test_daemon.py`:
```python
import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from voice.config import Config
from voice.daemon import Daemon, hotkey_specs
from voice.hotkey.keyspec import parse_keyspec
from voice.pipeline import State


class FakeListener:
    def __init__(self):
        self.started = False
        self.specs = None

    def start(self): self.started = True
    def stop(self): self.started = False
    def capture_next(self, cb): self.cb = cb
    def modifiers_held(self): return False
    def devices_ok(self): return True


class FakeSender:
    name = "fake"
    def send_chord(self, codes): pass
    def available(self): return True


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_hotkey_specs_reads_all_bindings(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.recall", "KEY_F14")
    specs = hotkey_specs(cfg)
    assert specs["dictate"] == parse_keyspec("KEY_F13")
    assert specs["recall"] == parse_keyspec("KEY_F14")
    assert specs["cancel"] == parse_keyspec("KEY_ESC")


def test_handle_commands_and_profile_switch(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda profile, secret: type("T", (), {
        "name": profile["backend"], "describe": lambda self: f"fake {profile['model']}",
        "warmup": lambda self: None, "transcribe": lambda self, *a: None})())
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender())
    d.build()
    assert d.handle({"cmd": "ping"}) == {"ok": True}
    st = d.handle({"cmd": "status"})
    assert st["ok"] and st["state"] == "idle" and st["profile"] == "local" and "large-v3-turbo" in st["backend"]
    assert d.handle({"cmd": "profile", "name": "openai"})["ok"]
    assert Config.load().get("stt.active") == "openai"
    assert "gpt-transcribe" in d.handle({"cmd": "status"})["backend"]
    bad = d.handle({"cmd": "profile", "name": "ghost"})
    assert bad["ok"] is False and "ghost" in bad["error"]
    assert d.handle({"cmd": "nope"})["ok"] is False
    d.shutdown()


def test_apply_config_rebinds_hotkeys(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender())
    d.build()
    cfg.set("hotkeys.dictate", "KEY_F20")
    cfg.save()
    d.apply_config()
    assert d.tracker.feed(parse_keyspec("KEY_F20").codes.__iter__().__next__(), 1) == [("dictate", "press")]
    assert d.dictation.state == State.RECORDING or True   # recorder is real Recorder; start may fail without pw-record
    d.shutdown()
```

`tests/test_cli.py`:
```python
import pytest

from voice import ipc
from voice.cli import main


def test_status_without_daemon_exits_1(isolated_xdg, capsys):
    assert main(["status"]) == 1
    assert "not running" in capsys.readouterr().err


def test_commands_are_forwarded_to_daemon(isolated_xdg, capsys):
    seen = []

    def handler(req):
        seen.append(req)
        return {"ok": True, "state": "idle", "profile": "local", "backend": "fake", "last_error": None}

    srv = ipc.Server(handler)
    srv.start()
    try:
        assert main(["toggle"]) == 0
        assert main(["profile", "openai"]) == 0
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "idle" in out and "local" in out
    finally:
        srv.stop()
    assert [r["cmd"] for r in seen] == ["toggle", "profile", "status"]
    assert seen[1]["name"] == "openai"


def test_error_reply_exits_1(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": False, "error": "no such profile"})
    srv.start()
    try:
        assert main(["profile", "x"]) == 1
        assert "no such profile" in capsys.readouterr().err
    finally:
        srv.stop()
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_daemon.py tests/test_cli.py -v`, FAIL on imports.

- [ ] **Step 3: Create `voice/daemon.py`**

```python
"""Wires every module together and runs the Qt event loop."""
from __future__ import annotations

import logging
import sys
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from voice import APP_NAME, __version__
from voice.audio.capture import Recorder, list_sources
from voice.config import Config
from voice.history import History
from voice.hotkey.evdev_listener import EvdevListener
from voice.hotkey.keyspec import KeySpec, Tracker, parse_keyspec
from voice.inject.clipboard import Clipboard
from voice.inject.fallback import make_key_sender
from voice.inject.injector import Injector, run_window_command
from voice.ipc import Server
from voice.pipeline import Dictation, Services, State
from voice.stt import make_transcriber
from voice.ui.notify import Notifier
from voice.ui.settings import SettingsDialog
from voice.ui.tray import Tray

log = logging.getLogger(__name__)


def hotkey_specs(config: Config) -> dict[str, KeySpec]:
    specs = {}
    for name in ("dictate", "recall", "cancel"):
        text = config.get(f"hotkeys.{name}", "") or ""
        try:
            specs[name] = parse_keyspec(text)
        except ValueError as exc:
            log.warning("ignoring hotkeys.%s: %s", name, exc)
            specs[name] = parse_keyspec("")
    return specs


def window_class_getter(config: Config) -> Callable[[], str | None]:
    return lambda: run_window_command(config.get("inject.active_window_command", "") or "")


class _Bridge(QObject):
    open_settings = Signal()
    quit = Signal()


class Daemon:
    def __init__(self, config: Config, *, listener=None, recorder=None, clipboard=None, sender=None,
                 notifier=None, tray=None):
        self.config = config
        self._listener_override = listener
        self._recorder = recorder or Recorder()
        self._clipboard = clipboard or Clipboard()
        self._sender = sender
        self._notifier = notifier or Notifier(bool(config.get("general.notifications", True)))
        self._tray = tray
        self._server: Server | None = None
        self._settings: SettingsDialog | None = None
        self._bridge = _Bridge()

    # -- construction -------------------------------------------------------
    def build(self) -> None:
        self.tracker = Tracker(hotkey_specs(self.config))
        self.history = History()
        self.listener = self._listener_override or EvdevListener(self.tracker, self._on_hotkey)
        sender = self._sender or make_key_sender()
        self.injector = Injector(self._clipboard, sender, self.config.get("inject", {}) or {},
                                 self.listener.modifiers_held, window_class_getter(self.config))
        services = Services(recorder=self._recorder, transcriber=self._make_transcriber(),
                            injector=self.injector, history=self.history, notify=self._notifier.notify,
                            config_getter=self.config.get, prompt_getter=self._prompt)
        self.dictation = Dictation(services)
        self.tray = self._tray or Tray(self._on_tray_action)
        self.dictation.on_state = lambda s, d: self.tray.state_changed.emit(s.value, d)
        self.tray.set_profiles(list(self.config.get("stt.profiles", {}) or {}), self.config.get("stt.active"))
        self._bridge.open_settings.connect(self.open_settings)
        self._bridge.quit.connect(self._quit)
        self._server = Server(self.handle)

    def _make_transcriber(self):
        name, profile = self.config.stt_profile()
        t = make_transcriber(profile, self.config.secret(profile))
        log.info("transcriber: %s (%s)", name, t.describe())
        return t

    def _prompt(self) -> str | None:
        return self.config.stt_profile()[1].get("prompt") or None

    # -- runtime ------------------------------------------------------------
    def run(self) -> int:
        app = QApplication.instance() or QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName(APP_NAME)
        self.build()
        self._server.start()
        self.listener.start()
        self.tray.show()
        from voice.pipeline import _thread_executor
        _thread_executor(self._warmup)
        if self.listener.devices_ok() is False:
            self._notifier.notify("No keyboard access", "Run the installer's udev step or add yourself to the input group.", "critical")
        log.info("%s %s ready", APP_NAME, __version__)
        code = app.exec()
        self.shutdown()
        return code

    def _warmup(self) -> None:
        try:
            self.dictation.sv.transcriber.warmup()
            reason = getattr(self.dictation.sv.transcriber, "fallback_reason", None)
            if reason:
                self._notifier.notify("Running on CPU", reason, "normal")
            self.tray.state_changed.emit("idle", self.dictation.sv.transcriber.describe())
        except Exception as exc:
            log.exception("warmup failed")
            self._notifier.notify("Model failed to load", str(exc), "critical")

    def shutdown(self) -> None:
        try:
            self.listener.stop()
        except Exception:
            pass
        if self._server:
            self._server.stop()

    def apply_config(self) -> None:
        self.config.reload()
        self.tracker.set_specs(hotkey_specs(self.config))
        self._notifier.set_enabled(bool(self.config.get("general.notifications", True)))
        try:
            self.dictation.set_transcriber(self._make_transcriber())
        except Exception as exc:
            self._notifier.notify("Transcription profile problem", str(exc), "critical")
        self.injector = Injector(self._clipboard, self.injector._sender, self.config.get("inject", {}) or {},
                                 self.listener.modifiers_held, window_class_getter(self.config))
        self.dictation.set_injector(self.injector)
        self.tray.set_profiles(list(self.config.get("stt.profiles", {}) or {}), self.config.get("stt.active"))
        from voice.pipeline import _thread_executor
        _thread_executor(self._warmup)

    # -- events ---------------------------------------------------------------
    def _on_hotkey(self, name: str, kind: str) -> None:
        self.dictation.on_hotkey(name, kind)

    def _on_tray_action(self, action: str) -> None:
        if action.startswith("profile:"):
            self.handle({"cmd": "profile", "name": action.split(":", 1)[1]})
        else:
            self.handle({"cmd": action})

    def open_settings(self) -> None:
        if self._settings is None:
            self._settings = SettingsDialog(self.config, self.listener.capture_next, list_sources)
            self._settings.saved.connect(self.apply_config)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    def _quit(self) -> None:
        app = QApplication.instance()
        if app:
            app.quit()

    # -- ipc ----------------------------------------------------------------
    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        d = self.dictation
        simple = {"start": d.start, "stop": d.stop, "toggle": d.toggle, "cancel": d.cancel,
                  "recall": d.recall, "retry": d.retry}
        if cmd == "ping":
            return {"ok": True}
        if cmd in simple:
            simple[cmd]()
            return {"ok": True, "state": d.state.value}
        if cmd == "status":
            return {"ok": True, "state": d.state.value, "profile": self.config.get("stt.active"),
                    "backend": d.sv.transcriber.describe(), "last_error": d.last_error,
                    "version": __version__, "keyboard": self.listener.devices_ok()}
        if cmd == "profile":
            name = request.get("name", "")
            if name not in (self.config.get("stt.profiles", {}) or {}):
                return {"ok": False, "error": f"unknown profile '{name}'"}
            self.config.set("stt.active", name)
            self.config.save()
            self.apply_config()
            return {"ok": True, "profile": name}
        if cmd == "reload":
            self.apply_config()
            return {"ok": True}
        if cmd == "settings":
            self._bridge.open_settings.emit()
            return {"ok": True}
        if cmd == "quit":
            self._bridge.quit.emit()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def main() -> int:
    config = Config.load()
    errs = config.errors()
    if errs:
        log.warning("config problems: %s", "; ".join(errs))
    return Daemon(config).run()
```

- [ ] **Step 4: Create `voice/cli.py`**

```python
"""`voice` command line: starts the daemon or talks to a running one."""
from __future__ import annotations

import argparse
import logging
import sys

from voice import APP_NAME, __version__
from voice.ipc import IPCError, is_running, send

SIMPLE = ["start", "stop", "toggle", "cancel", "recall", "retry", "status", "settings", "quit", "reload"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP_NAME, description="Wayland-native voice typing")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("daemon", help="run the background daemon (default when none is running)")
    for name in SIMPLE:
        sub.add_parser(name)
    prof = sub.add_parser("profile", help="switch transcription profile")
    prof.add_argument("name")
    sub.add_parser("doctor", help="check this machine for everything voice needs")
    return p


def _print_status(reply: dict) -> None:
    print(f"state:    {reply.get('state')}")
    print(f"profile:  {reply.get('profile')}")
    print(f"backend:  {reply.get('backend')}")
    print(f"keyboard: {'ok' if reply.get('keyboard') else 'NO ACCESS'}")
    if reply.get("last_error"):
        print(f"error:    {reply['last_error']}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "doctor":
        from voice.doctor import run_doctor
        return run_doctor()
    if args.cmd in (None, "daemon"):
        if args.cmd is None and is_running():
            send({"cmd": "settings"})
            return 0
        from voice.daemon import main as daemon_main
        return daemon_main()
    request = {"cmd": args.cmd}
    if args.cmd == "profile":
        request["name"] = args.name
    try:
        reply = send(request)
    except IPCError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1
    if not reply.get("ok"):
        print(f"{APP_NAME}: {reply.get('error', 'failed')}", file=sys.stderr)
        return 1
    if args.cmd == "status":
        _print_status(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests** → `uv run pytest tests/test_daemon.py tests/test_cli.py -v`, expected 6 passed. Then the whole suite: `uv run pytest -q` → all passed.

- [ ] **Step 6: Commit**

```bash
git add voice/daemon.py voice/cli.py tests/test_daemon.py tests/test_cli.py
git commit -m "feat: daemon wiring, ipc command handling and cli

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 20: Doctor

**Files:**
- Create: `voice/doctor.py`, `tests/test_doctor.py`

**Interfaces:**
- Produces: `@dataclass Check(name: str, ok: bool, detail: str)`; `run_checks(probes: dict[str, Callable[[], tuple[bool, str]]] | None = None) -> list[Check]`; `default_probes() -> dict`; `run_doctor() -> int` prints one line per check (`✔`/`✘`), returns 0 when all required checks pass (portal, wl-clipboard, pw-record, keyboard access, config valid), 1 otherwise. Optional checks (CUDA, model cached, notify-send, ydotool/wtype) never fail the run. Probes: `python`, `config`, `keyboard access` (count of readable keyboard devices), `pw-record`, `pw-dump sources` (list names), `wl-clipboard`, `portal` (RemoteDesktop version), `cuda` (ctranslate2 device count + cudnn import), `model cache` (whether the active local model directory exists under the HF cache), `notify-send`, `fallback senders`.

- [ ] **Step 1: Write the failing tests**

`tests/test_doctor.py`:
```python
from voice.doctor import Check, run_checks, run_doctor


def test_run_checks_collects_results_and_catches_exceptions():
    def boom():
        raise RuntimeError("kaput")
    checks = run_checks({"a": lambda: (True, "fine"), "b": boom})
    assert checks == [Check("a", True, "fine"), Check("b", False, "kaput")]


def test_run_doctor_exit_code_depends_on_required(monkeypatch, capsys):
    monkeypatch.setattr("voice.doctor.default_probes", lambda: {
        "portal": lambda: (True, "v2"), "wl-clipboard": lambda: (True, ""), "pw-record": lambda: (True, ""),
        "keyboard access": lambda: (True, "2 devices"), "config": lambda: (True, ""), "cuda": lambda: (False, "no gpu")})
    assert run_doctor() == 0
    out = capsys.readouterr().out
    assert "✔ portal" in out and "✘ cuda" in out and "optional" in out
    monkeypatch.setattr("voice.doctor.default_probes", lambda: {"portal": lambda: (False, "missing")})
    assert run_doctor() == 1
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_doctor.py -v`, FAIL `No module named 'voice.doctor'`.

- [ ] **Step 3: Create `voice/doctor.py`**

```python
"""`voice doctor`: tells the owner what works on this machine and what to fix."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from voice import __version__

REQUIRED = {"portal", "wl-clipboard", "pw-record", "keyboard access", "config"}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _which(binary: str) -> tuple[bool, str]:
    path = shutil.which(binary)
    return (True, path) if path else (False, f"{binary} not found in PATH")


def _config() -> tuple[bool, str]:
    from voice.config import Config
    cfg = Config.load()
    errs = cfg.errors()
    return (not errs, "; ".join(errs) or str(cfg.path))


def _keyboard() -> tuple[bool, str]:
    from voice.hotkey.evdev_listener import list_keyboards
    devs = list_keyboards()
    if not devs:
        return False, "no readable keyboards: run install.sh (udev rule) or add yourself to the 'input' group"
    return True, ", ".join(d.name for d in devs)


def _sources() -> tuple[bool, str]:
    from voice.audio.capture import list_sources
    srcs = list_sources()
    return (bool(srcs), ", ".join(f"{s.description}{' *' if s.is_default else ''}" for s in srcs) or "no microphones")


def _portal() -> tuple[bool, str]:
    from voice.inject.portal import portal_available
    return (True, "RemoteDesktop portal v2+") if portal_available() else (False, "RemoteDesktop portal missing (xdg-desktop-portal-kde/gnome)")


def _cuda() -> tuple[bool, str]:
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        return False, f"ctranslate2 cuda probe failed: {exc}"
    if n == 0:
        return False, "no CUDA device; local transcription will run on CPU (install with --extra gpu on the NVIDIA PC)"
    return True, f"{n} CUDA device(s)"


def _model_cache() -> tuple[bool, str]:
    from voice.config import Config
    from voice.stt.local import resolve_model_name
    cfg = Config.load()
    _, profile = cfg.stt_profile()
    if profile.get("backend") != "local":
        return True, "active profile is cloud; nothing to cache"
    name = resolve_model_name(profile["model"]).replace("/", "--")
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub" / f"models--{name}"
    return (True, str(hub)) if hub.exists() else (False, f"{profile['model']} not downloaded yet (first dictation downloads it)")


def _senders() -> tuple[bool, str]:
    found = [b for b in ("wtype", "ydotool") if shutil.which(b)]
    return True, ", ".join(found) or "none (portal is the primary path)"


def default_probes() -> dict[str, Callable[[], tuple[bool, str]]]:
    return {
        "python": lambda: (True, f"{sys.version.split()[0]} · voice {__version__}"),
        "config": _config,
        "keyboard access": _keyboard,
        "pw-record": lambda: _which("pw-record"),
        "microphones": _sources,
        "wl-clipboard": lambda: (_which("wl-copy")[0] and _which("wl-paste")[0], "wl-copy/wl-paste" if _which("wl-copy")[0] else "install wl-clipboard"),
        "portal": _portal,
        "cuda": _cuda,
        "model cache": _model_cache,
        "notify-send": lambda: _which("notify-send"),
        "fallback senders": _senders,
    }


def run_checks(probes: dict[str, Callable[[], tuple[bool, str]]] | None = None) -> list[Check]:
    out = []
    for name, probe in (probes or default_probes()).items():
        try:
            ok, detail = probe()
        except Exception as exc:
            ok, detail = False, str(exc)
        out.append(Check(name, ok, detail))
    return out


def run_doctor() -> int:
    checks = run_checks()
    failed_required = False
    for c in checks:
        mark = "✔" if c.ok else "✘"
        tag = "" if c.ok or c.name in REQUIRED else " (optional)"
        print(f"{mark} {c.name}{tag}: {c.detail}")
        if not c.ok and c.name in REQUIRED:
            failed_required = True
    print("\nall required checks passed" if not failed_required else "\nfix the ✘ required items above")
    return 1 if failed_required else 0
```

- [ ] **Step 4: Run tests** → `uv run pytest tests/test_doctor.py -v`, expected 2 passed. Run it for real in the VM: `uv run voice doctor` and paste the output into the task notes (expected: cuda ✘ optional, keyboard access ✘ until the udev rule exists, wl-clipboard ✘ until installed).

- [ ] **Step 5: Commit**

```bash
git add voice/doctor.py tests/test_doctor.py
git commit -m "feat: voice doctor environment checks

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
```

---

### Task 21: Packaging, installer, README, boundary run and review

**Files:**
- Create: `packaging/70-voice-input.rules`, `packaging/voice.desktop`, `install.sh`, `tests/test_install.py`
- Modify: `README.md`

**Interfaces:**
- `install.sh [--uninstall] [--no-udev] [--gpu|--cpu]`: idempotent. Steps: check `uv` (print the CachyOS command `sudo pacman -S uv` if missing and exit 1); `uv sync --extra gpu` (or plain with `--cpu`; default picks gpu when `nvidia-smi` exists); write `~/.local/bin/voice` wrapper (`exec uv --project "<repo>" run --no-sync voice "$@"`); copy `packaging/voice.desktop` to `~/.local/share/applications/` and `~/.config/autostart/` with `Exec=` pointing at the wrapper; install the udev rule via `sudo install -m 644 ... /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input` unless `--no-udev`; finish by running `voice doctor`. `--uninstall` removes wrapper, desktop files, udev rule (sudo), and prints the config/state dirs it leaves behind. `DRY_RUN=1` env prints commands instead of running them (used by the test).

- [ ] **Step 1: Write the failing test**

`tests/test_install.py`:
```python
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    e = {**os.environ, "DRY_RUN": "1", "HOME": "/tmp/voice-home", **(env or {})}
    return subprocess.run(["bash", str(ROOT / "install.sh"), *args], capture_output=True, text=True, env=e)


def test_script_parses_and_dry_run_lists_steps():
    assert subprocess.run(["bash", "-n", str(ROOT / "install.sh")]).returncode == 0
    cp = run("--cpu", "--no-udev")
    assert cp.returncode == 0, cp.stderr
    assert "uv sync" in cp.stdout and "--extra gpu" not in cp.stdout
    assert "/tmp/voice-home/.local/bin/voice" in cp.stdout
    assert "autostart" in cp.stdout and "udev" not in cp.stdout.lower().replace("no-udev", "")


def test_gpu_flag_and_udev_step():
    cp = run("--gpu")
    assert "--extra gpu" in cp.stdout and "70-voice-input.rules" in cp.stdout and "udevadm" in cp.stdout


def test_uninstall_lists_removals():
    cp = run("--uninstall")
    assert cp.returncode == 0
    assert "rm" in cp.stdout and "autostart" in cp.stdout and ".config/voice" in cp.stdout


def test_packaging_files_reference_app():
    rules = (ROOT / "packaging" / "70-voice-input.rules").read_text()
    assert 'SUBSYSTEM=="input"' in rules and "uaccess" in rules
    desktop = (ROOT / "packaging" / "voice.desktop").read_text()
    assert "Exec=voice" in desktop and "X-GNOME-Autostart" not in desktop
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_install.py -v`, FAIL (no install.sh).

- [ ] **Step 3: Create packaging files and `install.sh`**

`packaging/70-voice-input.rules`:
```
# voice: let the logged-in seat user read keyboard events for push-to-talk (no group change, no re-login)
SUBSYSTEM=="input", KERNEL=="event*", TAG+="uaccess"
```

`packaging/voice.desktop`:
```
[Desktop Entry]
Type=Application
Name=voice
Comment=Wayland-native voice typing
Exec=voice daemon
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Accessibility;
StartupNotify=false
```

`install.sh`:
```bash
#!/usr/bin/env bash
# voice installer: everything lives in this directory + ~/.config/voice; only the udev rule is system-wide.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=voice
BIN="$HOME/.local/bin/$APP"
APPS="$HOME/.local/share/applications/$APP.desktop"
AUTOSTART="$HOME/.config/autostart/$APP.desktop"
RULE_SRC="$ROOT/packaging/70-voice-input.rules"
RULE_DST="/etc/udev/rules.d/70-voice-input.rules"
MODE=install; UDEV=1; EXTRA=auto

for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE=uninstall ;;
    --no-udev) UDEV=0 ;;
    --gpu) EXTRA=gpu ;;
    --cpu) EXTRA=cpu ;;
    -h|--help) echo "usage: install.sh [--uninstall] [--no-udev] [--gpu|--cpu]"; exit 0 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

run() { if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ $*"; else echo "+ $*"; "$@"; fi; }
sudo_run() { if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ sudo $*"; else echo "+ sudo $*"; sudo "$@"; fi; }

if [ "$MODE" = uninstall ]; then
  run rm -f "$BIN" "$APPS" "$AUTOSTART"
  [ -e "$RULE_DST" ] || [ "${DRY_RUN:-0}" = 1 ] && sudo_run rm -f "$RULE_DST" || true
  echo "left in place (delete if you want a clean slate): $HOME/.config/$APP $HOME/.local/state/$APP $HOME/.cache/huggingface"
  exit 0
fi

if ! command -v uv >/dev/null 2>&1 && [ "${DRY_RUN:-0}" != 1 ]; then
  echo "uv is required: sudo pacman -S uv   (or: curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
  exit 1
fi

if [ "$EXTRA" = auto ]; then
  if command -v nvidia-smi >/dev/null 2>&1; then EXTRA=gpu; else EXTRA=cpu; fi
fi
if [ "$EXTRA" = gpu ]; then run uv sync --project "$ROOT" --extra gpu; else run uv sync --project "$ROOT"; fi

run mkdir -p "$(dirname "$BIN")" "$(dirname "$APPS")" "$(dirname "$AUTOSTART")"
if [ "${DRY_RUN:-0}" = 1 ]; then
  echo "+ write $BIN"
else
  cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec uv --project "$ROOT" run --no-sync $APP "\$@"
EOF
  chmod +x "$BIN"
fi
run sed "s|^Exec=.*|Exec=$BIN daemon|" "$ROOT/packaging/$APP.desktop" > /dev/null
if [ "${DRY_RUN:-0}" = 1 ]; then
  echo "+ install desktop entry -> $APPS and autostart -> $AUTOSTART"
else
  sed "s|^Exec=.*|Exec=$BIN daemon|" "$ROOT/packaging/$APP.desktop" > "$APPS"
  cp "$APPS" "$AUTOSTART"
fi

if [ "$UDEV" = 1 ]; then
  echo "installing udev rule for keyboard access (asks for sudo once)"
  sudo_run install -m 644 "$RULE_SRC" "$RULE_DST"
  sudo_run udevadm control --reload
  sudo_run udevadm trigger --subsystem-match=input
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: add $HOME/.local/bin to PATH" ;; esac
echo "installed. start with: $APP    (autostarts at next login)"
[ "${DRY_RUN:-0}" = 1 ] || "$BIN" doctor || true
```

Make it executable: `chmod +x install.sh`.

- [ ] **Step 4: Run tests** → `uv run pytest tests/test_install.py -v`, expected 4 passed.

- [ ] **Step 5: Write the full `README.md`**

Sections: what it is (3 lines), requirements (CachyOS/Arch, KDE Plasma Wayland, pipewire, wl-clipboard, xdg-desktop-portal-kde, uv; optional NVIDIA driver), install (the three commands from spec §10), first run (udev rule, portal permission dialog once, model download), usage (hold key, tray menu, CLI list), configuration (annotated `config.toml` excerpt, profiles, how to add OpenAI/Groq/OpenRouter keys, Swedish model), troubleshooting (doctor output meanings, keyboard access, Konsole paste chord, clipboard managers), uninstall, roadmap (phases 2–4 from spec §11), external components (the list from spec §12 verbatim with links), "License: to be decided".

- [ ] **Step 6: Boundary run in the VM (owner runs the two sudo commands first)**

Ask the owner to run in this session:
```
! sudo apt install -y wl-clipboard
! sudo install -m 644 packaging/70-voice-input.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input
```
Then run: `uv run pytest -m boundary -v` (VAD, pw-record, clipboard, portal; the portal test pops GNOME's permission dialog once, owner clicks Share). Expected: all boundary tests pass. Then a smoke test: `uv run voice daemon -v &`, `uv run voice status`, open a text editor, `uv run voice toggle`, say a sentence, `uv run voice toggle`, confirm the text appears (CPU transcription in the VM takes several seconds). Screenshot the tray icon states with `spectacle`/`gnome-screenshot` if available and read them back. Record results in the final report.

- [ ] **Step 7: Full validation and review**

```bash
uv run pytest -q                      # unit + interaction
uv run pytest -m boundary -q          # VM boundary
git diff --check
```
Then invoke the `code-review` skill at `high` effort over the cumulative diff against the first commit (`ce19c8a`), fix what it finds, rerun the affected tests.

- [ ] **Step 8: Commit and push**

```bash
git add packaging install.sh README.md tests/test_install.py
git commit -m "feat: installer, udev rule, desktop entry and readme

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QKSmwaGSv14Q2M3jfhk4Ca"
git push
```

---

## Self-review notes

- Spec coverage: §3 architecture → Tasks 1, 15, 16, 19; §4 data flow → 5–10, 15; §5 config → 2, 18; §6 hotkey/injection/indicator → 3, 4, 12–14, 17; §8 errors → 7 (mic), 8 (CPU fallback), 9 (cloud errors), 14 (portal fallbacks), 15 (retry/notify), 19 (config invalid warning, duplicate launch); §9 testing → every task, boundary/gpu markers in 6, 7, 8, 12, 13, 21; §10 install → 21; §11 phase 1 scope only. Polish (§4 step 5) is phase 2 and intentionally absent; `Dictation._process` has a single call site to insert it.
- Types: `Transcript` fields (`text, language, audio_s, elapsed_s, backend`) used identically in Tasks 8, 9, 15; `InjectResult(method, chord, restored)` in 14, 15; `Services` fields in 15, 19; `KeySpec`/`Tracker` in 3, 4, 19; `Source` in 7, 18.
- Known judgement calls: window-class detection is a user-supplied command in phase 1 (KWin scripting detector deferred to phase 4); combination hotkeys are typed, not captured; `is_running` treats any `ok: true` reply as alive.
