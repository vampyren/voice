# voice

Wayland-native voice typing for Linux. Hold a key, speak, release, and the text is pasted
into whatever has focus. Transcribes locally on your NVIDIA GPU with faster-whisper, or
through any OpenAI-compatible speech-to-text API.

Status: phase 1 (dictation core). See `docs/superpowers/specs/` for the full design.

## What it is

- A background daemon (tray icon + settings window) that listens for a push-to-talk key
  via evdev, records audio with PipeWire, transcribes it, and injects the text.
- A thin CLI (`voice ...`) that talks to the running daemon over a Unix socket, so the
  same actions can also be bound to compositor shortcuts.
- Everything lives under this project directory plus `~/.config/voice`; the only
  system-wide change is one udev rule for keyboard access.

## Requirements

- CachyOS or another Arch-based distro, KDE Plasma on Wayland (GNOME works for
  development; window-class detection for the terminal paste chord is KDE-only).
- `pipewire` (for `pw-record`), `wl-clipboard`, `xdg-desktop-portal-kde` (or
  `xdg-desktop-portal-gnome`), and [`uv`](https://docs.astral.sh/uv/).
- Optional: an NVIDIA GPU with a recent driver for local transcription on CUDA. Without
  one, the local backend falls back to CPU (slower, but works).

## Install

```
git clone https://github.com/vampyren/voice ~/Apps/voice && cd ~/Apps/voice
./install.sh          # uv sync --extra gpu, udev rule (sudo once), autostart, ~/.local/bin/voice
voice doctor
```

`install.sh` is idempotent and safe to re-run. Flags:

- `--cpu` / `--gpu` — force the dependency set instead of auto-detecting `nvidia-smi`.
- `--no-udev` — skip the udev rule (you'll need to add yourself to the `input` group,
  or a keyboard device with no `uaccess` tag, and re-login, instead).
- `--uninstall` — remove the wrapper, desktop entries and udev rule (see Uninstall below).

It writes a `~/.local/bin/voice` wrapper so the command works from anywhere without
activating a virtualenv, installs `voice.desktop` to both
`~/.local/share/applications/` (app launcher) and `~/.config/autostart/` (login
autostart), and finishes by running `voice doctor`.

## First run

1. The installer's udev rule (`packaging/70-voice-input.rules`) tags input event devices
   with `uaccess` so the logged-in user can read keyboard events, no group change or
   re-login needed. Used `--no-udev`? `voice doctor` will flag missing keyboard access.
2. The first dictation that injects text opens KDE's one-time `RemoteDesktop` portal
   permission dialog (click Share/Allow). The restore token is then cached in the state
   directory so it never asks again, until you revoke the grant in System Settings.
3. On the local backend, the whisper model downloads on first use (a few GB, cached
   under `~/.cache/huggingface`), with a tray tooltip/notification while it downloads.

## Usage

Hold the configured hotkey (`KEY_F13` by default), speak, release — the transcript is
pasted into the focused window. In toggle mode, press once to start recording and again
to stop.

The tray icon shows the current state (idle, recording, transcribing, error) and its menu
exposes start/stop/cancel/recall, profile switching, settings and quit.

CLI:

```
voice                  # start the daemon, or raise the settings window if one is running
voice daemon           # run the daemon in the foreground (exits and raises the settings window if one is already running)
voice start|stop       # begin/end recording explicitly (useful with toggle mode)
voice toggle           # alternate recording on/off
voice cancel           # discard the current recording
voice recall           # re-insert the last dictation
voice retry            # re-send the last recording's audio (e.g. after a transient cloud error)
voice status           # state, active profile, backend, keyboard access, last error
voice settings         # raise the settings window
voice profile <name>   # switch the active STT profile (local, openai, groq, openrouter, ...)
voice reload           # re-read config.toml without restarting
voice quit             # stop the daemon
voice doctor           # check this machine for everything voice needs
voice --verbose ...    # DEBUG logging to stderr (default: INFO)
voice --version
```

## Configuration

`~/.config/voice/config.toml` is created with defaults and comments on first run (mode
`0600`) and is safe to hand-edit — it's read and written with `tomlkit`, so your comments
survive settings-window saves. The defaults:

```toml
# voice configuration. Edited by the settings window; hand edits are fine too.

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
```

**Hotkeys.** Use the settings window's "Capture key" button — press the physical key and
it fills in the exact evdev name it received. Combinations are typed by hand, e.g.
`KEY_LEFTMETA+KEY_SPACE`. If a Keychron spare key (the circle/triangle/square keys)
sends nothing, remap it in Keychron Launcher to F13 and bind `KEY_F13` here.

**Profiles.** `stt.active` picks one of the `[stt.profiles.*]` tables. Add a cloud
profile by pasting an API key: either `api_key = "sk-..."` inline, or set the
environment variable named by `api_key_env` (`OPENAI_API_KEY`, `GROQ_API_KEY`,
`OPENROUTER_API_KEY`) and keep the config file free of secrets. Switch with
`voice profile openai` or from the tray/settings. For Swedish, use the `local-swedish`
template in the settings window's profile picker (or set a profile's `model` to
`KBLab/kb-whisper-large` by hand) and `general.language = "sv"`.

