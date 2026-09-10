# voice — Wayland-native voice typing for CachyOS/KDE

Design spec, 2026-09-10. Status: awaiting owner review.

## 1. Goal

Hold a key, speak, release, and the words appear in whatever text field has focus.
Transcription runs locally on the RTX 4090 by default and can be switched to any paid
speech-to-text provider from a config file or the settings window. Optional "polish"
rewrites the raw transcript with a local or cloud language model. A later phase adds
text-to-speech ("read selection aloud") with cloneable voices.

The experience target is Vibe Typer's simplicity. The install target is: one project
directory plus one config directory, nothing else on the system except a udev rule and
optional packages the owner chooses (ollama-cuda).

### Non-goals

- X11 support. Wayland only. XWayland apps receive text the same way as native ones
  because injection goes through the compositor, but nothing X11-specific is written.
- Live/streaming transcription while speaking. Text is delivered after key release.
- Meeting mode, diarization, note-taking. This is dictation.
- Mobile, Windows, other distros. Arch/CachyOS + KDE Plasma is the target;
  GNOME is supported only because the development VM runs it.
- macOS in this version. The owner plans a Mac later, so the platform-specific code is
  confined to `hotkey/`, `audio/capture.py` and `inject/` (section 3); a port replaces
  those three with a Quartz event tap, PortAudio capture, and pbcopy + Cmd+V, and swaps
  the local STT backend for whisper.cpp (Metal) or MLX. Nothing else may import
  Linux-only libraries.

## 2. Target environment

| | Owner's PC (target) | Dev VM (this session) |
|---|---|---|
| OS | CachyOS (Arch), KDE Plasma latest, Wayland | Ubuntu 26.04, GNOME 50, Wayland, KVM |
| GPU | NVIDIA RTX 4090, 24 GB | none |
| Mic | OBSBOT Tiny 3, Hollyland Lark | Hollyland (USB pass-through) |
| Keyboard | Keychron, Swedish layout, spare keys top-right (circle/triangle/square) | virtual |
| Present on system | pyside6, python-gobject, wl-clipboard, libnotify, pipewire, xdg-desktop-portal-kde | ffmpeg, pipewire, xdg-desktop-portal-gnome |

Everything GPU- and KDE-specific is verified by the owner on the real PC through
`voice doctor`. The VM verifies audio capture, clipboard, portal injection on GNOME,
and every non-GPU code path.

## 3. Architecture

One long-running Python process (the daemon) started at login. It owns:

- the tray icon and settings window (PySide6),
- the push-to-talk listener (evdev),
- audio capture (pw-record subprocess),
- the active STT backend, the active polish backend, and in phase 3 the TTS backends,
- text injection (wl-clipboard + desktop portal),
- dictation history.

A thin CLI (`voice start|stop|toggle|cancel|recall|polish on|off|toggle|say|status|settings|doctor`)
talks to the daemon over a Unix socket at `$XDG_RUNTIME_DIR/voice.sock` using
newline-delimited JSON. Running `voice` with no arguments starts the daemon (or raises the
settings window if one is already running). The CLI exists so the owner can also bind
compositor shortcuts to `voice toggle` if evdev is ever unwanted.

### Package layout

```
voice/
  __init__.py        APP_NAME, APP_ID, version           (rename = edit here + pyproject)
  cli.py             argparse entry point, socket client
  daemon.py          wires modules, state machine, socket server
  config.py          load/save TOML (tomlkit, comments preserved), defaults, validation
  hotkey/evdev.py    device discovery, hot-plug, key-spec parsing, press/release events
  audio/capture.py   pw-record wrapper -> 16 kHz mono int16 buffer; device listing
  audio/vad.py       silence trim via faster-whisper's bundled Silero VAD (onnxruntime)
  stt/base.py        Transcriber protocol: transcribe(pcm, language, prompt) -> Result
  stt/local.py       faster-whisper backend
  stt/openai_compat.py  /v1/audio/transcriptions backend (OpenAI, Groq, Mistral, OpenRouter, Together, any)
  polish/base.py     Polisher protocol
  polish/openai_chat.py  /v1/chat/completions backend (Ollama local, OpenAI, OpenRouter, any)
  inject/clipboard.py  wl-copy / wl-paste, save+restore
  inject/portal.py   RemoteDesktop portal session, restore token, keycode chords
  inject/inject.py   strategy: copy -> wait modifiers released -> chord -> restore
  history.py         last N dictations, recall, retry-last-audio
  ui/tray.py         QSystemTrayIcon, state icons, menu
  ui/settings.py     settings dialog, hotkey capture, profile editors
  ui/notify.py       notify-send wrapper
  doctor.py          environment checks
  tts/ (phase 3)     base.py, kokoro.py, chatterbox.py, openai_speech.py, play.py
```

