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
voice status           # state, active profile, backend, language, overlay, last error, and
                       # either keyboard access (evdev) or whether the desktop bound the
                       # shortcuts (portal)
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
something fails. It is never clickable, and on a layer-shell surface it never takes
focus. The counter uses JetBrains Mono where that font is installed and the default
monospace font otherwise.

It runs as a separate helper process (`voice.ui.overlay`, GTK 4 through PyGObject), so a
crash there cannot affect dictation - the daemon logs it, restarts it once, and carries
on without it. Switch it off with `ui.overlay = false`, or move it with
`ui.overlay_position` (below). `voice status` shows what it is doing.

**gtk4-layer-shell is required in practice.** GTK 4 removed the "do not focus me" window
hints, so without that library the pill is an ordinary window that takes keyboard focus
when it appears — and the paste would then land in the pill instead of your editor. The
helper therefore refuses to show it: it exits, and the daemon logs

```
overlay disabled: no layer-shell; install gtk4-layer-shell or set ui.overlay_allow_fallback = true
```

once and carries on without a pill. `voice doctor` and `voice status` report the same
thing (`overlay: disabled: no layer-shell`). Set `ui.overlay_allow_fallback = true` if
you want the pill anyway and accept that it takes focus.

#### Where the pill sits

`ui.overlay_position` names one of nine placements - a vertical half,
`top`, `middle` or `bottom`, and a horizontal one, `left`, `center` or `right`:

```
top-left        top-center        top-right
middle-left     middle-center     middle-right
bottom-left     bottom-center     bottom-right
```

`middle` is centred vertically on the screen, so `middle-center` is dead centre.
The two values this setting used to take still work: `"bottom"` means
`"bottom-center"` and `"top"` means `"top-center"`.

`ui.overlay_margin_x` and `ui.overlay_margin_y` push the pill in from the sides
it is anchored to, in pixels - positive moves it inward, negative pushes it out
past the edge, and the range is -2000 to 2000. A half that is `center` or
`middle` is centred by the compositor and has no edge to be a distance from, so
it ignores its margin: with the shipped `bottom-center` only `overlay_margin_y`
(48 by default) does anything.

Changing any of the three takes effect on `voice reload` and on Save in the
settings window - the daemon restarts the pill helper - without restarting the
daemon itself.

**Drag it where you want it.** The settings window's General tab shows this
screen in miniature with the pill in it: drag the pill, and the drop is saved as
the nearest anchor plus the gap it was left with. Dropping it near one of the
nine anchors takes that anchor exactly; the ghosts that appear while dragging
are where those anchors sit. Arrow keys nudge it a pixel at a time and
Shift+arrow ten, and the placement is spelled out in words beside the preview.

**The pill on screen cannot itself be dragged.** It is deliberately
input-transparent - it never takes a click, which is also what keeps it from
stealing focus mid-dictation - and a layer-shell surface has no position to
drag, only anchors and margins. That is why the dragging happens in the
preview, and why what is stored is an anchor and a margin. Where there is no
layer-shell (GNOME, see below) not even that applies: the compositor places the
window, and the helper says so once in the log rather than pretending the
setting was honoured.

#### The pill and auto-paste, per desktop

Whether the pill can float without stealing the keyboard is the compositor's decision,
not ours, and it decides what happens to `inject.mode = "paste"`:

| Desktop | Layer-shell | What you get |
| --- | --- | --- |
| KDE Plasma, wlroots (Sway, Hyprland, river) | yes | the pill floats, never takes focus, and auto-paste works. Nothing below applies. |
| GNOME / Mutter | no — `Gtk4LayerShell.is_supported()` is False even with the library installed | the pill can only be an ordinary window, and it has the keyboard while it is on screen |

On GNOME, then, a pill on screen would swallow the Ctrl+V — the chord goes to the pill,
and with `inject.restore_clipboard = true` the transcript is replaced by the old
clipboard contents 150 ms later, so the text is lost outright. `inject.pill_focus`
decides what happens instead:

