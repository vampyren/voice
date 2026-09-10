# Portal hotkey backend and recording overlay

Design addendum to `2026-09-10-voice-dictation-design.md`, written 2026-09-10 after the first
live test. Status: approved by the owner 2026-09-10 (visual reference supplied as a screenshot);
all three sections were built in phase 1b, and this document has been brought back into line
with what was built.

## Why

The kernel-device hotkey listener (evdev) cannot see keystrokes in a remote-desktop session,
because they arrive through the remote-desktop server rather than `/dev/input`. The owner's
dev VM is such a session, and KDE users without the udev rule hit the same wall. The desktop
portal's `org.freedesktop.portal.GlobalShortcuts` interface (present on KDE Plasma and GNOME 48+,
verified in the VM: portal 1.21, `Activated`/`Deactivated` signals) sees every keystroke the
compositor sees and needs no device permissions.

The owner also wants Vibe Typer's visible feedback: a floating capsule with a live waveform
while recording.

## 1. Portal hotkey backend

**Config.**
```toml
[hotkeys]
backend = "auto"       # "auto" | "evdev" | "portal"
dictate = "KEY_RIGHTCTRL"          # evdev name (kernel listener)
portal_dictate = "CTRL+space"      # XDG shortcut trigger (portal listener); user can change it in the compositor dialog
portal_language_toggle = ""        # and the same for recall, cancel and the language toggle
```
`auto` chooses `evdev` when at least one keyboard device is readable and the session has a local
seat, otherwise `portal`. `voice doctor` and `voice status` report the active backend.

Both syntaxes accept combinations joined with `+`: `KEY_LEFTMETA+KEY_SPACE` for evdev,
`CTRL+space` or `CTRL+SHIFT+l` for the portal.

**Module.** `voice/hotkey/portal_listener.py` exposes the same interface as `EvdevListener`
(`start/stop/capture_next/held/modifiers_held/devices_ok`) so the daemon swaps them without
other changes. Flow over jeepney on the session bus: `CreateSession` → `BindShortcuts` with one
shortcut id `dictate` (description "Voice dictation", `preferred_trigger` from config) → listen
for `Activated` (press) and `Deactivated` (release) on the session → feed the existing
`Tracker`-shaped events (`("dictate", "press"|"release")`) to the daemon. `recall`, `cancel`
and `language_toggle` are bound as extra shortcuts when configured (empty trigger = not bound).
The compositor shows its shortcut dialog once; the session handle is reused while the daemon runs. Non-sandboxed apps need an app id: the
daemon already runs with `APP_ID` and the installer ships `packaging/voice.desktop`; the
listener sets the `handle_token`/`session_handle_token` options and, where the portal is
≥ 1.21, registers via `org.freedesktop.host.portal.Registry` with that app id.

**Limits.** Compositors reject a bare modifier as a global shortcut, so the portal path needs a
combination (Ctrl+Space by default). `modifiers_held()` returns False for the portal backend
(the compositor has already consumed the chord), so the injector's modifier wait is skipped.
There is no key to capture on this backend - the compositor consumes the chord before anything
else sees it - so the settings window replaces the capture button with text fields for the four
triggers themselves. Saving a changed trigger (or a changed `hotkeys.backend`) rebuilds the
listener on `apply_config`, which creates a new portal session: the desktop may ask for
permission again, and that is the price of applying it without restarting the daemon. Only a
trigger the *running* backend binds forces that rebuild - a portal trigger edited while evdev
is active must not drop the evdev listener's device descriptors - and a listener that cannot be
built (the portal refuses the session) is reported as "Hotkeys are off" rather than left as a
stopped listener, with the next reload free to try again.

**The trigger belongs to the desktop, and `hotkeys.portal_*` is a first-run preference we must
stop re-sending.** Verified live on GNOME Shell 50 (portal `GlobalShortcuts` version 1): the
confirmed trigger is stored in dconf under
`/org/gnome/settings-daemon/global-shortcuts/<app-id>/shortcuts` as
`[('dictate', {'shortcuts': <['F13']>, 'description': <'Voice dictation'>}), ...]`. Calling
`BindShortcuts` again with `preferred_trigger` for that same id does not merely fail to move
it - GNOME answers success and **drops the `shortcuts` member from the stored entry**, leaving
`('dictate', {'description': <'Voice dictation'>})`: the shortcut stays registered with no key
attached, every press does nothing, and both the log line and `devices_ok()` used to call that
"bound". Our own restart was destroying the user's binding.

