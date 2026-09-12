# Desktop setup: GNOME and KDE

[← back to the README](../README.md)

The two desktops differ in ways that decide whether a dictation lands in your
window. This page is the difference.

### 1. The shortcut belongs to the desktop

On the portal backend — which is what you get on GNOME, and on any remote or Wayland
session without readable input devices — the desktop owns the key, not `config.toml`.
`hotkeys.portal_dictate` is a first-run *preference*; once the desktop knows the
shortcut, it decides.

- **GNOME** — **Settings → Keyboard → Keyboard Shortcuts**, find `voice`, set the key.
  GNOME stores it from the very first run, so `hotkeys.portal_*` is never applied there.
- **KDE Plasma** — the settings window's **Change in the desktop** button opens the right
  dialog, or **System Settings → Shortcuts**.

`voice status` and `voice doctor`'s **portal shortcuts** line show what the desktop
actually holds. `registered, no key assigned` means the shortcut exists with no key
attached, so nothing will ever fire it.

### 2. Whether voice can tell which window you are in

This decides which paste shortcut it sends. A terminal pastes with **Ctrl+Shift+V** and
silently discards **Ctrl+V**; everything else is the other way round. To choose, voice
has to ask the desktop what has the keyboard.

**KDE Plasma — it can ask.** Install `kdotool` and voice picks the right shortcut per
window automatically, with no configuration:

```
# Arch / CachyOS
paru -S kdotool          # or yay, or build from the AUR
voice doctor             # the "paste target" line should go green
```

Prefer not to install anything? KWin can answer over its own D-Bus interface, and
`qdbus` already ships with Plasma. **Check it returns without asking you to click a
window** — run this and do not touch the mouse:

```
time timeout 5 qdbus6 org.kde.KWin /KWin org.kde.KWin.queryWindowInfo
```

If it prints the window's properties immediately, put it to work:

```toml
[inject]
active_window_command = "qdbus6 org.kde.KWin /KWin org.kde.KWin.queryWindowInfo | sed -n 's/^resourceClass: //p' | head -n1"
```

If instead your cursor becomes a crosshair and it waits for a click, it is the
interactive window picker — do not use it, and install `kdotool` instead. (This is why
voice does not enable it by itself.)

**GNOME — it cannot ask, and this is not fixable.** GNOME exposes no focused-window API
to ordinary applications: `org.gnome.Shell.Introspect.GetWindows` returns `AccessDenied`
and `org.gnome.Shell.Eval` is disabled. Looking Glass can see it only because it runs
inside the Shell itself.

So on GNOME voice always sends `inject.paste_chord`, and cannot confirm it landed. It
handles that honestly rather than pretending:

- the transcript is **left on the clipboard** — `inject.restore_clipboard` deliberately
  does not apply, so your words are never lost;
- the pill says **Copied** instead of a checkmark that might not be true;
- `voice doctor`'s **paste target** line says so, and the daemon warns once at startup.

If you dictate mostly into a terminal, tell it so and pasting becomes automatic again:

```toml
[inject]
paste_chord = "ctrl+shift+v"
```

Note that Ctrl+Shift+V also pastes in Firefox, Chrome and most Electron apps (as plain
text, which is what you want for dictation), but does nothing in some plain GTK text
fields.

### 3. Where the pill appears

`ui.overlay_position` and the margins are honoured only where the compositor supports
`zwlr_layer_shell_v1` — **KDE and wlroots compositors do; GNOME does not.** On GNOME the
pill is an ordinary window and the compositor places it, so those settings do nothing
there. `voice doctor`'s **pill placement** line tells you which case you are in.

The same limitation is why the pill takes keyboard focus on GNOME, and why voice hides
it before sending the paste shortcut — see [Recording pill](usage.md#recording-pill).

### 4. The "Remote Desktop" permission dialog

The first paste after the daemon starts asks permission to send keystrokes — "Remote
Desktop", with a **Remember This Selection** tick box. Tick it and click **Share**; it
should not ask again.

If it keeps asking every time, the daemon is being started in a way the desktop does not
recognise as the same app each time. Start it from its desktop entry or let the autostart
entry do it, rather than launching `voice daemon` by hand from a shell or a bare systemd
unit — the permission is remembered per application identity, and a hand-launched process
has a different one.