**Paste behaviour.** Text is copied to the clipboard then pasted with Ctrl+V through the
desktop portal (`org.freedesktop.portal.RemoteDesktop`); KDE asks permission once and
remembers it. Windows whose class is in `inject.terminal_classes` get Ctrl+Shift+V
instead, since most terminals reserve Ctrl+V. `inject.active_window_command` should be a
command that prints the focused window's class to stdout; leave it empty if you don't
have one (the default chord is used, unknown window). The previous clipboard contents
are restored after the paste.

## Troubleshooting

`voice doctor` runs a checklist and prints `✔`/`✘` per item; failures marked
`(optional)` don't affect the exit code:

- **config** — `config.toml` parses and passes validation.
- **keyboard access** — at least one input device is readable without root. Re-run
  `./install.sh` (installs the udev rule) or add yourself to the `input` group and
  log out/in. A device already open before the rule existed may need re-plugging.
- **pw-record** — PipeWire's recording tool is on PATH.
- **wl-clipboard** — `wl-copy`/`wl-paste` are installed.
- **portal** — the `RemoteDesktop` portal is reachable (`xdg-desktop-portal-kde` on KDE,
  `xdg-desktop-portal-gnome` on GNOME). If the permission dialog needs revoking or
  doesn't reappear, check KDE System Settings → Applications → Remote Desktop.
- **cuda** *(optional)* — an NVIDIA GPU is visible to `ctranslate2`; without it (or on a
  machine with none), the local backend runs on CPU int8 automatically — slower, but it
  works, and this line explains why nothing is using the GPU.
- **microphones** *(optional)* — at least one PipeWire source is visible.
- **model cache** *(optional)* — whether the active local model has already downloaded.
- **notify-send**, **fallback senders** *(optional)* — desktop notifications, and
  `wtype`/`ydotool` as a paste fallback if the portal chord path is ever unavailable.

Two more common issues doctor doesn't cover directly:

- **Paste doesn't work in Konsole (or another terminal).** Terminals bind Ctrl+V to
  something else; make sure the window class is in `inject.terminal_classes` (already
  includes `konsole`, `kitty`, `alacritty`, `foot`, `wezterm`, `gnome-terminal`) so
  `voice` sends Ctrl+Shift+V there instead.
- **A clipboard manager is recording every dictation.** Klipper (and similar) keeps a
  history entry per dictation, since `voice` pastes via the clipboard. Exclude
  `voice`-owned changes in Klipper's settings, or live with the history.

## Uninstall

```
./install.sh --uninstall
```

This removes the `~/.local/bin/voice` wrapper, both desktop entries, and the udev rule
(asks for sudo once). It leaves your data behind and prints the paths; delete them
yourself if you want a clean slate:

- `~/.config/voice` — config file.
- `~/.local/state/voice` — history, portal restore token.
- `~/.cache/huggingface` — downloaded local models (shared with other tools that use it).

Then remove the cloned project directory.

## Roadmap

Phase 1 (this repo, in progress): config, evdev hotkey, capture, local + OpenAI-compatible
STT, inject, tray, notifications, history/recall, CLI, doctor, install script, README.

- **Phase 2 — Polish**: an `openai_chat` backend that rewrites the raw transcript (fix
  punctuation, drop filler words, or restyle as an email/chat message), presets, a tray
  toggle and a settings tab.
- **Phase 3 — Text-to-speech**: `kokoro`, `chatterbox` and `openai_speech` backends,
  per-voice profiles (including a Swedish Chatterbox fine-tune), and a "read selection
  aloud" hotkey.
- **Phase 4 — Nice-to-have**: a floating on-screen pill via layer-shell on KWin, the
  GlobalShortcuts portal as a permission-free hotkey alternative, and direct typing via
  libei text events once KWin/Mutter ship it.

## External components

- faster-whisper — https://github.com/SYSTRAN/faster-whisper
- Whisper large-v3-turbo — https://huggingface.co/Systran/faster-whisper-large-v3-turbo
- KB-Whisper (Swedish) — https://huggingface.co/KBLab/kb-whisper-large
- Silero VAD — https://github.com/snakers4/silero-vad
- python-evdev — https://github.com/gvalkov/python-evdev
- wl-clipboard — https://github.com/bugaevc/wl-clipboard
- xdg-desktop-portal RemoteDesktop — https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html
- PySide6 — https://doc.qt.io/qtforpython-6/
- uv — https://docs.astral.sh/uv/
- Ollama — https://ollama.com (Arch package `ollama-cuda`)
- OpenAI speech-to-text — https://developers.openai.com/api/docs/guides/speech-to-text
- OpenRouter transcription — https://openrouter.ai/docs/api/api-reference/stt/create-transcription
- Groq speech-to-text — https://console.groq.com/docs/speech-to-text
- Kokoro — https://github.com/thewh1teagle/kokoro-onnx
- Chatterbox — https://github.com/resemble-ai/chatterbox
- Chatterbox Swedish fine-tune — https://huggingface.co/Parambe/Chatterbox-Swedish-Parambe-V1
- Prior art studied: voxtype (https://github.com/peteonrails/voxtype), hyprwhspr (https://github.com/goodroot/hyprwhspr), Handy (https://github.com/cjpais/Handy)

## License

To be decided by the owner. No `LICENSE` file yet — treat this as all-rights-reserved
until one is added.
