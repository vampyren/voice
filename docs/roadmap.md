# Roadmap

[← back to the README](../README.md)

## Working today

Dictation, end to end, in daily use:

- Hold a key (or toggle), speak, and the text is pasted where your cursor is.
- Transcription on your own machine with faster-whisper, or through any
  OpenAI-compatible API.
- English and Swedish, with a shortcut to switch between them.
- Recording pill, tray icon, settings window, history and recall, a CLI, and
  `voice doctor`.
- KDE Plasma and GNOME, on Wayland.

## Planned

**Cleaning up what you dictate.** An optional pass that takes the raw transcript and
fixes the punctuation, drops the "ums", or restyles it as an email or a chat message —
with presets, a tray toggle to turn it on and off, and its own settings tab.

**Reading text aloud.** Text-to-speech, the other direction: select some text, press a
key, and hear it. Planned engines are `kokoro`, `chatterbox` and OpenAI's speech API,
with per-voice profiles including a Swedish voice.

**Typing directly instead of pasting.** Today the text goes via the clipboard and a
paste shortcut, which is why this project cares so much about which window has focus.
Once KDE and GNOME ship libei text events, voice can type into the window directly and
that whole problem disappears.

---

*The design documents under `superpowers/specs/` call these "phase 1" through "phase 4".
That is numbering for the specs, not a release schedule.*
