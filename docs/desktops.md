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
- **KDE Plasma** — press **Change…** beside *dictate* in the settings window and follow
  the prompt, or open **System Settings → Shortcuts** yourself. The settings window's
  **Advanced** section has an **Open your desktop's keyboard settings** button that takes
  you straight there.

`voice status` and `voice doctor`'s **portal shortcuts** line show what the desktop
actually holds. `registered, no key assigned` means the shortcut exists with no key
attached, so nothing will ever fire it.

### 2. Whether voice can tell which window you are in

This decides which paste shortcut it sends. A terminal pastes with **Ctrl+Shift+V** and
silently discards **Ctrl+V**; everything else is the other way round. To choose, voice
has to ask the desktop what has the keyboard.

**KDE Plasma — it can ask, and nothing needs installing.** KWin will not answer a plain
D-Bus question, but it *will* run a script for you, in-process, with its full scripting
API. voice writes a five-line script, asks KWin to run it, and the script calls back
over D-Bus with the answer:

```js
var w = workspace.activeWindow;
callDBus(<voice's bus name>, "/", ..., w ? String(w.resourceClass) : "");
```

`resourceClass` is exactly the form `inject.terminal_classes` is written in — Konsole
reports `org.kde.konsole`. The whole exchange is D-Bus from the daemon's own connection:
no `qdbus`, no shell, no subprocess between focus returning and the chord going out.

This is the same technique `kdotool` uses, which is why `kdotool` is no longer needed or
suggested — voice does it itself. `voice doctor` reports **focused window read from KWin
directly (no command needed)**.

### Why not `org.kde.KWin.queryWindowInfo`?

It looks like the obvious answer — it reports `resourceClass`, and `qdbus` ships with
Plasma. **It is an interactive window picker.** KWin waits for you to click a window;
your cursor becomes a crosshair. Measured on Plasma 6:

```
$ time timeout 5 qdbus6 org.kde.KWin /KWin org.kde.KWin.queryWindowInfo
(no output)                       Executed in 5.01 secs     ← timed out, untouched
$ # and again, this time clicking the terminal
resourceClass: org.kde.konsole    Executed in 3.26 secs     ← the click
```

Run before every paste it would put a grab in front of you every time. voice does not use
it, and neither should you.

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
