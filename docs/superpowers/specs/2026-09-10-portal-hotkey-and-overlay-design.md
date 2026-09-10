# Portal hotkey backend and recording overlay

Design addendum to `2026-09-10-voice-dictation-design.md`, written 2026-09-10 after the first
live test. Status: awaiting owner review (visual mockup pending via /design).

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
```
`auto` chooses `evdev` when at least one keyboard device is readable and the session has a local
seat, otherwise `portal`. `voice doctor` and `voice status` report the active backend.

**Module.** `voice/hotkey/portal_listener.py` exposes the same interface as `EvdevListener`
(`start/stop/capture_next/held/modifiers_held/devices_ok`) so the daemon swaps them without
other changes. Flow over jeepney on the session bus: `CreateSession` → `BindShortcuts` with one
shortcut id `dictate` (description "Voice dictation", `preferred_trigger` from config) → listen
for `Activated` (press) and `Deactivated` (release) on the session → feed the existing
`Tracker`-shaped events (`("dictate", "press"|"release")`) to the daemon. `recall` and `cancel`
are bound as extra shortcuts when configured. The compositor shows its shortcut dialog once;
the session handle is reused while the daemon runs. Non-sandboxed apps need an app id: the
daemon already runs with `APP_ID` and the installer ships `packaging/voice.desktop`; the
listener sets the `handle_token`/`session_handle_token` options and, where the portal is
≥ 1.21, registers via `org.freedesktop.host.portal.Registry` with that app id.

**Limits.** Compositors reject a bare modifier as a global shortcut, so the portal path needs a
combination (Ctrl+Space by default). `modifiers_held()` returns False for the portal backend
(the compositor has already consumed the chord), so the injector's modifier wait is skipped.
Capture-key in settings shows the compositor dialog instead of reading a raw key.

**Tests.** Unit: a fake bus that emits `Activated`/`Deactivated` messages drives the listener
and the daemon receives press/release; backend selection table for `auto`. Boundary (VM):
bind a shortcut for real, the owner presses it, the daemon records.

## 2. Recording overlay ("pill")

**What the user sees.** When recording starts, a dark rounded capsule appears at the bottom
centre of the screen: a red dot, the word "Listening", and 24 vertical bars whose heights
follow the microphone level at ~30 fps. On release it switches to "Transcribing…" with a
subtle pulse, then to a checkmark for 600 ms, then hides. On error it shows the message in
amber for 2 s. Nothing is clickable; it never takes focus.

**Process model.** A separate helper process, `voice-overlay` (entry point
`voice.ui.overlay:main`), written with GTK4 through the already-installed PyGObject. The daemon
spawns it on startup and talks to it over the helper's stdin with one JSON line per event:
`{"state": "recording"}`, `{"level": 0.42}`, `{"state": "transcribing"}`, `{"state": "done"}`,
`{"state": "error", "text": "..."}`, `{"state": "hidden"}`. A crash or absence of the helper
never affects dictation; the daemon just logs it.

**Placement.** With `gtk4-layer-shell` present (Arch `extra`, needed on KDE Plasma and on
wlroots) the window is a layer-shell surface on the overlay layer, anchored bottom, 48 px
margin, no keyboard interactivity, so it floats above everything. Without it (GNOME, or the
package missing) it falls back to a plain undecorated GTK window that the compositor places;
functional, not pinned.

**Audio levels.** `Recorder` gains an optional `on_level(rms: float)` callback computed per
4096-byte chunk on the reader thread (RMS of int16 samples, normalised to 0..1 with a soft
knee). The daemon forwards levels to the helper, throttled to 30/s.

**Config.**
```toml
[ui]
overlay = true
overlay_position = "bottom"   # "bottom" | "top"
```

**Tests.** Unit: level computation; the helper's state machine and drawing model with a fake
clock (bar heights decay, state transitions, hide timer); daemon forwards events and survives
a dead helper. Visual: screenshots of each state rendered offscreen, inspected. Boundary
(VM): overlay appears during a real dictation.

## Out of scope for this addendum

Click actions on the pill, dragging it, showing partial transcripts, and the KDE-specific
tray/notification polish already listed in the phase plan.
