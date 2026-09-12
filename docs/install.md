# Installing voice

[← back to the README](../README.md)

Everything needed to get `voice` running, and to remove it again.

## Requirements

CachyOS or another Arch-based distro on Wayland. **KDE Plasma and GNOME are both
supported**, and they differ in ways that matter — see [Desktop setup](desktops.md).
The short version: KDE can tell voice which window has focus (so it picks the right
paste shortcut automatically), and GNOME cannot.

### Do I need a GPU?

**No.** The graphics card does one thing: it makes transcription faster. Everything
works without one.

Measured on this project's own test machine — a virtual machine with **no GPU at all**,
across 20 real dictations:

| You spoke for | It took | |
|---|---|---|
| 13.9 s | 3.1 s | 4.5× faster than real time |
| 25.1 s | 5.3 s | 4.7× |
| 30.5 s | 9.5 s | 3.2× |

Median **3.4× faster than real time**: half a minute of speech is transcribed in about
nine seconds, on a machine with nothing helping it. A powerful desktop CPU does better.

A GPU brings that down further, which is worth having if you dictate long passages — but
it is a convenience, not a requirement. Build with `VOICE_GPU=0` and you save roughly
3 GB of CUDA runtime as well.

Text-to-speech does not exist yet (see [Roadmap](roadmap.md)); when it does, it will also
run on CPU.

**You do not install any of the tables below by hand.** `makepkg -si` installs the
dependencies and the package carries the bundled set. They are listed so you know what
lands on the machine and why.

### System packages, installed for you as dependencies

Everything here is in Arch's official repositories (`core` or `extra`) — nothing comes
from the AUR, which is what lets `makepkg -si` work on a clean machine.

| package | what it is for |
| --- | --- |
| `python` | Arch's own interpreter — the app and its bundled libraries run on it |
| `pipewire` | `pw-dump`, which lists your microphones |
| `pipewire-audio` | `pw-record`, the recording itself (this is *not* part of `pipewire`) |
| `wl-clipboard` | `wl-copy` / `wl-paste` — how the transcribed text reaches the focused window |
| `libnotify` | `notify-send`, the desktop notifications |
| `xdg-desktop-portal` | the portal service: sends the paste keystroke and owns the push-to-talk shortcut |
| `xdg-desktop-portal-impl` | a portal implementation to go with it — `xdg-desktop-portal-kde` on KDE, `-gnome` on GNOME |
| `python-gobject` | GTK bindings the recording pill is built on |
| `gtk4` | the toolkit that draws the pill |
| `python-cairo` | the pill's drawing |
| `gtk4-layer-shell` | lets the pill float above other windows without stealing focus |
| `nvidia-utils` | `libcuda.so.1`, the driver half of CUDA (GPU build only — not in a `VOICE_GPU=0` build) |

### What ships inside the package

These are Python libraries the package brings its own copies of, under `/usr/lib/voice`.

| bundled | why it is bundled instead of being a dependency |
| --- | --- |
| **faster-whisper**, **CTranslate2** | not in the official repos; the AUR `ctranslate2` is built with CUDA switched off, so depending on it would mean CPU-only transcription on an NVIDIA card |
| **onnxruntime** | no Arch package at all, in any repository — and the Silero voice-activity detection needs it |
| **PySide6** | `pyside6` does exist in `extra`, but the tray and settings window are tested against the exact version in `uv.lock`; the distro package moves on its own schedule |
| **numpy**, **httpx**, **tomlkit**, **evdev**, **jeepney** | likewise all in `extra`, and all bundled for the same reason: one locked, tested set rather than a mixture that no test has ever run against |
| **CUDA 12 libraries** (cuBLAS, cuDNN, NVRTC) | Arch ships CUDA **13**, whose `libcublas.so.13` the CTranslate2 build cannot load — it opens `libcublas.so.12`. Arch's `cuda` would be 6 GB and still fall back to the CPU |

