# Troubleshooting

[← back to the README](../README.md)

## Troubleshooting

`voice doctor` runs a checklist and prints `✔`/`✘` per item; failures marked
`(optional)` don't affect the exit code:

- **python** *(optional)* — the interpreter and `voice` version this is running as.
- **config** — `config.toml` parses and passes validation.
- **keyboard access** — at least one input device is readable without root. Re-run
  `./install.sh` or install the package — both install the udev rule — or add yourself to
  the `input` group and log out/in. A device already open before the rule existed may
  need re-plugging.
- **hotkey backend** *(optional)* — informational: which listener the daemon would use here and why
  (`evdev`, or `portal (no local seat)`). Never fails the run; see
  [Configuration](configuration.md#configuration).
- **portal shortcuts** *(optional)* — on the portal backend, the trigger the desktop
  actually holds for each shortcut id, read from the running daemon. `no key assigned`
  means the shortcut is registered and no key is attached to it, so nothing will ever fire
  it — fix that in your desktop's keyboard settings, not in `config.toml`. With no daemon
  running it falls back to GNOME's stored copy. See
  [The desktop owns the trigger](configuration.md#the-desktop-owns-the-trigger).
- **overlay** *(optional)* — informational: whether the recording pill can run here, which
  interpreter starts its helper, and whether `gtk4-layer-shell` was found. Never fails
  the run; see [Recording pill](usage.md#recording-pill).
- **pw-record** — PipeWire's recording tool is on PATH.
- **wl-clipboard** — `wl-copy`/`wl-paste` are installed.
- **portal** — the `RemoteDesktop` portal is reachable (`xdg-desktop-portal-kde` on KDE,
  `xdg-desktop-portal-gnome` on GNOME). If the permission dialog needs revoking or
  doesn't reappear, check KDE System Settings → Applications → Remote Desktop.
- **cuda** *(optional)* — an NVIDIA GPU is visible to `ctranslate2`. The package ships the CUDA 12
  runtime, so on a machine with a card this should be green with no extra install; if it
  is not, the driver (`nvidia-utils`) is the thing to check. Without a GPU — or in a
  `VOICE_GPU=0` build — the local backend runs on CPU int8 automatically, slower but
  working, and this line explains why nothing is using the GPU.
- **paste target** *(optional)* — whether the paste can reach the window you are in. It
  fails when nothing on this desktop will name the focused window *and* a separate
  terminal chord is configured, because then every dictation is sent with the
  non-terminal shortcut and a terminal will discard it. The message names the cause and
  the fix. It also fails if a configured `inject.active_window_command` has stopped
  answering.
- **language profiles** *(optional)* — which transcription profile each language in
  `general.languages` maps to, so a language switch that would silently keep the wrong
  model is visible.
- **pill placement** *(optional)* — whether `ui.overlay_position` can be honoured on this
  desktop at all. KDE and wlroots compositors honour it; GNOME places the pill itself, so
  the setting does nothing there.
- **microphones** *(optional)* — at least one PipeWire source is visible.
- **model cache** *(optional)* — whether the active local model has already downloaded.
- **notify-send**, **fallback senders** *(optional)* — desktop notifications, and
  `wtype`/`ydotool` as a paste fallback if the portal chord path is ever unavailable.

Some common problems doctor doesn't cover directly:

- **Paste doesn't work in Konsole (or another terminal).** Terminals bind Ctrl+V to
  something else, so voice has to know it is aiming at one. That needs two things: the
  window class in `inject.terminal_classes` (already includes `konsole`,
  `org.kde.konsole`, `kitty`, `alacritty`, `foot`, `wezterm`, `org.gnome.Ptyxis`,
  `gnome-terminal`), **and** a way to read the focused window at all. On KDE that means
  `kdotool` or an `inject.active_window_command`; **on GNOME it is impossible**, so the
  text is left on the clipboard and the pill says **Copied**. See
  [Desktop setup](desktops.md#2-whether-voice-can-tell-which-window-you-are-in).
- **The portal shortcut does nothing, and `hotkeys.portal_dictate` changes nothing.** Both
  have the same cause: the desktop owns the key once it knows the shortcut, and it may be
  holding the shortcut with no key attached at all. Check `voice status` — `registered, no
  key assigned` is that state — or `voice doctor`'s **portal shortcuts** line, then set the
  key in **Settings → Keyboard → Keyboard Shortcuts**. See
  [The desktop owns the trigger](configuration.md#the-desktop-owns-the-trigger).
- **A dictation never finishes: the tray stays amber and `voice status` says
  `transcribing`.** A transcription is bounded by `stt.timeout_seconds` (5 minutes by
  default): past that the attempt is abandoned, a notification says so, the state goes
  back to idle and the recording is kept — `voice retry` runs it again, on another
  profile if the local model is the problem. You don't have to wait for the bound:
  `voice cancel` (or the cancel shortcut, or the tray's **Cancel**) abandons it at once,
  and the recording is still kept for a retry. The abandoned conversion keeps running on
  its own thread until it finishes — a local model cannot be interrupted safely — but
  nothing waits for it and its result is discarded.
- **"No microphone found".** PipeWire has no capture device, so there is nothing to
  record and the dictation is refused before it starts. `pw-record` does not report this
  itself: with no source to link to it produces no audio at all, and where it can fall
  back to a monitor it records silence — which is how a dictation with the microphone
  unplugged used to end up transcribing half a minute of nothing. `voice doctor`'s
  **microphones** line lists what PipeWire can see.
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
  `hotkeys.portal_dictate`; after that the key lives in your desktop's keyboard settings). Install the package or run
  `./install.sh` first — the portal needs the desktop entry they install to resolve this
  app's id. What remains
  is the paste: the portal keystroke is not delivered to the focused window in some remote
  sessions, so if Ctrl+V never arrives, set `inject.restore_clipboard = false` and paste
  yourself, or drive dictation from the command line (`voice toggle`, or `voice start`
  with a short `audio.max_seconds`).
