# voice

**Wayland-native voice typing for Linux.** Hold a key, speak, release — your words appear
in whatever window you are working in.

Transcribes locally on your NVIDIA GPU with faster-whisper, or through any
OpenAI-compatible speech-to-text API. Swedish and English out of the box, switchable with
a shortcut.

> **Status:** phase 1 — the dictation core, in daily use on KDE Plasma and GNOME.

---

## Install

### Arch / CachyOS — the supported way

```bash
git clone https://github.com/vampyren/voice ~/Apps/voice
cd ~/Apps/voice/packaging
makepkg -si
voice doctor
```

One package: the app, its locked dependencies, the CUDA 12 runtime, the udev rule, the
desktop entry and autostart. Remove it with `pacman -R voice`.

No NVIDIA card? `VOICE_GPU=0 makepkg -si` — same package, ~3 GB smaller, transcribes on
CPU.

### Any distro — from the checkout

```bash
git clone https://github.com/vampyren/voice ~/Apps/voice && cd ~/Apps/voice
./install.sh
voice doctor
```

Keeps the code in your checkout so edits are live on the next daemon start. Needs
`uv`, PipeWire, `wl-clipboard`, GTK 4 and PyGObject.

📖 **[Full requirements, options and uninstall →](docs/install.md)**

---

## Set up your desktop

**Two steps, once.**

**1. Assign the shortcut.** The desktop owns it, not `config.toml`:

| | Where |
|---|---|
| **GNOME** | Settings → Keyboard → Keyboard Shortcuts → `voice` |
| **KDE Plasma** | Settings window → **Change…** next to *dictate* |

**2. On KDE, let voice see which window you are in** — optional, but it is what makes
pasting into a terminal work. Terminals need Ctrl+Shift+V and everything else needs
Ctrl+V, and voice can only choose correctly if it can ask. Either install `kdotool` (it
is in the AUR), or use KWin's own D-Bus interface, which needs nothing installed —
[both routes, with the check to run first](docs/desktops.md).

GNOME will not tell any app which window has focus, so voice cannot do this there. It
still pastes — it just cannot pick between the two shortcuts, or confirm the paste
landed. So it sends Ctrl+V, keeps your words on the clipboard as well, and the pill says
**Copied** rather than a checkmark it cannot stand behind. Nothing is ever lost.

📖 **[Desktop differences explained →](docs/desktops.md)**

---

## Use it

**Hold the key, speak, release.** The text is pasted where your cursor is.

A small pill shows a live waveform while you talk, then a checkmark when the text lands.

| Command | Does |
|---|---|
| `voice` | Start the daemon, or open settings if it is running |
| `voice status` | State, backend, language, shortcuts |
| `voice recall` | Re-insert the last dictation |
| `voice retry` | Re-transcribe the last recording |
| `voice cancel` | Discard the current recording |
| `voice language sv` | Switch language (`next` cycles) |
| `voice doctor` | Check this machine for everything voice needs |

📖 **[Full usage, the pill, languages, every command →](docs/usage.md)**

---

## Configure it

Settings live in `~/.config/voice/config.toml`, and the settings window edits the same
file. Most people never need to touch it.

```toml
[hotkeys]
dictate_mode = "hold"        # or "toggle"

[stt]
active = "local"             # local, openai, groq, openrouter, ...

[general]
language = "en"
languages = ["en", "sv"]     # the cycle for the language shortcut
```

📖 **[Every setting, with defaults →](docs/configuration.md)**

---

## Something wrong?

Run **`voice doctor`** first — it checks the whole machine and tells you what to fix.

📖 **[Troubleshooting →](docs/troubleshooting.md)**

---

## Documentation

| Page | What's in it |
|---|---|
| [Install](docs/install.md) | Requirements, both install routes, uninstall |
| [Desktop setup](docs/desktops.md) | GNOME vs KDE: shortcuts, window detection, the pill |
| [Usage](docs/usage.md) | First run, the pill, languages, the full CLI |
| [Configuration](docs/configuration.md) | Every setting and default |
| [Troubleshooting](docs/troubleshooting.md) | `voice doctor`, and the common problems |
| [Roadmap](docs/roadmap.md) | What is planned |
| [macOS port assessment](docs/macos-port-assessment.md) | What a Mac build would take, measured |
| [Packaging](packaging/README.md) | How the Arch package is built |

---

## Built on

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) ·
[PipeWire](https://pipewire.org/) ·
[PySide6](https://doc.qt.io/qtforpython-6/) ·
[GTK 4](https://www.gtk.org/) ·
[xdg-desktop-portal](https://github.com/flatpak/xdg-desktop-portal)

## License

To be decided by the owner. No `LICENSE` file yet — treat this as all-rights-reserved
until one is added.