So `ListShortcuts` (present in version 1; `oa{sv}` in, a `Response` carrying
`shortcuts a(sa{sv})` back) is called before binding and decides per id: an id the portal
already knows is bound with `description` only, and `preferred_trigger` goes out solely for an
id it has never seen. Where `ListShortcuts` cannot be reached - an older backend, an error, a
reply with no `shortcuts` member - **every id counts as known**, because the cost of not
expressing a preference is one trip to Keyboard Settings and the cost of expressing it wrongly
is the user's binding.

**Reporting follows the effective trigger, never the requested one.** Each shortcut's
`trigger_description` is read back from the `BindShortcuts` response (falling back to a second
`ListShortcuts` where a backend answers with a bare vardict) and kept as
`effective_triggers()`. `shortcut_state()` is then one of `bound` / `unassigned` / `denied`,
`devices_ok()` is True only for `bound`, the startup log prints the effective triggers, and
`on_ready` carries the state word rather than a bool. `voice status` renders `unassigned` as
"registered, no key assigned"; `voice doctor`'s **portal shortcuts** line asks the running
daemon and prints the trigger per id (or `no key assigned`), falling back to `dconf read` when
nothing is running (no new dependency, and its absence is not an error). The daemon sends one
notification per run when a shortcut comes back unassigned - once, because a reload rebuilds
the listener and a repeated critical notification for a state the user is already looking at is
noise they cannot switch off.

The user assigns the key in GNOME Settings -> Keyboard -> Keyboard Shortcuts (the app appears
there by name). KDE Plasma implements version 2 of the interface and exposes a reconfigure
dialog instead, which the settings window can ask it to open. Hand-writing GNOME's dconf key
remains possible but is documented as a last resort only: it is GNOME's private storage, and
the write replaces the whole list. README documents the same thing for users.

**Tests.** Unit: a fake bus that emits `Activated`/`Deactivated` messages drives the listener
and the daemon receives press/release; backend selection table for `auto`; the same fake bus
answers `ListShortcuts` and `BindShortcuts` with triggers, covering a known id bound without a
preference, an unknown id bound with one, a failed `ListShortcuts` falling back to the safe
binding, and a bind response with no trigger reaching status, doctor and a single notification.
Boundary (VM): bind a shortcut for real, the owner presses it, the daemon records.

## 2. Recording overlay ("pill")

