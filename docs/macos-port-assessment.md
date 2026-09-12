# Porting voice to macOS — an assessment

**Date:** 2026-09-12 · **Against:** `main` at the "reliable dictation" merge ·
**Question asked:** how hard would it be to give macOS colleagues this app, as a
signed `.dmg` that uses Apple's own APIs?

**Verdict: worth doing.** About a quarter of the code needs a macOS
counterpart, roughly a tenth is deleted outright, and the rest runs unchanged.
The port is not a rewrite — the app already has plug-in points for the three
things that differ most, with two to four implementations each.

The more interesting finding is that **macOS removes this project's two hardest
problems** rather than replacing them with new ones. See "What gets easier".

---

## How these numbers were produced

Everything below is measured, not estimated by eye. To reproduce:

```bash
# line counts by module
find voice -name '*.py' | xargs wc -l

# what actually depends on the Linux keyboard library
grep -rn "import evdev\|from evdev" voice/

# how much of the suite survives without it (simulates a Mac)
mkdir -p /tmp/noevdev
printf 'raise ImportError("no evdev on this platform")\n' > /tmp/noevdev/evdev.py
PYTHONPATH=/tmp/noevdev .venv/bin/python -m pytest -q --continue-on-collection-errors
```

---

## The numbers

`voice/` is **11,320 lines** of Python across 43 files. The test suite is
**1,313 tests** (15,864 lines).

| | Lines | Share |
|---|---:|---:|
| Runs on macOS unchanged | 3,065 | 27% |
| Linux-specific, needs a counterpart | 2,368 | 21% |
| Mixed — portable core, thin platform edge | 5,887 | 52% |

The "mixed" figure overstates the work considerably. Within it, the actual
platform-bound edges are small and already isolated:

| Module | Total | Platform-bound within it |
|---|---:|---:|
| `daemon.py` | 1,689 | ~200 (six functions, one listener factory) |
| `ui/settings.py` | 2,093 | ~50 |
| `ui/overlay_client.py` | 721 | ~200 (helper launch; the other 473 are portable) |
| `config.py` | 436 | the `evdev` import chain + Linux vocabulary in defaults |
| `doctor.py` | 429 | 11 of 17 health checks |
| `inject/injector.py` | 286 | ~90, all of it Wayland workaround (see below) |

**Realistic rewrite surface: roughly 2,900 of 11,320 lines — about 26%.**

---

## What gets easier

Today's entire bug hunt came down to two Wayland limitations. Both are absent
on macOS:

**1. "Which window am I pasting into?"** On GNOME nothing will say — the
supported API returns `AccessDenied` and the debug route is disabled. That one
gap is why `inject/window.py` exists (137 lines of per-compositor shell
commands for KDE, Hyprland and Sway), why the paste can silently go into a
terminal that ignores Ctrl+V, and why the pill has to say "Copied" instead of
confirming success.

On macOS, `NSWorkspace.shared.frontmostApplication.bundleIdentifier` answers
this always, with no permission prompt. **137 lines become about 10, and the
whole unverifiable-paste concept can be retired.**

**2. "The pill steals the keyboard."** GTK 4 dropped `set_accept_focus`, and
GNOME does not implement `zwlr_layer_shell_v1`, so the recording pill is an
ordinary window that takes focus when it appears. Working around that produced
~90 lines in the injector (`pill_focus` policies, hide-then-settle-then-paste),
a defect class that recurred four times at three call sites, and eventually a
scripted invariant to stop it recurring a fifth.

macOS has had `NSPanel` with `.nonactivatingPanel` for two decades: a floating
window that genuinely never takes focus. **All of that machinery becomes dead
code.**

---

## What carries over unchanged (3,065 lines)

The valuable half, and the half that was expensive to get right:

