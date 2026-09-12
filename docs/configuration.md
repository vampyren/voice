# Configuration

[← back to the README](../README.md)

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
timeout_seconds = 300      # give up on a transcription still running after this long;
                           # the recording is kept for "Retry last recording"

[stt.profiles.local]
backend = "local"
model = "large-v3-turbo"   # or "KBLab/kb-whisper-large" for Swedish
device = "cuda"            # falls back to cpu/int8 with a warning
compute_type = "float16"
beam_size = 5
prompt = ""                # steers the style of what is written, e.g. "Notes on a
                           # meeting." Names and jargon belong in [dictionary]
                           # hotwords instead: a prose prompt here pulls ordinary
                           # sentences towards its own wording.

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
# Names and jargon to tell the local model to listen for, as whole words or short
# phrases, e.g. ["CachyOS", "Hollyland Lark"]. The spellings the replacements
# above aim at are added for you; the first 200 characters of the combined list
# are sent with each recording, and anything past that is left out.
hotwords = []

[inject]
mode = "paste"             # "paste" sends the paste chord; "clipboard" only copies and
                           # tells you to press Ctrl+V yourself (remote desktops, and any
                           # compositor that refuses synthetic keystrokes)
paste_chord = "ctrl+v"
terminal_chord = "ctrl+shift+v"
terminal_classes = ["konsole", "org.kde.konsole", "kitty", "alacritty", "foot", "wezterm", "org.gnome.Ptyxis", "gnome-terminal"]
active_window_command = ""   # command printing the focused window class; "" = unknown
# Note: wherever the focused window cannot be read, a paste cannot be confirmed
# to have landed, so the transcript is deliberately LEFT on the clipboard and
# restore_clipboard below does not apply to it - the pill says "Copied", and
# that is only true while the text is still there.
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

**Words the model does not know.** Whisper has never seen *CachyOS*, *OBSBOT* or
*CTranslate2* written down, so it writes the nearest ordinary English it can hear —
"khaki OS", "Ubspot" — and which way it lands changes with how you said the word. List
those names in `[dictionary] hotwords` (the settings window calls it **Words to listen
for**) and the model is told to expect them: on this project's test corpus that took
domain-term recall from 3 of 8 to 7 of 8 with no change at all to ordinary English. The
`replacements` table below it is the other half — it fixes a spelling *after* the fact —
and every spelling in its "Replace with" column is listened for automatically, so you
keep one list, not two. The vocabulary is capped at 200 characters because it shares
Whisper's small prompt window; entries past that are left out. Put names here rather than
in a profile's `prompt`: a prose prompt reaches the same recall but pulls ordinary
sentences towards its own wording, which doubled the errors on plain English.

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

The portal backend needs the desktop entry the package (or `install.sh`) writes
(`/usr/share/applications/io.github.vampyren.voice.desktop`, or the same name under
`~/.local/share/applications/`): the portal resolves the
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
  dialog: **Open your desktop's keyboard settings** (under **Advanced**) asks KDE to open it, and KDE
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

**Hotkeys.** On the evdev backend, use the settings window's **Change…** button — press
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
`general.language_profiles` (see [Model per language](usage.md#model-per-language)) and the
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
[The pill and auto-paste, per desktop](usage.md#the-pill-and-auto-paste-per-desktop) for the two
other things `inject.pill_focus` can do instead.