Each module depends only on `config` and its own `base` protocol. Backends are selected
by name from config, so adding a provider is one new file plus a registry entry.

### Dictation state machine

```
IDLE --key down--> RECORDING --key up--> TRANSCRIBING --ok--> INJECTING --> IDLE
                                 |                     |--error--> IDLE (notify, audio kept)
                                 |--cancel/too short--> IDLE
toggle mode: key press alternates IDLE<->RECORDING; a second press while TRANSCRIBING is ignored
```

Timings: recordings shorter than 300 ms after VAD trim are discarded silently. A maximum
duration (default 120 s) stops recording and transcribes. Tray icon and a notification
reflect every state; the notification for TRANSCRIBING is only shown if it exceeds 1 s.

## 4. Data flow per dictation

1. Key down: spawn `pw-record --rate 16000 --channels 1 --format s16 [--target <id>] -`
   and read stdout into a buffer. Device comes from config or PipeWire default.
2. Key up: terminate pw-record, run Silero VAD, keep speech spans with 200 ms padding.
3. STT backend transcribes. Local: faster-whisper on CUDA, `large-v3-turbo`, fp16,
   `initial_prompt` from config (vocabulary hint), language fixed or auto.
   Cloud: PCM encoded to WAV in memory, multipart POST, `.text` parsed from JSON.
4. Dictionary replacements apply (ordered, case-sensitive optional, regex optional).
5. If polish is enabled: transcript sent to the polish backend with the active preset's
   system prompt; the reply replaces the transcript. Failure falls back to the raw text
   with a notification.
6. Inject (section 6). Text appended to history with timestamp, backend, duration.

## 5. Configuration

File: `~/.config/voice/config.toml`, created with defaults and comments on first run,
mode 0600. Edited by the settings window through tomlkit so comments survive.
Secrets: `api_key = "sk-..."` inline, or `api_key_env = "OPENAI_API_KEY"`.

```toml
[general]
language = "en"            # "en", "sv", or "auto"
polish_enabled = false
notifications = true

[hotkeys]
dictate = "KEY_F13"        # any evdev key or "KEY_LEFTMETA+KEY_SPACE"
dictate_mode = "hold"      # "hold" or "toggle"
recall = ""                # re-insert last dictation
polish_toggle = ""
read_selection = ""        # phase 3

[audio]
device = ""                # PipeWire node name; "" = default source
max_seconds = 120

[stt]
active = "local"

[stt.profiles.local]
backend = "local"
model = "large-v3-turbo"   # or "KBLab/kb-whisper-large" for Swedish
device = "cuda"            # falls back to cpu/int8 with a warning
compute_type = "float16"
beam_size = 5
prompt = ""                # vocabulary hint

[stt.profiles.openai]
backend = "openai_compatible"
base_url = "https://api.openai.com/v1"
model = "gpt-transcribe"
api_key_env = "OPENAI_API_KEY"
prompt = ""

[stt.profiles.openrouter]
backend = "openai_compatible"
base_url = "https://openrouter.ai/api/v1"
model = "openai/whisper-large-v3-turbo"
api_key_env = "OPENROUTER_API_KEY"
# OpenRouter ignores prompt; the backend drops it when base_url matches

[polish]
active = "ollama"
preset = "clean"

[polish.profiles.ollama]
backend = "openai_chat"
base_url = "http://localhost:11434/v1"
model = "qwen3:4b"

[polish.profiles.openai]
backend = "openai_chat"
base_url = "https://api.openai.com/v1"
model = "gpt-5-mini"
api_key_env = "OPENAI_API_KEY"

[polish.presets]
clean = "Fix punctuation and capitalisation. Remove filler words and self-corrections. Keep wording and language. Output only the text."
email = "Rewrite as a clear, polite email body in the same language. Output only the text."
chat = "Tidy into a casual chat message. Output only the text."

[dictionary]
replacements = [ ["cachy os", "CachyOS"], ["obs bot", "OBSBOT"] ]

[inject]
paste_chord = "ctrl+v"
terminal_chord = "ctrl+shift+v"
terminal_classes = ["konsole", "org.kde.konsole", "kitty", "alacritty", "foot", "wezterm", "org.gnome.Ptyxis"]
restore_clipboard = true
```