- **`"hide"` (the default)** — just before the chord the daemon sends the pill
  `{"state": "hidden"}`, the helper unmaps the window on its next frame, and the injector
  waits `inject.pill_settle_ms` (150 ms) for the compositor to hand focus back to whatever
  had it. Then the chord is sent, and the pill comes straight back with the checkmark. You
  keep both the pill and automatic pasting. Raise `pill_settle_ms` if your compositor is
  slower than that; it is a guess about someone else's window manager, not a fact.
- **`"clipboard"`** — do not paste at all. The text is copied, nothing is restored over
  it, and a notification says so once per daemon run: *"The pill takes focus on this
  desktop, so the text is on the clipboard - press Ctrl+V."* This is the honest fallback if
  hiding does not work on your setup.
- **`"paste"`** — the escape hatch: send the chord anyway, pill and all, for a compositor
  that hands it on regardless.

Which one is in force is visible in both `voice status` and `voice doctor`:

```
insertion: paste (the pill takes focus here, so it is hidden for the chord)
insertion: clipboard - press Ctrl+V (the pill takes focus on this desktop)
```

None of this happens when the pill is switched off (`ui.overlay = false`), when
`ui.overlay_allow_fallback` is false (there is no pill to be in the way), or when
`inject.mode` is already `"clipboard"` — there is no chord to protect.

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

#### Model per language

One model rarely wins in two languages, so `general.language_profiles` names the
transcription profile each language selects:

```toml
[general]
language = "en"
languages = ["en", "sv"]

[general.language_profiles]
en = "local"
sv = "local-swedish"
```

Both entries are commented out in a shipped config, so nothing changes until you opt in.
Add the `local-swedish` profile first — settings window, Transcription tab, **Add from
template** — then uncomment the mapping, or fill in the **Profile per language** table on
the General tab, which writes it for you.

On the CachyOS box that pairs `local` = `large-v3-turbo` (English) with `local-swedish` =
`KBLab/kb-whisper-large` (Swedish), both `device = "cuda"`. KB-Whisper also publishes
`KBLab/kb-whisper-small`, `-base` and `-medium` if the large model is too slow on your
machine.