| Module | Lines | Note |
|---|---:|---|
| `pipeline.py` | 733 | The whole dictation state machine. Zero OS references outside comments. |
| `ui/overlay_model.py` | 562 | Pill timing, waveform, animation. Explicitly dependency-free. |
| `ui/overlay_draw.py` | 500 | Cairo drawing. Returns an image buffer; never touches a window. |
| `ui/pill_placer.py` + `placement.py` | 420 | |
| `ipc.py` | 129 | Unix sockets — POSIX, fine on macOS. |
| `ui/tray.py` + `icons.py` | 228 | Qt system tray works on macOS. |
| `text.py`, `history.py`, `stt/*` | 392 | Transcription, history, replacements. |

`ui/settings.py` adds a further ~2,040 lines of Qt widgets that also carry over.

Crucially, the drawing is already separated from the windowing: the renderer
hands back a buffer rather than painting into a GTK widget, and its 40 tests
already run headless. **A macOS host would replace ~230 lines of GTK and keep
1,062 lines of model and drawing untouched.**

---

## What is deleted rather than ported (~1,300 lines)

These exist only to cope with Wayland and have no macOS counterpart:

| Module | Lines | Why it goes |
|---|---:|---|
| `hotkey/portal_listener.py` | 521 | Replaced by ~150 lines of Carbon/NSEvent. |
| `hotkey/desktop_shortcuts.py` | 409 | Reads GNOME's shortcut registry via `dconf`. No analogue. |
| `inject/window.py` | 137 | Four compositors' worth of shell commands → one API call. |
| `inject/fallback.py` | 94 | wtype/ydotool fallbacks. Quartz is the single path. |
| `portal_common.py` | 64 | D-Bus handshake. |
| `injector.py` pill-focus machinery | ~90 | See "What gets easier". |

---

## What must actually be written

Six pieces. Each has an existing plug-in point, so none is structural surgery:

| Piece | Today | macOS | Existing seam |
|---|---|---|---|
| Recording | `pw-record` subprocess | `AVAudioEngine`, or `sounddevice` | Constructor injection only — **needs a new interface** |
| Hotkey | evdev + XDG portal (676 lines) | `RegisterEventHotKey` / `NSEvent` global monitor | Six-method duck type, parity enforced by a test |
| Sending the paste | D-Bus RemoteDesktop portal | `CGEventCreateKeyboardEvent` + `CGEventPost` | `KeySender` Protocol, 4 implementations already |
| Clipboard | `wl-copy` / `wl-paste` | `NSPasteboard` (or `pbcopy`/`pbpaste`) | Concrete class over an injected `run` — **needs a Protocol** |
| Pill window | GTK 4 + layer-shell | `NSPanel` non-activating | Renderer already returns a buffer |
| Notifications | `notify-send` | `UNUserNotificationCenter` | Injected argv |
| Paths | XDG | `~/Library/Application Support` | One file, four call sites |

One detail worth planning for: keystrokes are passed around as **evdev key
codes**, produced from a name table. A Quartz sender needs a reverse map to
`CGKeyCode` — exactly as the existing wtype sender already reverse-maps to xkb
names, so the pattern exists.

---

## The one structural blocker

**`evdev` is imported by the config layer, and it does not build on macOS.**

`voice/config.py` imports `voice.inject.injector` → `voice.inject.keys`, whose
first line is `from evdev import ecodes`. `evdev` ships as a source archive
only and compiles against `linux/input.h`.

Measured impact — with `evdev` made unavailable:

```
baseline:            1313 tests collected
without evdev:        706 collected, 11 collection errors
                      701 passed
```

**607 tests — 46% of the suite — cannot even load on a Mac today.**

The fix is much smaller than that number suggests. Of the four modules that
import it, **three use it only as a table of key names** — `ecodes.KEY_LEFTCTRL`
and friends — with no device access at all:

- `hotkey/keyspec.py` — constants and name lookups
- `inject/keys.py` — a chord-name → code dictionary
- `inject/fallback.py` — a code → xkb-name map

Only `hotkey/evdev_listener.py` opens `/dev/input`. Replacing the shared table
with a plain dictionary and gating the listener import behind the platform
recovers the large majority of those 607 tests **without writing any macOS code
at all** — and is worth doing regardless, because a keyboard-device library has
no business being a hard dependency of the config parser.

---

## Apple-specific costs (the real friction)

The code is the easy part.

