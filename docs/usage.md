# Using voice

[← back to the README](../README.md)

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
voice cancel           # discard the current recording, or abandon a transcription
                       # that is taking too long
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
("EN", "SV", "AUTO"). It turns into a progress line while transcribing, and shows the
error in amber for two seconds when something fails. It is never clickable, and on a
layer-shell surface it never takes focus. The counter uses JetBrains Mono where that font
is installed and the default monospace font otherwise.

**What it says when a dictation ends:**

- a bare **checkmark** — the text was inserted, and voice could confirm which window it
  was aiming at. It holds for 1.2 s.
- **Copied** — the text is on the clipboard and voice could *not* confirm the paste
  landed, because nothing on this desktop will name the focused window. Your words are
  never lost; paste them with whatever your app uses. It holds for 2.4 s, because there
  is something to read. See
  [Desktop setup](desktops.md#2-whether-voice-can-tell-which-window-you-are-in).
- **Copied · Ctrl+V** — voice deliberately did not paste at all: `inject.mode =
  "clipboard"`, or a paste that failed outright.

**What it says when you press the key and it cannot act:** pressing dictate while a
dictation is still transcribing or pasting used to do nothing at all, silently — so it
was natural to press again, and that second press would land after the pipeline had
finished and start an unwanted recording. The pill now answers **Still working** or
**Still pasting** for two seconds instead. Nothing appears during the moment the pill is
hidden for the paste shortcut, because putting it back on screen there would take the
keyboard and the paste would land in the pill.

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

**And it shows you.** A moment after you drop the pill (or stop nudging it), the
daemon puts the *real* pill on your desktop at that placement for five seconds,
with a waveform and the language badge, then takes it away. It is only a
picture: nothing is recorded, no history entry is written, and starting a real
dictation ends it immediately. It needs a desktop that can place the pill at
all — see the note under the placer — and the daemon refuses it, saying why,
while a dictation is in flight or when `ui.overlay = false`.

**The pill on screen cannot itself be dragged.** It is deliberately
input-transparent - it never takes a click, which is also what keeps it from
stealing focus mid-dictation - and a layer-shell surface has no position to
drag, only anchors and margins. That is why the dragging happens in the
preview, and why what is stored is an anchor and a margin. Where there is no
layer-shell (GNOME, see below) not even that applies: the compositor places the
window. The settings window says so under the placer — *"Your desktop places
this window itself, so this only takes effect on KDE/wlroots"* — and `voice
doctor`'s **pill placement** line says the same, so the setting is never
silently ignored. The control stays live either way: the placement is recorded
and applies on a machine that does have a layer shell.

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