Every path switches the pair together — the toggle hotkey, the tray, `voice language sv`,
and Save in the settings window — in a single write, so the model is loaded once. A
profile the map chose is shown as `profile: local-swedish (for sv)` by `voice status` and
in the tray tooltip, and `voice doctor` prints the whole map. `voice profile <name>` still
switches the model on its own and leaves the language alone. If the mapped profile is
missing, voice says so once and keeps the model it has.

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
```

**Hotkey backend.** `hotkeys.backend` decides how the hotkey is seen:

- `evdev` reads the kernel input devices directly. It sees any key, including a bare
  modifier such as `KEY_RIGHTCTRL`, but needs read access to `/dev/input` (the udev rule
  or the `input` group) and a local seat.
- `portal` asks the desktop to bind a global shortcut through
  `org.freedesktop.portal.GlobalShortcuts` (KDE Plasma, GNOME 48+). No device
  permissions, and it works in a remote-desktop session, because the compositor sees the
  keystroke before anything else does. **The key itself is the desktop's**, not
  `config.toml`'s: `hotkeys.portal_dictate` (**Ctrl+Space** by default) is at most a
  first-run preference, and on GNOME it is never applied at all — see
  [The desktop owns the trigger](#the-desktop-owns-the-trigger) below, which is where you
  set the key. Compositors also refuse a bare modifier as a global shortcut, so where a
  preference *is* used it must be a combination — a lone `CTRL` will not bind.
- `auto` (the default) picks `evdev` when at least one keyboard is readable *and* the
  session has a local seat, and `portal` otherwise. `voice doctor` prints the choice and
  the reason (`hotkey backend: portal (no local seat)`), and so does `voice status`.

The portal backend needs the desktop entry `install.sh` writes
(`~/.local/share/applications/io.github.vampyren.voice.desktop`): the portal resolves the
app id through it, and refuses the shortcut session with "An app id is required" without
it. Changing `hotkeys.backend` applies on `voice reload` (and on Save in the settings
window): the daemon closes the portal session and creates a new one, so the desktop may
ask for permission again.

### The desktop owns the trigger

Once your desktop knows a shortcut id, **the key attached to it is the desktop's, not
`config.toml`'s**, and `voice` deliberately stops asking for one. Re-requesting a trigger
for a shortcut the desktop already knows is worse than useless: on GNOME 50 it makes the
stored entry lose its key entirely, so the shortcut stays listed with nothing bound to it
and every press does nothing. That is what used to happen on **every daemon start**, which
is why a key you had set could quietly stop working.

`voice` only expresses a preference when the desktop can tell it the shortcut is genuinely
new. **On GNOME it never can.** The portal's `ListShortcuts` is scoped to the session
`voice` has just created, and that session is necessarily new when we ask, so it reports
nothing whether or not GNOME has held a key for months. Measured on GNOME 50 with three
shortcuts assigned for this app id, the whole answer is:

```
ListShortcuts -> 0
raw results: {'shortcuts': ('a(sa{sv})', [])}
```

An empty listing is therefore no evidence of anything, and reading it as "never seen" is
exactly how the key got destroyed. The rule that follows: **an answer that cannot separate
"never seen" from "seen and assigned" counts as "seen", and no preference goes out** — so
on GNOME `hotkeys.portal_*` (and the Hotkeys tab's trigger fields) is never applied, and
the key is always yours to set below. Editing it changes nothing, no matter how often you
reload or restart.

To set the key, or change one:

- **From the settings window, on GNOME** — type the trigger in the Hotkeys tab
  (`CTRL+space`, `F13`, `CTRL+SHIFT+l`) and press **Save**. `voice` writes it into GNOME's
  own store — the `global-shortcuts` dconf key below, which its Settings app does not
  surface usefully — and rebinds the listener, so the new key works at once without a
  restart. Only the shortcut ids `voice` owns are touched, an entry that cannot be parsed
  is refused rather than overwritten, and the result (or the refusal) is shown in the tab.
  On desktops that do not keep shortcuts there — KDE — nothing is written and the two
  routes below apply instead.
- **GNOME** — open **Settings → Keyboard → Keyboard Shortcuts**, where `voice` appears
  under its own name, and set the key there. It takes effect immediately; nothing needs
  restarting, and `voice status` follows the change as the desktop makes it. The desktop's
  own dialog on the first bind is the other place to set it. The settings window's **Open
  shortcut settings** button opens this panel for you (`gnome-control-center keyboard`).
- **KDE Plasma** — implements version 2 of the portal interface, which has a reconfigure
  dialog: **Open shortcut settings** (and "Capture key") asks KDE to open it, and KDE
  System Settings → Shortcuts lists the binding as well. Because that version *can* answer
  ListShortcuts usefully, `hotkeys.portal_*` is honoured there on a genuine first run.

`voice doctor`'s **portal shortcuts** line shows the effective trigger per shortcut id, or
`no key assigned` where the desktop registered a shortcut without one; `voice status` says
the same in one line, and the settings window shows it beside each trigger field. All three
follow the desktop as it changes: the portal announces a rebinding and `voice` takes it,
and a reload or opening the settings window asks outright in case the announcement was
missed. If a shortcut comes back with no key, `voice` also tells you once, with a
notification pointing here — and again if you lose the key a second time.

<details>
<summary>Last resort: writing GNOME's dconf key by hand</summary>

Only if Settings will not show or set the shortcut. This is GNOME's private storage, not a
`voice` interface, and a wrong write silently unbinds every shortcut in the list:

```
dconf read /org/gnome/settings-daemon/global-shortcuts/io.github.vampyren.voice/shortcuts
dconf write /org/gnome/settings-daemon/global-shortcuts/io.github.vampyren.voice/shortcuts \
  "[('dictate', {'description': <'Voice dictation'>, 'shortcuts': <['F14']>})]"