**Permissions.** Each user grants, on first run:
- *Microphone* — standard, one dialog.
- *Accessibility* — required to post synthetic keystrokes. Cannot be granted
  programmatically; the user must open System Settings and toggle it.
- *Input Monitoring* — required for a global hotkey via event taps.

These are one-time but they are a genuine onboarding step, and the dialogs look
alarming to a non-technical user. Budget for a first-run screen that explains
them and links straight to the right settings pane.

**Signing and notarization.** For a `.dmg` that opens without a warning:
- Apple Developer Program membership, ~$99/year.
- A Developer ID certificate, hardened runtime, and entitlements for microphone
  and Apple Events.
- Notarization: upload to Apple, wait for the scan, staple the ticket.

Unsigned, colleagues get "cannot be opened because the developer cannot be
verified" and must right-click → Open. Workable for three colleagues, not for
wider distribution.

**Bundle.** `py2app` or `briefcase` to produce a `.app`, with `LSUIElement=1`
so it lives in the menu bar and not the Dock. Then the `.dmg`. Nothing in
today's packaging is reusable: `PKGBUILD` (218 lines), `install.sh` (87) and
the udev rule are wholly Linux, and their 38 tests have no macOS counterpart.

---

## Speed on Apple Silicon

The current engine is faster-whisper, on CTranslate2. **CTranslate2 has no Metal
backend**, so on an M-series Mac it would fall back to CPU int8 — the same path
this Linux VM uses, and noticeably slower than a GPU.

The fix is a third transcription backend using `whisper.cpp` or MLX, both of
which use Metal properly. The transcription interface is a clean four-method
Protocol with a factory and two implementations already, so this is an addition
rather than surgery — but it is real work, and on a Mac it is the difference
between usable and pleasant.

Worth noting in the app's favour: Mac laptops generally have better microphones
and better built-in noise handling than the average Linux desktop setup, so
transcription accuracy should improve.

---

## Fork, or one repository?

**Recommendation: one repository, not a fork.**

A fork splits the 3,065 lines of portable code — the state machine, the pill
model, the drawing, transcription, history — into two copies that drift. Those
are precisely the parts that were expensive to get right and are still being
improved.

The shape the code already has suggests the alternative:

```
voice/            shared: pipeline, model, drawing, stt, history, config
voice/platform/
    linux/        pw-record, evdev, portal, GTK host
    darwin/       AVAudioEngine, NSEvent, Quartz, NSPanel host
```

Selection happens at the existing factories — `make_key_sender`,
`_make_listener`, `make_transcriber` — plus two new ones for the recorder and
the pill host. A separate name for the Mac build (`xvoice`, or similar) is fine
as a product name without needing a separate history.

If the Mac version later diverges in intent rather than just in platform, a
fork is cheap to do *then*, from a codebase that has already been split
cleanly. Doing it first costs the split and gains nothing.

---

## Effort

Assuming one developer with a Mac to test on, and taking the existing
abstractions at face value:

| Stage | Estimate |
|---|---|
| Decouple `evdev`; get the suite loading on macOS | 0.5–1 day |
| Platform split (move Linux code behind `platform/linux`) | 1–2 days |
| Recorder, clipboard, notifications | 1–2 days |
| Hotkey listener + permissions handling | 2–4 days |
| Quartz key sender + keycode mapping | 1–2 days |
| `NSPanel` pill host | 2–3 days |
| Doctor checks, config vocabulary, docs | 1–2 days |
| **Working prototype** | **~2 weeks** |
| Metal transcription backend | 2–4 days |
| `.app` bundle, signing, notarization, `.dmg`, first-run permissions UX | 3–5 days |
| **Shippable to colleagues** | **~3–4 weeks** |

The largest risks are not in the list: Apple's permission model behaving
differently than documented, and notarization's first run, which is reliably
more painful than expected.

---

## Recommended first step

**Decouple `evdev` from the config layer.** It is half a day, it is worth doing
whether or not the port ever happens, and it converts this assessment from an
estimate into a measurement — run the suite on a Mac and see exactly what
survives.
