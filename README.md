# voice

**Wayland-native voice typing for Linux.** Hold a key, speak, release — your words appear
in whatever window you are working in.

Transcribes locally on your NVIDIA GPU with faster-whisper, or through any
OpenAI-compatible speech-to-text API. Swedish and English out of the box, switchable with
a shortcut.

> **Dictation works today** — speak, and your words are typed. In daily use on KDE
> Plasma and GNOME. Reading text aloud, and tidying up what you dictate, are
> [planned](docs/roadmap.md).

**No graphics card needed.** The GPU only makes transcription faster — on a plain CPU it
still runs about 3× faster than you speak. [Why](docs/install.md#do-i-need-a-gpu)

---

## Install

### Arch / CachyOS — one command, nothing to build

```bash
sudo pacman -U https://github.com/vampyren/voice/releases/download/v0.1.1/voice-0.1.1-1-x86_64.pkg.tar.zst
voice doctor
```

That is the whole install — the app, its locked dependencies, the desktop entry, the
autostart entry and the udev rule. Remove it with `pacman -R voice`.

This is the **CPU build**, which is all most people need: the GPU only makes
transcription faster, and on a plain CPU it still runs about 3× faster than you speak.
[The measurements →](docs/install.md#do-i-need-a-gpu)

### Want CUDA, or want to build it yourself?

```bash
git clone https://github.com/vampyren/voice ~/Apps/voice
cd ~/Apps/voice/packaging
makepkg -si                  # adds ~3 GB of CUDA runtime; VOICE_GPU=0 leaves it out
voice doctor
```

### Any distro — from the checkout

```bash
git clone https://github.com/vampyren/voice ~/Apps/voice && cd ~/Apps/voice
./install.sh
voice doctor
```

Keeps the code in your checkout so edits are live on the next daemon start. Needs
`uv`, PipeWire, `wl-clipboard`, GTK 4 and PyGObject.

📖 **[Full requirements, options and uninstall →](docs/install.md)**

### What it puts on your system

| Where | What |
|---|---|
| `/usr/lib/voice/` | the app and its Python dependencies — nearly all of the 346 MB |
| `/usr/bin/voice` · `/usr/bin/voice-overlay` | the command, and the recording pill's helper |
| `/usr/lib/udev/rules.d/70-voice-input.rules` | one line — see below |
| `/usr/share/applications/…voice.desktop` | the entry in your app launcher |
| `/etc/xdg/autostart/…voice.desktop` | starts the daemon when you log in (`Exec=voice daemon`) |
| `/usr/share/doc/voice/` | this page and everything under `docs/` |

**The udev rule, in full — it is one line:**

```
SUBSYSTEM=="input", KERNEL=="event*", TAG+="uaccess"
```

It tags keyboard event devices with `uaccess`, which tells systemd-logind to grant **the
user currently logged in at the screen** read access to them. That is the whole reason
it exists: voice can see your push-to-talk key without running as root.

It does **not** make anything world-readable, does **not** add you to a group, and does
**not** need a re-login. A remote or SSH session gets nothing from it. On desktops where
voice uses the portal shortcut backend instead — GNOME always does — the rule is not
even used.

**In your home**, created as you use it, never by the installer:

`~/.config/voice/` (settings) · `~/.local/state/voice/` (history) ·
`~/.cache/huggingface/` (the speech model, downloaded on first dictation)

`pacman -R voice` removes everything in the table. Your home files stay — those are
yours to delete.

---

## Set up your desktop

**Two steps, once.**

**1. Assign the shortcut.** The desktop owns it, not `config.toml`:

| | Where |
|---|---|
| **GNOME** | Settings → Keyboard → Keyboard Shortcuts → `voice` |
| **KDE Plasma** | Settings window → **Change…** next to *dictate* |

**2. On KDE — nothing to do.** voice asks KWin directly which window has the keyboard,
so it sends Ctrl+Shift+V to terminals and Ctrl+V everywhere else, by itself. No extra
package. [How that works](docs/desktops.md)

On GNOME there is no equivalent — GNOME will not tell any app which window has focus —
so there voice keeps your words on the clipboard and the pill says **Copied**.

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
