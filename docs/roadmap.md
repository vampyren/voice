# Roadmap

[← back to the README](../README.md)

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