**What the user sees** (reference: the owner's Vibe Typer screenshot, 2026-09-10). When
recording starts, a dark rounded capsule (~210×48 px, near-black, subtle 1 px lighter border,
fully rounded ends) appears at the bottom centre of the screen. Left/centre: a waveform of
~28 thin vertical bars in violet (#a78bfa-ish, brighter in the middle, dimmer at the ends),
each bar mirrored around the horizontal centre line so the shape reads as a sound wave; bar
heights follow the microphone level at ~30 fps with a short decay, and the envelope tapers
toward both ends. Right: an elapsed-time counter in white, `m:ss`, monospaced digits. On
release the bars freeze and dim and the counter is replaced by "…" pulsing while transcribing;
then a brief checkmark (600 ms), then the capsule hides. On error the counter area shows the
message in amber for 2 s. Nothing is clickable; it never takes focus.

**Process model.** A separate helper process, `voice-overlay` (entry point
`voice.ui.overlay:main`), written with GTK4 through the already-installed PyGObject. The daemon
spawns it on startup and talks to it over the helper's stdin with one JSON line per event:
`{"state": "recording"}`, `{"level": 0.42}`, `{"state": "transcribing"}`, `{"state": "done"}`,
`{"state": "error", "text": "..."}`, `{"state": "hidden"}`, `{"language": "sv"}` and
`{"state": "notice", "text": "EN → SV"}`. A crash or absence of the helper never affects
dictation; the daemon just logs it.

Two ordering rules the daemon keeps: a `{"language": ...}` message precedes every `recording`,
so the pill never appears showing the language of the last dictation; and more generally it
precedes any state message whose language differs from the last one sent, which is how a switch
made from the settings window or the tray reaches a pill that is already on screen. `done` is
sent once per dictation, on the idle that follows a successful insertion - the injection itself
sends nothing.

**Placement, and the focus rule.** With `gtk4-layer-shell` present (Arch `extra`, needed on KDE
Plasma and on wlroots) the window is a layer-shell surface on the overlay layer, anchored bottom,
48 px margin, no keyboard interactivity, so it floats above everything and cannot take the
keyboard. Without it, a plain undecorated GTK window is all that is left - and GTK 4 dropped the
accept-focus and focus-on-map hints, so such a window *is* focused when it maps and the paste
chord would land in the pill. The helper therefore refuses to show one unless
`ui.overlay_allow_fallback` is set, and the daemon runs without a pill instead.

**Layer-shell present but unsupported by the compositor counts as absent.** The library needs
`zwlr_layer_shell_v1`, which GNOME does not implement; installed-but-inert would otherwise map
the focus-taking toplevel while `voice doctor` reported "layer-shell ok". The helper asks
`Gtk4LayerShell.is_supported()` and treats False exactly like a missing typelib, and doctor
says "layer-shell: installed but unsupported by this compositor".

**Audio levels.** `Recorder` gains an optional `on_level(rms: float)` callback computed per
4096-byte chunk on the reader thread (RMS of int16 samples, normalised to 0..1 with a soft
knee). The daemon forwards levels to the helper, throttled to 30/s.

**Config.**
```toml
[ui]
overlay = true
overlay_position = "bottom"        # "bottom" | "top"
overlay_allow_fallback = false     # show a focus-taking window rather than no pill
```
`[ui]` is baked into the helper's command line, so `voice reload` compares the table against
what the running helper was started with and rebuilds the client only when it differs. The
language is not part of that comparison: it travels to the running helper as a message, because
restarting the pill for it would take it off the screen mid-notice.

**Tests.** Unit: level computation; the helper's state machine and drawing model with a fake
clock (bar heights, automatic gain, state transitions, hold timers); the three layer-shell cases
(absent, present-unsupported, present-supported) against a stubbed `gi`; daemon forwards events,
restarts a helper that dies or stops reading exactly once, and never blocks a caller on a spawn.
Visual: screenshots of each state rendered offscreen, inspected. Boundary (VM): overlay appears
during a real dictation.

## 3. Fast language switch

Status: built in phase 1b alongside sections 1 and 2.

**What the user sees.** The pill shows a small language badge ("EN", "SV", "AUTO") next to the
elapsed counter. A `language_toggle` hotkey cycles through `general.languages`; on each change
the pill shows a `notice` for 2 s reading "EN → SV" (no bars). A notice is an overlay on
whatever was on screen: when it expires the pill returns to that state with its timers intact -
a recording keeps counting underneath it and resumes with the time it had left - and it hides
only when the state it interrupted was `hidden`. The tray menu gets a "Language" submenu with
radio entries for the same list. CLI: `voice language <code>` and `voice language next`.

**Config.**
```toml
[general]
language = "en"
languages = ["en", "sv"]        # cycle order for the toggle; "auto" allowed

[hotkeys]
language_toggle = ""            # evdev key name
portal_language_toggle = ""     # portal trigger
```

**Behaviour.** Switching sets `general.language` and saves the config on the Qt thread (same
path as profile switching, including re-reading the file first so the write lands on top of it),
then applies it. `general.language` is read at transcription time rather than when recording
starts, so a switch made mid-recording applies to that recording. The overlay protocol gains
`{"language": "sv"}` and `{"state": "notice", "text": "EN → SV"}`. The pill remains
non-interactive.

**The cycle.** `general.languages` is the toggle's order, and `general.language` need not be in
it (a language chosen by name or from the tray is honoured whatever the list says).

- No list at all - a config written before this feature - means the cycle is the one language
  in force, so a toggle bound in a newer build does nothing rather than jumping somewhere the
  user never chose.
- `next` from a language outside the cycle enters it at the first entry.
- A cycle with nowhere to move to - one entry, already in force - makes the toggle a no-op:
  no save, no reload, no "EN → EN" flashing on the pill. Switching to the language already in
  force is the same no-op, whichever route asked for it.
- `next` is resolved on the Qt thread, where the config is written, not on the caller's: two
  toggles in quick succession are two steps. The IPC reply therefore cannot name the result and
  says `{"ok": true, "language": "pending"}`. `voice language next` reads the current language
  before it asks, then polls `status` (every 50 ms, up to 1.5 s) until the language has left
  that one - a single immediate read races the Qt thread and printed the language just left.
  A toggle is allowed to be a no-op, so the poll is bounded and then prints what it last read.
- The daemon validates the resolved target before writing it: `general.languages` is not
  checked on the way in, and an entry that is not `"auto"` or a two-letter code would otherwise
  be written to `general.language`, where every later load rejects it. Such an entry is logged,
  notified and skipped, and the language stays as it was.

**Tests.** IPC `language` command (valid, invalid, `next` wraps around, `next` resolved on the Qt
thread so a double tap is two steps, a switch to the current language changes nothing); toggle
hotkey cycles and persists (asserted through `Config.load()`, i.e. the file); a single-entry
cycle is a no-op; the overlay model's `notice` returns to the state it interrupted with its
timers intact and hides only from `hidden`; the badge is rendered in the PNG check; the settings
dialog does not revert a language switched while it was open.

## Out of scope for this addendum

Click actions on the pill, dragging it, showing partial transcripts, and the KDE-specific
notification polish already listed in the phase plan.