Provider presets (OpenAI, Groq, Mistral, OpenRouter, Together, Ollama) appear in the
settings window as "add profile from template" so the owner only pastes a key.

## 6. Wayland specifics

**Hotkey.** evdev reads every keyboard-class device under `/dev/input`, watches for
hot-plug with inotify, and emits press/release for the configured key spec. Access is
granted by a udev rule `TAG+="uaccess"` on input event devices, installed once by the
install script with sudo; no group change, no re-login. The settings window has a
"press a key" capture that shows the exact evdev name received, which is how the
Keychron circle/triangle/square keys get bound whatever code the firmware sends.
Fn-combinations are resolved in the keyboard firmware and are only bindable if the
firmware emits a distinct code.

**Injection.** Direct character typing on KWin and Mutter is unreliable for å ä ö, so
the app copies then pastes:

1. Save current clipboard (`wl-paste --list-types`, `wl-paste`), text only; non-text
   clipboards are not restored and a note is logged.
2. `wl-copy` the transcript.
3. Wait until evdev reports the hotkey's modifiers released (so the chord cannot combine
   with a still-held key).
4. Send the chord as keycodes through the RemoteDesktop portal
   (`org.freedesktop.portal.RemoteDesktop`: CreateSession → SelectDevices(keyboard,
   persist_mode=2, restore_token) → Start → NotifyKeyboardKeycode). Keycodes are
   layout-independent, so the Swedish layout is irrelevant. The first run shows one
   KDE permission dialog; the restore token is saved to the state dir so it never asks
   again. A dead session (after suspend/lock) is recreated transparently.
5. After 150 ms restore the clipboard.

The chord is `ctrl+shift+v` when the active window class is in `terminal_classes`,
otherwise `ctrl+v`. Active window class is read on KDE through KWin's scripting D-Bus
interface; where that fails (GNOME) the default chord is used. Users can always add
window classes.

Fallbacks: if the portal is unavailable, `wtype` (wlroots) then `ydotool` are tried
for the chord if installed, and finally the text is left on the clipboard with a
notification "paste with Ctrl+V". The direct-typing path via libei text events is
noted for later, once KWin and Mutter ship it.

**Indicator.** Tray icon via StatusNotifierItem (native on KDE; GNOME needs the
AppIndicator extension, VM only). Icons: idle mic, red recording, spinner transcribing,
warning on error. Notifications via `notify-send`. A floating on-screen pill is phase 4
because it needs layer-shell, an extra package on KDE.

## 7. Phase 3: text-to-speech

Trigger: `read_selection` hotkey or `voice say "text"`. Text comes from the primary
selection (`wl-paste --primary`), falling back to the clipboard. Playback via
`pw-play`. Speed is a per-voice setting; no pitch control.

Backends, all in-process, installed as optional extras so nothing leaves the project
directory:

- `kokoro` (`uv sync --extra tts`): kokoro-onnx, ~300 MB model, ~50 preset voices,
  Apache-2.0. English (plus a few languages), no Swedish. Voice = preset name.