Because these are compiled for one CPython version, **the package needs rebuilding
(`makepkg -si`) when Arch moves `python` to a new minor version** — see
[`packaging/README.md`](../packaging/README.md) for the detail.

### Optional extras

Not installed by default; pacman lists them as optional dependencies.

| package | what it buys you |
| --- | --- |
| `wtype` | fallback key injection where the portal cannot send the paste chord |
| `ydotool` | the other fallback injection backend |
| `ttf-jetbrains-mono` | the font for the pill's timer (any monospace font does) |
| `dconf` | lets `voice doctor` read back the shortcut GNOME stored |
| `uv` | only for the git-clone development route, not for the package |

### Downloaded on first use, not shipped

The speech models are fetched the first time you dictate and cached under
`~/.cache/huggingface` — shared with any other Hugging Face tool on the machine.

| model | for | download |
| --- | --- | --- |
| `large-v3-turbo` | English (the default) | ~1.6 GB |
| `KBLab/kb-whisper-large` | Swedish, set up separately | ~3.1 GB |

### Separate software, only for optional features

Neither is a dependency and neither is installed for you.

| feature | what you would install |
| --- | --- |
| Phase 2 transcript polish (punctuation, filler removal, restyling) | `ollama-cuda` (in `extra`) plus a small local model to run against it |
| Phase 3 text-to-speech | the Kokoro / Chatterbox stack, which is not packaged for Arch and is not built yet |

### What you probably already have

On a standard CachyOS KDE install with the NVIDIA drivers in place, most of the first
table is already there. Ask pacman rather than guessing — this prints **only what is
missing**, and nothing at all if you have everything:

```
pacman -T python pipewire pipewire-audio wl-clipboard libnotify \
  xdg-desktop-portal xdg-desktop-portal-kde python-gobject gtk4 \
  python-cairo gtk4-layer-shell nvidia-utils
```

