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
- Optional, for the recording pill: `python-gobject` with GTK 4 (system package, already
  present on KDE and GNOME) and `gtk4-layer-shell` (Debian/Ubuntu:
  `gir1.2-gtk4layershell-1.0`). See "Recording pill" below for why the second one is not
  really optional. The JetBrains Mono font is used for the timer if it is installed.

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
voice language <code>  # switch dictation language ("sv", "auto", or "next" to cycle)
voice reload           # re-read config.toml without restarting
voice quit             # stop the daemon
voice doctor           # check this machine for everything voice needs
voice --verbose ...    # DEBUG logging to stderr (default: INFO)
voice --version
```

### Recording pill

While you dictate, a small dark capsule floats near the bottom of the screen: a live
waveform driven by the microphone level, an elapsed `m:ss` counter, and a language badge
("EN", "SV", "AUTO"). It turns into a progress line while transcribing, flashes a
checkmark when the text is inserted, and shows the error in amber for two seconds when
something fails. It is never clickable and never takes focus.

It runs as a separate helper process (`voice.ui.overlay`, GTK 4 through PyGObject), so a
crash there cannot affect dictation - the daemon logs it, restarts it once, and carries
on without it. Switch it off with `ui.overlay = false`, or move it with
`ui.overlay_position = "top"`.

**gtk4-layer-shell is required in practice.** GTK 4 removed the "do not focus me" window
hints, so without that library the pill is an ordinary window that takes keyboard focus
when it appears - and the paste would land in the pill instead of your editor. When the
library is missing the daemon leaves the pill off and says so once in the log and in
`voice doctor`. If you want it anyway, start the daemon with
`VOICE_OVERLAY_ALLOW_PLAIN_WINDOW=1`.

### Language

`general.language` is the language passed to the transcriber (`"auto"` detects it), and
`general.languages` is the cycle the fast switch walks through:

```toml
[general]
language = "en"
languages = ["en", "sv"]

[hotkeys]
language_toggle = "KEY_F15"        # evdev backend
portal_language_toggle = "CTRL+ALT+l"   # portal backend
```

Pressing the toggle moves to the next language in the list, wrapping around; the pill
shows "EN → SV" for two seconds. The same switch is in the tray's Language submenu and on
the command line (`voice language sv`, `voice language next`), and `voice status` reports
which one is active. Every switch is saved to `config.toml` and applies to the next
dictation.

## Configuration

`~/.config/voice/config.toml` is created with defaults and comments on first run (mode
`0600`) and is safe to hand-edit — it's read and written with `tomlkit`, so your comments
survive settings-window saves. The defaults:

```toml
# voice configuration. Edited by the settings window; hand edits are fine too.

[general]
language = "en"            # "en", "sv", or "auto"
languages = ["en", "sv"]   # cycle order for the language toggle
notifications = true

[hotkeys]
backend = "auto"           # "auto" | "evdev" (kernel devices) | "portal" (desktop shortcuts)
dictate = "KEY_F13"        # any evdev key, or a combination like "KEY_LEFTMETA+KEY_SPACE"
dictate_mode = "hold"      # "hold" (push-to-talk) or "toggle"
recall = ""                # re-insert the last dictation
cancel = "KEY_ESC"         # discard the current recording
language_toggle = ""       # cycle through general.languages
# Portal backend triggers (XDG shortcut syntax). Compositors reject bare
# modifiers, so these need a combination. Empty = not bound.
portal_dictate = "CTRL+space"
portal_recall = ""
portal_cancel = ""
portal_language_toggle = ""

[audio]
device = ""                # PipeWire source node name; "" = default source
max_seconds = 120

[ui]
overlay = true             # the recording pill: waveform, timer, language badge
overlay_position = "bottom"  # "bottom" | "top"

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

**Hotkey backend.** `hotkeys.backend` decides how the hotkey is seen:

- `evdev` reads the kernel input devices directly. It sees any key, including a bare
  modifier such as `KEY_RIGHTCTRL`, but needs read access to `/dev/input` (the udev rule
  or the `input` group) and a local seat.
- `portal` asks the desktop to bind a global shortcut through
  `org.freedesktop.portal.GlobalShortcuts` (KDE Plasma, GNOME 48+). No device
  permissions, and it works in a remote-desktop session, because the compositor sees the
  keystroke before anything else does. The trigger comes from `hotkeys.portal_dictate`,
  **Ctrl+Space** by default; `portal_recall` and `portal_cancel` are bound too when set.
  Compositors refuse a bare modifier as a global shortcut, so these must be
  combinations — a lone `CTRL` will not bind. Your desktop may also let you rebind the
  shortcut in its own settings, which wins over the config file.
- `auto` (the default) picks `evdev` when at least one keyboard is readable *and* the
  session has a local seat, and `portal` otherwise. `voice doctor` prints the choice and
  the reason (`hotkey backend: portal (no local seat)`), and so does `voice status`.

The portal backend needs the desktop entry `install.sh` writes
(`~/.local/share/applications/io.github.vampyren.voice.desktop`): the portal resolves the
app id through it, and refuses the shortcut session with "An app id is required" without
it. Changing `hotkeys.backend` or a portal trigger takes effect on the next daemon start, not on
`voice reload` — the portal session is created once and reused while the daemon runs.

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
- **hotkey backend** — informational: which listener the daemon would use here and why
  (`evdev`, or `portal (no local seat)`). Never fails the run; see
  [Configuration](#configuration).
- **overlay** — informational: whether the recording pill can run here, which
  interpreter starts its helper, and whether `gtk4-layer-shell` was found. Never fails
  the run; see [Recording pill](#recording-pill).
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
- **Remote-desktop sessions (RDP, VNC, GNOME Remote Desktop, a VM console reached
  remotely).** Two limits apply when your desktop session is not on the machine's
  physical seat. First, the udev rule grants keyboard access to the *active local seat*
  user, which in a remote session is usually the login greeter, so `voice doctor` keeps
  reporting no keyboard access: add yourself to the `input` group instead
  (`sudo usermod -aG input $USER`, then log out and in; `sudo setfacl -m u:$USER:rw
  /dev/input/event*` works immediately for the current boot). Second, keystrokes in a
  remote session arrive through the remote-desktop server, not the kernel input devices,
  so the push-to-talk key is never seen. **The portal hotkey backend is the answer here,
  and `hotkeys.backend = "auto"` already switches to it**: no local seat means the daemon
  binds Ctrl+Space (`hotkeys.portal_dictate`) through the desktop instead of reading
  `/dev/input`, so no `input` group membership is needed either. Run `./install.sh` first
  — the portal needs the desktop entry it installs to resolve this app's id. What remains
  is the paste: the portal keystroke is not delivered to the focused window in some remote
  sessions, so if Ctrl+V never arrives, set `inject.restore_clipboard = false` and paste
  yourself, or drive dictation from the command line (`voice toggle`, or `voice start`
  with a short `audio.max_seconds`).

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
- **Phase 4 — Nice-to-have**: direct typing via libei text events once KWin/Mutter ship
  it. (The GlobalShortcuts portal hotkey backend and the layer-shell recording pill both
  landed early, in phase 1 — see `hotkeys.backend` and `ui.overlay`.)

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