- `chatterbox` (`uv sync --extra tts`): Chatterbox Multilingual v3, PyTorch CUDA
  (~5 GB libraries, 2–4 GB model, ~8 GB VRAM while loaded), MIT. Voice = path to a
  5–10 s reference clip plus `language_id` ("en", "sv") and exaggeration. The Swedish
  fine-tune Parambe-V1 (Apache-2.0) is selectable by model id. Model loads on first
  use and unloads after an idle timeout so it does not hold VRAM permanently.
- `openai_speech`: any `/v1/audio/speech` endpoint (OpenAI cloud, or a local
  Kokoro-FastAPI/Speaches server).

```toml
[tts]
active = "jarvis"

[tts.voices.assistant]
backend = "kokoro"
voice = "af_heart"
speed = 1.1

[tts.voices.jarvis]
backend = "chatterbox"
reference = "~/.config/voice/voices/jarvis.wav"
language = "en"
exaggeration = 0.5
speed = 1.0
```

## 8. Error handling

| Situation | Behaviour |
|---|---|
| No CUDA at startup | local backend loads on CPU int8, tray warns once, doctor explains |
| Model not downloaded | download on first use with a notification and progress in the tray tooltip |
| Cloud request fails / times out | notification with the reason; audio kept; `recall`/retry re-sends without re-speaking |
| Polish fails | inject raw transcript, notify |
| Portal denied or missing | fallback chain (section 6), never lose the text |
| pw-record fails | notification naming the device; fall back to default source next time |
| Daemon already running | second launch raises the settings window and exits |
| Config invalid | daemon starts with defaults, tray warns, settings window shows the error |

## 9. Testing

- Unit: config load/save round-trip, key-spec parsing, replacement rules, WAV encoding,
  VAD trimming, provider request shaping (prompt dropped for OpenRouter, `languages[]`
  for gpt-transcribe), clipboard save/restore logic.
- Interaction: the state machine driven by a fake hotkey source, fake capture, fake
  backends and controlled timers, asserting every transition in section 3 including
  cancel, too-short, error, and toggle mode.
- Boundary (VM, marked `boundary`): pw-record captures real audio; wl-copy/wl-paste
  round-trip; portal session creation and a chord into a test window on GNOME; socket
  IPC; tray icon renders.
- GPU (owner's PC, marked `gpu`): faster-whisper on CUDA transcribes a fixture clip in
  under 1 s; `voice doctor` reports device, VRAM, model cache, input access, portal,
  mic list. Owner pastes the doctor output back.
- Visual: settings window and tray states screenshotted in the VM and inspected.

## 10. Install and footprint

```
git clone https://github.com/vampyren/voice ~/Apps/voice && cd ~/Apps/voice
./install.sh          # uv sync --extra gpu, udev rule (sudo once), autostart, ~/.local/bin/voice
voice doctor
```

- Python 3.12 pinned by uv; system Python untouched.
- Core: faster-whisper, ctranslate2, onnxruntime, PySide6, evdev, httpx, tomlkit.
  `gpu` extra adds the nvidia-cublas-cu12 and nvidia-cudnn-cu12 wheels (~1.5 GB).
  `tts` extra adds kokoro-onnx, chatterbox-tts and PyTorch CUDA (~5 GB).
- Models cached under `~/.cache/huggingface` (whisper, chatterbox) and
  `~/.cache/voice` (kokoro).
- Uninstall: delete the project directory, `~/.config/voice`, `~/.local/state/voice`,
  the udev rule and the autostart entry. `./install.sh --uninstall` does all of it.
- License: to be decided by the owner; no LICENSE file until then, README says so.

## 11. Phases

1. **Dictation core**: config, evdev hotkey, capture, local + OpenAI-compatible STT,
   inject, tray, notifications, history/recall, CLI, doctor, install script, README.
2. **Polish**: openai_chat backend, presets, tray toggle, settings tab.
3. **TTS**: kokoro + chatterbox + openai_speech, voice profiles, read-selection hotkey.
4. **Nice-to-have**: layer-shell pill on KWin, GlobalShortcuts portal as a
   permission-free hotkey alternative, direct typing via libei text events.

## 12. External components (linked from README)

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
