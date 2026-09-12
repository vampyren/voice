# Deferred review findings (phase 1)

Minor items raised during the phase 1 reviews that were judged not to block the branch. Each is real; none affects a happy-path dictation. Pick them up in phase 2 or when touching the file.

## Concurrency and lifecycle
- `voice/daemon.py`: two warmup threads can overlap after a fast profile switch (duplicate "Running on CPU" notification and tray emit; the model lock prevents a double load).
- `voice/ipc.py`: two simultaneous cold starts can both probe "nothing listening", both unlink, and both bind; `stop()` never joins the serve thread; any read error is labelled "timeout".
- `voice/pipeline.py`: `recorder.stop()` runs under the state lock (up to ~4 s); a reserved TRANSCRIBING/INJECTING state has no recovery if the executor dispatch itself raises; `_thread_executor` discards the future.
- `voice/hotkey/evdev_listener.py`: instance-state hazard if `start()` is called again after a timed-out `stop()` join; `_rescan` exceptions propagate out of `start()`; a factory returning the same device object is closed as a duplicate (test doubles only).
- `voice/ui/settings.py`: narrow read-then-write window between the on-disk `stt.active` carry-over and the final save.

## Behaviour and UX
- `voice/ui/tray.py`: old `QAction`s are removed but not `deleteLater()`'d on `set_profiles`.
- `voice/ui/settings.py`: adding an already-present template is a silent no-op; no Cancel/revert button (Close discards).
- `voice/ui/notify.py`: `notify-send` processes are never reaped.
- `voice/inject/portal.py`: `close()` swallows errors without logging; `TokenStore` has a benign TOCTOU.
- `voice/inject/fallback.py` and `clipboard.py`: duplicated subprocess-error mapping; `make_key_sender` ignores `available()` for the portal.
- `voice/history.py`: `keep_audio` stores the array by reference; `_flush` chmods after writing.
- `voice/config.py`: `load()` writes the default file before chmod (no secrets at that point); importing `Config` now pulls in `evdev` via chord validation.
- `voice/doctor.py`: `fallback senders` reports ✔ with none installed; the individual probes have no unit tests.
- `voice/stt/openai_compat.py`: profile-level prompt fallback untested; OpenRouter host match is a substring test.
- `install.sh`: throwaway `sed` pass; the uninstall `|| true` masks a real `rm` failure.
- `tests/ui/test_settings.py::test_close_discards_edits_and_never_touches_the_callers_config` asserts nothing that could fail; strengthen or delete.
- Dictionary tab gives most width to the Flags column.

## Cleanup candidates cut by the final review
- Profile-template duplication between `voice/config.py` `DEFAULT_CONFIG` and `voice/ui/settings.py` `PROFILE_TEMPLATES`.
- Copy-paste between `Daemon.build()` and `Daemon.apply_config()` for the injector.
- Untested portal session lifecycle and daemon `run()`/`shutdown()` paths (boundary-only today).

## Raised while fixing the 2026-09-12 dictation-reliability incident

Found while investigating a dictation that appeared lost and a stop press that
started a new recording (branch `fix/reliable-dictation`). Each is real; none
is the cause of that incident, and each was left alone rather than widen the
branch.

- `voice/hotkey/portal_listener.py`: `stop()` calls neither `Session.Close()`
  nor `RemoveMatch` - teardown happens as a side effect of the connection
  dropping. A race between `_close()` setting `self._conn = None` and `_open()`
  assigning a fresh connection can strand that connection, its match rule and
  its portal session for the life of the daemon. Observed live: one daemon
  owning six D-Bus connections. **Inert as an event source** - a dump of every
  match rule (`org.freedesktop.DBus.Debug.Stats.GetAllMatchRules`) showed
  exactly one live `org.freedesktop.portal.GlobalShortcuts` subscription - so
  it leaks, it does not double-deliver.
- `voice/hotkey/portal_listener.py`: `modifiers_held()` returns `False`
  unconditionally, so the injector's `MODIFIER_WAIT_S` loop is dead code on the
  portal backend. The paste chord can be sent while the owner is still
  physically holding the Ctrl of `Ctrl+Space`.
- `voice/hotkey/portal_listener.py`: no idempotence gate on `Activated`. The
  evdev path drops auto-repeat (`keyspec.py`, `value == 2`) and re-press; the
  portal path forwards whatever the compositor sends. A compositor that emits
  two `Activated` per press turns one toggle into start-then-stop, and the
  recording dies as "too short".
- `voice/pipeline.py`: `toggle()` can read TRANSCRIBING, have the worker reach
  IDLE underneath it, and then put "Still working" on the pill for 2 s while
  the pipeline is idle. The press genuinely did nothing, so the notice is not
  a lie, but it describes a state that has already been left.