Measured on one CachyOS KDE machine, 2026-09-11: three names came back —
`python312`, `wl-clipboard` and `gtk4-layer-shell`. `python312` is no longer a dependency
(the package uses Arch's `python` now), which leaves **two packages, both in `extra`**,
and `makepkg -si` installs them itself. Your machine may differ; the command above is the
answer for yours.

## Install on Arch / CachyOS

The supported install is the package: pacman owns every file and removes them all again.

```
git clone https://github.com/vampyren/voice ~/Apps/voice
cd ~/Apps/voice/packaging
makepkg -si                                     # builds and installs `voice`, GPU included
voice doctor
```

One package. It carries the app, its locked Python dependency set, `/usr/bin/voice`, the
desktop entry, the autostart entry, the udev rule **and the CUDA 12 runtime**, so
transcription uses your NVIDIA GPU straight after the install — `voice doctor`'s **cuda**
line confirms it. GPU support is a hard dependency (`nvidia-utils`), not an extra.

`makepkg` needs only `uv` and `git` to build — everything it depends on is in the
official repositories, so there is nothing to fetch from the AUR first. It downloads the
locked wheels during the build (so build with plain `makepkg`, not a network-less
chroot) and installs about 4 GB (1.2 GB app and dependencies, roughly 3 GB of CUDA
runtime). The app runs on Arch's own `python`, out of `/usr/lib/voice` rather than
site-packages; `packaging/README.md` explains why, and why that means rebuilding the
package when Arch moves to a new Python version.

Remove it with `pacman -R voice`. See [Uninstall](#uninstall) for the three directories
under your home that a package must not delete.

### No NVIDIA card? The CPU-only build

```
cd ~/Apps/voice/packaging
VOICE_GPU=0 makepkg -si
```

Same package, same name, same paths — it just leaves out the ~3 GB of CUDA wheels and the
`nvidia-utils` dependency, and transcription runs on CPU int8. This is the exception, not
the recommended install; use the plain `makepkg -si` above if the machine has a GPU.

## Install from the git clone (development)

This is the development route: it keeps the code in your checkout, in a uv virtualenv, so
an edit is live the next time the daemon starts. Use the package for a machine you just
want to dictate on.

```
git clone https://github.com/vampyren/voice ~/Apps/voice && cd ~/Apps/voice
./install.sh          # uv sync --extra gpu, udev rule (sudo once), autostart, ~/.local/bin/voice
voice doctor
```

Only one of the two at a time: `~/.local/bin/voice` comes first on most PATHs and would
shadow the packaged `/usr/bin/voice`. Run `./install.sh --uninstall` before installing the
package, or `pacman -R voice` before going back to the clone.

`install.sh` is idempotent and safe to re-run. Flags:

- `--cpu` / `--gpu` — force the dependency set instead of auto-detecting `nvidia-smi`.
- `--no-udev` — skip the udev rule (you'll need to add yourself to the `input` group,
  or a keyboard device with no `uaccess` tag, and re-login, instead).
- `--uninstall` — remove the wrapper, desktop entries and udev rule (see Uninstall below).

It writes a `~/.local/bin/voice` wrapper so the command works from anywhere without
activating a virtualenv, installs `voice.desktop` to both
`~/.local/share/applications/` (app launcher) and `~/.config/autostart/` (login
autostart), and finishes by running `voice doctor`.

## Uninstall

Packaged install:

```
sudo pacman -R voice
```

Git clone — this one **asks** whether to delete your settings as well:

```
./install.sh --uninstall                     # asks, and keeps them if you say no
./install.sh --uninstall --purge             # delete settings and history too
./install.sh --uninstall --keep-settings     # never ask, always keep
```

With no terminal to ask on (a script, CI) your files are kept, because that is the
answer you can undo.

### What is left behind, and how to clear it

Removing the package removes every file it owned. Nothing under your home is touched,
because none of it belongs to the package:

| What | Where | |
|---|---|---|
| Settings | `~/.config/voice` | `config.toml` — your key, language, profiles |
| History | `~/.local/state/voice` | past dictations, and the portal permission token |
| Speech models | `~/.cache/huggingface` | ~1.6 GB, **shared with any other Hugging Face tool** |
| Autostart override | `~/.config/autostart/io.github.vampyren.voice.desktop` | only if you made one by hand |

```bash
rm -rf ~/.config/voice ~/.local/state/voice
rm -f  ~/.config/autostart/io.github.vampyren.voice.desktop

# just voice's models, leaving anything else that uses Hugging Face alone:
rm -rf ~/.cache/huggingface/hub/models--Systran--faster-whisper-*
```

**A daemon that was already running keeps going** until you log out. Stop it now with:

```bash
pkill -f 'voice daemon'
```

`pacman -R` prints this same list when it removes the package, so you do not have to
come back here for it.

### What the udev rule actually does

The whole rule is one line:

```
SUBSYSTEM=="input", KERNEL=="event*", TAG+="uaccess"
```

`uaccess` is a systemd-logind tag. Tagging a device with it makes logind put an ACL on
that device for **the user of the active local session** — the person physically logged
in at the screen — and remove it again when they log out.

So voice can read your push-to-talk key without running as root, without you joining the
`input` group, and without a re-login. The device does not become world-readable, and a
remote or SSH session is not the active local session, so it gets nothing.

It only matters for the **evdev** hotkey backend. On GNOME — and on any session with no
local seat — voice uses the desktop's own global-shortcut portal instead, and the rule is
unused. `voice doctor`'s **hotkey backend** line says which one you are on.

`--no-udev` skips it: then you need to be in the `input` group (and re-login), or use the
portal backend.

`pacman -R` removes every file the package owns, including the udev rule and both desktop
entries, and reloads udev. `install.sh --uninstall` removes the `~/.local/bin/voice`
wrapper, both desktop entries, and the udev rule (asks for sudo once). Neither touches
your data; delete it yourself if you want a clean slate:

- `~/.config/voice` — config file.
- `~/.local/state/voice` — history, portal restore token.
- `~/.cache/huggingface` — downloaded local models (shared with other tools that use it).

Then remove the cloned project directory (the package install does not need it either,
once the package is built).

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