```

Triggers use GTK accelerator syntax (`F14`, `<Shift><Control>l`). The write **replaces the
whole list**, so read it back first and keep every id you still want, one entry each
(`dictate`, `recall`, `cancel`, `language_toggle`). Restart `voice` afterwards if the new
trigger does not answer.

</details>

**Hotkeys.** On the evdev backend, use the settings window's "Capture key" button — press
the physical key and it fills in the exact evdev name it received. Combinations are typed
by hand, e.g. `KEY_LEFTMETA+KEY_SPACE`. If a Keychron spare key (the circle/triangle/square
keys) sends nothing, remap it in Keychron Launcher to F13 and bind `KEY_F13` here.

On the portal backend there is no key to capture — the compositor consumes the chord
before anything else sees it — so the Hotkeys tab shows the four triggers themselves
(`portal_dictate`, `portal_recall`, `portal_cancel`, `portal_language_toggle`) as text
fields in the desktop's own syntax: `F14`, `CTRL+space`, `CTRL+SHIFT+l`. They are what
`voice` asks for the first time the desktop meets each shortcut — see
[The desktop owns the trigger](#the-desktop-owns-the-trigger) — and, on GNOME, what Save
writes into the desktop's own store so the change takes effect straight away.

Beside each field is the key the desktop **actually** holds for that shortcut, re-read
every time the window opens: the trigger itself, `no key assigned` for a shortcut the
desktop registered without one, `not registered` for one `voice` never asked it to bind
(an empty `portal_*` value), or `waiting for the desktop` before the portal has answered.
That is the line to read — the field above it is only ever a request. **Open shortcut
settings** takes you to where the key really lives: KDE's reconfigure dialog on portal
version 2, otherwise `gnome-control-center keyboard` or `systemsettings kcm_keys`,
whichever is installed, and failing both it prints the path to click yourself.

**Profiles.** `stt.active` picks one of the `[stt.profiles.*]` tables. Add a cloud
profile by pasting an API key: either `api_key = "sk-..."` inline, or set the
environment variable named by `api_key_env` (`OPENAI_API_KEY`, `GROQ_API_KEY`,
`OPENROUTER_API_KEY`) and keep the config file free of secrets. Switch with
`voice profile openai` or from the tray/settings. For Swedish, use the `local-swedish`
template in the settings window's profile picker (or set a profile's `model` to
`KBLab/kb-whisper-large` by hand) and `general.language = "sv"`. Map the two together with
`general.language_profiles` (see [Model per language](#model-per-language)) and the
language switch carries the model with it.

**Paste behaviour.** Text is copied to the clipboard then pasted with Ctrl+V through the
desktop portal (`org.freedesktop.portal.RemoteDesktop`); KDE asks permission once and
remembers it. Windows whose class is in `inject.terminal_classes` get Ctrl+Shift+V
instead, since most terminals reserve Ctrl+V. `inject.active_window_command` should be a
command that prints the focused window's class to stdout; leave it empty if you don't
have one (the default chord is used, unknown window). The previous clipboard contents
are restored after the paste.

On a desktop where the recording pill cannot refuse keyboard focus (GNOME), the pill is
taken off screen for the chord and brought straight back — see
[The pill and auto-paste, per desktop](#the-pill-and-auto-paste-per-desktop) for the two
other things `inject.pill_focus` can do instead.

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
- **portal shortcuts** *(optional)* — on the portal backend, the trigger the desktop
  actually holds for each shortcut id, read from the running daemon. `no key assigned`
  means the shortcut is registered and no key is attached to it, so nothing will ever fire
  it — fix that in your desktop's keyboard settings, not in `config.toml`. With no daemon
  running it falls back to GNOME's stored copy. See
  [The desktop owns the trigger](#the-desktop-owns-the-trigger).
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
- **The portal shortcut does nothing, and `hotkeys.portal_dictate` changes nothing.** Both
  have the same cause: the desktop owns the key once it knows the shortcut, and it may be
  holding the shortcut with no key attached at all. Check `voice status` — `registered, no
  key assigned` is that state — or `voice doctor`'s **portal shortcuts** line, then set the
  key in **Settings → Keyboard → Keyboard Shortcuts**. See
  [The desktop owns the trigger](#the-desktop-owns-the-trigger).
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
  binds its shortcuts through the desktop instead of reading `/dev/input`, so no `input`
  group membership is needed either (the first run asks for Ctrl+Space,
  `hotkeys.portal_dictate`; after that the key lives in your desktop's keyboard settings). Run `./install.sh` first
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