- ~~a paste the injector cannot verify still ends in a plain checkmark~~ -
  **decided and shipped on this branch.** The owner chose to be told: an
  unverifiable paste now reports `BLIND_PASTE` and the pill says "Copied". It
  names no chord: the owner was shown "Use Ctrl+Shift+V" while pasting into a
  browser, and with the window unknown neither chord can be recommended. The
  cost was accepted knowingly - it appears on every dictation on a desktop that
  will not name the focused window, i.e. GNOME - and on Plasma it is nearly
  never seen, because KWin does name it.
- `voice/inject/window.py`: `terminal_chord_is_unreachable` now catches "the
  window cannot be read" and "`terminal_classes` is empty", but it cannot catch
  a terminal that simply is not in the list - Ghostty
  (`com.mitchellh.ghostty`), a renamed WezTerm, anything unlisted. Only running
  the command and reporting the class it actually returns would close that;
  `voice doctor` is the natural place.
- `voice/inject/injector.py`: `WINDOW_COMMAND_TIMEOUT_S` is 1.0 s and the
  command now runs *inside* the focus window, between the pill being unmapped
  and the chord being sent. Raising it would make a slow `kdotool` more likely
  to answer but lengthens the gap in which focus is back and nothing has been
  pasted. The timeout is logged distinctly so the trade-off can be judged from
  a real machine rather than guessed at.
- `voice/inject/injector.py`: `run_window_command` runs with `shell=True`, so a
  timeout kills only `/bin/sh`. The built-in Hyprland and Sway commands are
  pipelines (`hyprctl | awk`, `swaymsg | jq | head`), where `sh` does not
  `exec` and stays a real parent - a wedged compositor socket therefore leaks
  one `swaymsg` and one `jq` per dictation while the new timeout line reports a
  clean timeout each time. Fixing it means `start_new_session=True` plus a
  process-group kill, which needs `Popen` and would change the `run=` seam the
  existing tests use, or parsing in Python instead of a shell pipeline.
  Does NOT currently reach either desktop the owner uses: Plasma's default is
  `kdotool`, a single process that `sh` execs and the timeout really does kill,
  or no command at all; GNOME runs no command at all. It would start to matter
  the moment `KWIN_QUERY` (a pipeline) is promoted to the Plasma default - see
  the last entry in this section - or if anyone configures a pipeline by hand
  in `inject.active_window_command`.
- `voice/inject/injector.py`: `run_window_command`'s `cp.returncode == 0` check
  is vacuous for all three built-in commands, because they are shell pipelines
  and the status is `head`/`sed`/`awk`'s, not `qdbus`/`hyprctl`/`swaymsg`'s. A
  compositor query that errors *instantly* (a D-Bus error, an unreachable sway
  socket) therefore looks exactly like "no window is focused": empty output,
  `None`, and `FocusedWindow` keeps asking it on every dictation forever.
  `set -o pipefail` is not available - `shell=True` runs `/bin/sh`, which is
  dash on Debian/Ubuntu - so the real fix is parsing in Python rather than in a
  pipeline. The hang case *is* handled (`FocusedWindow` gives up after one
  timeout); only the instant-error case is not.
- `voice/inject/injector.py`: `inject.restore_clipboard` is now a no-op on any
  desktop that will not name the focused window, because every paste there
  reports `BLIND_PASTE` and the transcript must stay on the clipboard for the
  pill's "Use Ctrl+Shift+V" to mean anything. That is the right trade, but it
  silently discards the owner's previous clipboard contents on every dictation
  on GNOME. Documented in `config.py`; not surfaced by `voice doctor`, and no
  way to opt out short of setting one chord for every window.
- ~~the KWin `queryWindowInfo` command is written and tested but not the Plasma
  default, pending verification~~ - **verified 2026-09-12 on Plasma 6: it is an
  interactive window picker.** Untouched it times out with no output; it answers
  only after a window is clicked. Removed from the codebase entirely, with the
  measurement recorded in `voice/inject/window.py` so nobody adds it back. The
  fixture and its parser tests went with it.

## Accepted, with reasons (not defects)

- ~~`inject.pill_focus = "paste"` makes every dictation report
  `BLIND_PASTE`~~ - **resolved**, by the owner's own suggestion: the focused
  window is now read when the dictation BEGINS, before the pill is on screen,
  and replayed at paste time. There is no longer a moment where the pill owns
  the keyboard and the answer describes the pill, so every policy gets a real
  window class and the trust machinery this entry described is gone. It also
  took the window command out of the paste path, where it sat between focus
  returning and the chord going out.
- `voice/daemon.py` `_preview_pill`: a narrow gap remains between checking the
  paste-in-flight flag and swapping in the preview client. **Owner decision
  (2026-09-12): will not fix.** It requires previewing pill placement in the
  settings window during the ~0.15 s of a paste, and the owner does not dictate
  while in settings. Recorded so it is a decision rather than an oversight.
